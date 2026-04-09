#!/usr/bin/env python3
"""Daily performance report — ALL leagues (whitelisted + others).

Source 1: /opt/bottie/data/trades.jsonl              — Bottie executed trades
Source 2: /opt/bottie/data/cannae/closed_positions.json + current_positions.json
Source 3: /opt/bottie/config.yaml + data/ssot/rules.yaml — whitelist status

Output:   /opt/bottie/reports/YYYY-MM-DD-perf.md
Cron:     09:00 CET (07:00 UTC)
"""
import json
import os
import sys
import yaml
from collections import defaultdict
from datetime import datetime, timedelta, timezone

TRADES = "/opt/bottie/data/trades.jsonl"
CLOSED = "/opt/bottie/data/cannae/closed_positions.json"
CURRENT = "/opt/bottie/data/cannae/current_positions.json"
CONFIG = "/opt/bottie/config.yaml"
REPORT_DIR = "/opt/bottie/reports"

WINDOWS = [("7d", 7), ("14d", 14), ("30d", 30)]

US_SPORTS = {"nba", "nhl", "mlb", "nfl", "cbb", "ncaa"}


def slug_league(slug):
    if not slug:
        return None
    parts = slug.split("-")
    return parts[0] if parts else None


def slug_date(slug):
    if not slug:
        return None
    parts = slug.split("-")
    if len(parts) < 5:
        return None
    return "-".join(parts[-3:])


def is_more_markets(slug):
    return slug and "more-markets" in slug


def classify_leg(title, outcome):
    """(line, side) where line in {win,draw,spread,ou,btts,prop} and side in {yes,no}."""
    t = (title or "").lower()
    o = (outcome or "").lower()
    if "draw" in t:
        side = "yes" if o == "yes" else ("no" if o == "no" else "?")
        return "draw", side
    if "spread" in t or " (-" in t or " (+" in t:
        return "spread", "team"  # team-named outcomes, side is meaningless
    if "o/u" in t or "over/under" in t or " total" in t:
        side = "over" if o in ("over", "yes") else ("under" if o in ("under", "no") else "?")
        return "ou", side
    if "both teams to score" in t or "btts" in t:
        side = "yes" if o == "yes" else ("no" if o == "no" else "?")
        return "btts", side
    if "win" in t:
        side = "yes" if o == "yes" else ("no" if o == "no" else "?")
        return "win", side
    return "prop", "team"


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_trades():
    trades = []
    if not os.path.exists(TRADES):
        return trades
    with open(TRADES) as f:
        for line in f:
            try:
                trades.append(json.loads(line))
            except Exception:
                continue
    return trades


def load_cannae():
    positions = []
    for path in (CLOSED, CURRENT):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                positions.extend(json.load(f))
        except Exception:
            continue
    return positions


def load_whitelist():
    """Returns {league: status} where status in {ACTIVE, MONITOR, BLACKLIST, OFF}."""
    if not os.path.exists(CONFIG):
        return {}
    try:
        with open(CONFIG) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    wl = {}
    for w in (cfg.get("copy_trading") or {}).get("watchlist") or []:
        if (w.get("name") or "").lower() != "cannae":
            continue
        for lg in w.get("leagues") or []:
            wl[lg.lower()] = "ACTIVE"
    return wl


def aggregate_trades(trades, days):
    """Per league × per leg-type Bottie stats over last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = defaultdict(lambda: defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "staked": 0.0}))
    league_total = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "staked": 0.0})
    for t in trades:
        slug = t.get("event_slug", "") or ""
        if is_more_markets(slug):
            continue
        league = slug_league(slug)
        if not league:
            continue
        resolved = parse_iso(t.get("resolved_at"))
        if not resolved or resolved < cutoff:
            continue
        if t.get("result") not in ("win", "loss"):
            continue
        line, side = classify_leg(t.get("market_title", ""), t.get("outcome", ""))
        key = f"{line}_{side}"
        d = out[league][key]
        lt = league_total[league]
        for dd in (d, lt):
            dd["n"] += 1
            dd["staked"] += float(t.get("size_usdc", 0) or 0)
            pnl = float(t.get("actual_pnl", t.get("pnl", 0)) or 0)
            dd["pnl"] += pnl
            if t.get("result") == "win":
                dd["w"] += 1
            else:
                dd["l"] += 1
    return out, league_total


def aggregate_cannae(positions, days):
    """Per league: cost basis + realized pnl over last N days (by event date)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = defaultdict(lambda: {"games": set(), "bought": 0.0, "realized": 0.0, "n_legs": 0, "n_resolved": 0})
    seen = set()
    for p in positions:
        slug = p.get("eventSlug", "") or ""
        if not slug or is_more_markets(slug):
            continue
        d = slug_date(slug)
        if not d or not (cutoff <= d <= today):
            continue
        league = slug_league(slug)
        if not league:
            continue
        # Dedupe by (slug, conditionId, outcome)
        k = (slug, p.get("conditionId", ""), p.get("outcome", ""))
        if k in seen:
            continue
        seen.add(k)
        avg = float(p.get("avgPrice", 0) or 0)
        bought = float(p.get("totalBought", 0) or 0) * avg
        rp = p.get("realizedPnl")
        agg = out[league]
        agg["games"].add(slug)
        agg["bought"] += bought
        agg["n_legs"] += 1
        if rp is not None:
            agg["realized"] += float(rp)
            agg["n_resolved"] += 1
    return out


def fmt_pct(num, den):
    if not den:
        return "  ·  "
    return f"{100 * num / den:+.1f}%"


def fmt_wr(w, n):
    if not n:
        return "  ·  "
    return f"{100 * w / n:.1f}%"


def render_summary(trades_30, cannae_30):
    tot_n = sum(d["n"] for d in trades_30.values())
    tot_pnl = sum(d["pnl"] for d in trades_30.values())
    tot_staked = sum(d["staked"] for d in trades_30.values())
    tot_w = sum(d["w"] for d in trades_30.values())
    c_bought = sum(d["bought"] for d in cannae_30.values())
    c_real = sum(d["realized"] for d in cannae_30.values())
    lines = [
        "## Summary (30d)",
        "",
        f"- **Bottie**: {tot_n} trades | WR {fmt_wr(tot_w, tot_n)} | "
        f"staked ${tot_staked:,.0f} | pnl ${tot_pnl:+,.0f} | ROI {fmt_pct(tot_pnl, tot_staked)}",
        f"- **Cannae**: {len(cannae_30)} leagues | bought ${c_bought:,.0f} | "
        f"realized ${c_real:+,.0f} | ROI {fmt_pct(c_real, c_bought)}",
        "",
    ]
    return "\n".join(lines)


def render_league_table(label, trades_agg, cannae_agg, whitelist):
    """Wide table: per league Bottie + Cannae stats for one window."""
    leagues = set(trades_agg.keys()) | set(cannae_agg.keys())
    rows = []
    for lg in leagues:
        bt = trades_agg.get(lg, {"n": 0, "w": 0, "pnl": 0.0, "staked": 0.0})
        ca = cannae_agg.get(lg, {"games": set(), "bought": 0.0, "realized": 0.0, "n_resolved": 0})
        status = whitelist.get(lg, "OFF")
        rows.append({
            "lg": lg,
            "status": status,
            "c_games": len(ca.get("games", [])),
            "c_bought": ca.get("bought", 0),
            "c_real": ca.get("realized", 0),
            "c_resolved": ca.get("n_resolved", 0),
            "b_n": bt["n"],
            "b_w": bt["w"],
            "b_pnl": bt["pnl"],
            "b_staked": bt["staked"],
        })
    rows.sort(key=lambda r: (-r["c_bought"], -r["b_n"]))
    out = [
        f"## League × {label}",
        "",
        "| Lg | Status | Cannae games | Cannae$ | Cannae ROI | Bottie n | Bottie WR | Bottie$ | Bottie ROI | Δ vs Cannae |",
        "|----|--------|-------------:|--------:|-----------:|---------:|----------:|--------:|-----------:|------------:|",
    ]
    for r in rows:
        c_roi = (100 * r["c_real"] / r["c_bought"]) if r["c_bought"] else None
        b_roi = (100 * r["b_pnl"] / r["b_staked"]) if r["b_staked"] else None
        delta = (b_roi - c_roi) if (b_roi is not None and c_roi is not None) else None
        out.append(
            f"| {r['lg']} | {r['status']} | {r['c_games']} | "
            f"${r['c_bought']:,.0f} | "
            f"{(f'{c_roi:+.1f}%' if c_roi is not None else '·')} | "
            f"{r['b_n']} | "
            f"{(f'{100*r['b_w']/r['b_n']:.1f}%' if r['b_n'] else '·')} | "
            f"${r['b_staked']:,.0f} | "
            f"{(f'{b_roi:+.1f}%' if b_roi is not None else '·')} | "
            f"{(f'{delta:+.1f}pp' if delta is not None else '·')} |"
        )
    out.append("")
    return "\n".join(out)


def render_leg_type_table(label, trades_agg_by_league):
    """Per leg-type aggregate across all leagues."""
    by_leg = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "staked": 0.0})
    for league_legs in trades_agg_by_league.values():
        for leg, d in league_legs.items():
            agg = by_leg[leg]
            agg["n"] += d["n"]
            agg["w"] += d["w"]
            agg["l"] += d["l"]
            agg["pnl"] += d["pnl"]
            agg["staked"] += d["staked"]
    rows = sorted(by_leg.items(), key=lambda kv: -kv[1]["staked"])
    out = [
        f"## Leg type × {label}",
        "",
        "| Leg | n | W | L | WR | Staked | PnL | ROI |",
        "|-----|---:|---:|---:|---:|-------:|----:|----:|",
    ]
    for leg, d in rows:
        if d["n"] == 0:
            continue
        wr = 100 * d["w"] / d["n"]
        roi = (100 * d["pnl"] / d["staked"]) if d["staked"] else 0
        out.append(
            f"| {leg} | {d['n']} | {d['w']} | {d['l']} | {wr:.1f}% | "
            f"${d['staked']:,.0f} | ${d['pnl']:+,.0f} | {roi:+.1f}% |"
        )
    out.append("")
    return "\n".join(out)


def render_league_x_leg_matrix(trades_agg_by_league, top_n=20):
    """League × leg type matrix (top N leagues by trade count)."""
    leg_order = ["win_yes", "win_no", "draw_yes", "draw_no", "spread_team",
                 "ou_over", "ou_under", "btts_yes", "btts_no", "prop_team"]
    league_n = {lg: sum(d["n"] for d in legs.values()) for lg, legs in trades_agg_by_league.items()}
    top = sorted(league_n.items(), key=lambda kv: -kv[1])[:top_n]
    out = [
        "## League × Leg type matrix (30d, top 20 leagues by trades)",
        "",
        "ROI per cell (n in parens). Empty = no trades.",
        "",
        "| Lg | " + " | ".join(leg_order) + " |",
        "|----|" + "|".join(["---:"] * len(leg_order)) + "|",
    ]
    for lg, _ in top:
        cells = []
        for leg in leg_order:
            d = trades_agg_by_league[lg].get(leg)
            if not d or d["n"] == 0:
                cells.append("·")
            else:
                roi = (100 * d["pnl"] / d["staked"]) if d["staked"] else 0
                cells.append(f"{roi:+.0f}% ({d['n']})")
        out.append(f"| {lg} | " + " | ".join(cells) + " |")
    out.append("")
    return "\n".join(out)


def main():
    trades = load_trades()
    positions = load_cannae()
    whitelist = load_whitelist()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(REPORT_DIR, exist_ok=True)
    out_path = os.path.join(REPORT_DIR, f"{today}-perf.md")

    parts = [
        f"# Bottie daily performance — {today}",
        "",
        f"Source: {len(trades)} trades, {len(positions)} Cannae positions, "
        f"{len(whitelist)} whitelisted leagues.",
        "",
    ]

    # 30d sections
    trades_30_by_league_legs, trades_30_league_total = aggregate_trades(trades, 30)
    cannae_30 = aggregate_cannae(positions, 30)
    parts.append(render_summary(trades_30_league_total, cannae_30))
    parts.append(render_league_table("30d", trades_30_league_total, cannae_30, whitelist))
    parts.append(render_leg_type_table("30d", trades_30_by_league_legs))
    parts.append(render_league_x_leg_matrix(trades_30_by_league_legs))

    # 14d + 7d (compact)
    for label, days in (("14d", 14), ("7d", 7)):
        legs_by_lg, lg_total = aggregate_trades(trades, days)
        ca = aggregate_cannae(positions, days)
        parts.append(render_league_table(label, lg_total, ca, whitelist))
        parts.append(render_leg_type_table(label, legs_by_lg))

    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    print(out_path)


if __name__ == "__main__":
    main()
