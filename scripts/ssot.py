#!/usr/bin/env python3
"""SSOT Analysis Tool — Single Source of Truth for all Bottie analytics.

Reads trades.jsonl + optional Cannae closed_positions.json.
Outputs JSON (for dashboard) + Markdown (for humans).

Usage:
    python3 scripts/ssot.py                    # markdown to stdout
    python3 scripts/ssot.py --json             # JSON to stdout
    python3 scripts/ssot.py --out data/ssot.json  # JSON to file (for dashboard)
"""

import json
import math
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Add project root so we can import scripts.lib.analyse
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.lib.analyse import classify_sport, get_game_line, FOOTBALL_SLUGS

# ── Data Loading ──────────────────────────────────────────────────────────────

def load_trades(path: Path) -> list:
    """Load trades.jsonl safely (copy first to avoid race with Rust rewrite)."""
    if not path.exists():
        return []
    # Copy to temp to avoid reading during Rust rewrite_all()
    tmp = Path(tempfile.mktemp(suffix=".jsonl"))
    try:
        shutil.copy2(path, tmp)
        trades = []
        for line in tmp.read_text().splitlines():
            if line.strip():
                try:
                    t = json.loads(line)
                    if t.get("filled") and not t.get("dry_run"):
                        trades.append(t)
                except json.JSONDecodeError:
                    continue
        return trades
    finally:
        tmp.unlink(missing_ok=True)


def load_cannae_positions(path: Path) -> list:
    """Load Cannae closed_positions.json."""
    if not path.exists():
        return []
    return json.load(open(path))


# ── Leg Classification ────────────────────────────────────────────────────────

def classify_leg(title: str, outcome: str) -> str:
    """Classify a trade leg: WIN_YES, WIN_NO, DRAW_YES, DRAW_NO, SPREAD, TOTALS, OTHER."""
    game_line = get_game_line(title)
    out = outcome.lower()
    if game_line == "win":
        return f"WIN_{out.upper()}"
    elif game_line == "draw":
        return f"DRAW_{out.upper()}"
    elif game_line == "spread":
        return "SPREAD"
    elif game_line == "totals":
        return "TOTALS"
    return "OTHER"


def classify_combo(legs: list[str]) -> str:
    """Classify a game's combination type from its leg types."""
    s = set(legs)
    if s == {"WIN_YES"}:
        return "WIN_YES_ONLY"
    if s == {"WIN_NO"}:
        return "WIN_NO_ONLY"
    if s == {"WIN_NO", "DRAW_YES"}:
        return "WIN_NO_DRAW_YES"
    if s == {"WIN_YES", "DRAW_NO"}:
        return "WIN_YES_DRAW_NO"
    if s == {"SPREAD"}:
        return "SPREAD_ONLY"
    if s == {"TOTALS"}:
        return "TOTALS_ONLY"
    if "DRAW_YES" in s and len(s) == 1:
        return "DRAW_YES_ONLY"
    return "MULTI_LEG"


# ── Wilson Confidence Interval ────────────────────────────────────────────────

def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for binomial proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    lo = (centre - spread) / denom
    hi = (centre + spread) / denom
    return (max(0, lo), min(1, hi))


# ── Game-Level Rollup ─────────────────────────────────────────────────────────

def game_rollup(trades: list) -> list:
    """Group resolved trades by event_slug into game-level records."""
    # Only resolved trades
    resolved = [t for t in trades if t.get("result") in ("win", "loss", "refund")]

    # Group by event_slug
    games = defaultdict(list)
    for t in resolved:
        slug = (t.get("event_slug") or "").strip()
        if not slug:
            slug = t.get("condition_id", "unknown")
        # Normalize: strip -more-markets suffix
        slug = slug.removesuffix("-more-markets")
        games[slug].append(t)

    result = []
    for slug, legs in games.items():
        leg_types = [classify_leg(t.get("market_title", ""), t.get("outcome", "")) for t in legs]
        combo = classify_combo(leg_types)

        total_pnl = sum(t.get("pnl", 0) or 0 for t in legs)
        total_invested = sum(t.get("size_usdc", 0) or 0 for t in legs)
        prefix = slug.split("-")[0] if slug else "unknown"
        league = prefix if not prefix.startswith("0x") else "other"
        sport = classify_sport(legs[0].get("market_title", ""), slug)

        # Per-leg detail
        leg_detail = []
        for t, lt in zip(legs, leg_types):
            leg_detail.append({
                "type": lt,
                "outcome": t.get("outcome"),
                "price": t.get("price", 0),
                "size_usdc": t.get("size_usdc", 0),
                "pnl": t.get("pnl", 0) or 0,
                "result": t.get("result"),
                "closing_price": t.get("closing_price"),
            })

        result.append({
            "event_slug": slug,
            "league": league,
            "sport": sport,
            "combo": combo,
            "legs": leg_detail,
            "leg_count": len(legs),
            "total_pnl": round(total_pnl, 2),
            "total_invested": round(total_invested, 2),
            "game_won": total_pnl > 0,
            "resolved_at": max(t.get("resolved_at", "") or "" for t in legs),
            "title": legs[0].get("market_title", ""),
        })

    return sorted(result, key=lambda g: g["resolved_at"], reverse=True)


# ── Per-Combo Stats ───────────────────────────────────────────────────────────

def combo_stats(games: list) -> dict:
    """Stats per combination type."""
    groups = defaultdict(list)
    for g in games:
        groups[g["combo"]].append(g)

    stats = {}
    for combo, gs in sorted(groups.items()):
        wins = sum(1 for g in gs if g["game_won"])
        total = len(gs)
        pnl = sum(g["total_pnl"] for g in gs)
        invested = sum(g["total_invested"] for g in gs)
        roi = (pnl / invested * 100) if invested else 0
        lo, hi = wilson_ci(wins, total)
        stats[combo] = {
            "games": total,
            "wins": wins,
            "losses": total - wins,
            "wr": round(wins / total * 100, 1) if total else 0,
            "wr_ci": [round(lo * 100, 1), round(hi * 100, 1)],
            "pnl": round(pnl, 2),
            "invested": round(invested, 2),
            "roi": round(roi, 1),
        }
    return stats


# ── Per-League Stats ──────────────────────────────────────────────────────────

def league_stats(games: list) -> dict:
    """Stats per league."""
    groups = defaultdict(list)
    for g in games:
        groups[g["league"]].append(g)

    stats = {}
    for league, gs in sorted(groups.items(), key=lambda x: sum(g["total_pnl"] for g in x[1]), reverse=True):
        wins = sum(1 for g in gs if g["game_won"])
        total = len(gs)
        pnl = sum(g["total_pnl"] for g in gs)
        invested = sum(g["total_invested"] for g in gs)
        roi = (pnl / invested * 100) if invested else 0
        lo, hi = wilson_ci(wins, total)
        sport = gs[0]["sport"] if gs else "unknown"
        stats[league] = {
            "sport": sport,
            "games": total,
            "wins": wins,
            "losses": total - wins,
            "wr": round(wins / total * 100, 1) if total else 0,
            "wr_ci": [round(lo * 100, 1), round(hi * 100, 1)],
            "pnl": round(pnl, 2),
            "invested": round(invested, 2),
            "roi": round(roi, 1),
        }
    return stats


# ── CLV Analysis ──────────────────────────────────────────────────────────────

def clv_analysis(games: list) -> dict:
    """CLV stats for trades that have closing_price data."""
    clv_trades = []
    for g in games:
        for leg in g["legs"]:
            if leg["closing_price"] is not None and leg["price"] > 0:
                clv = leg["closing_price"] - leg["price"]
                clv_trades.append({
                    "league": g["league"],
                    "combo": g["combo"],
                    "clv": clv,
                    "result": leg["result"],
                    "pnl": leg["pnl"],
                })

    if not clv_trades:
        return {"available": False, "message": "No closing_price data yet — will populate as markets resolve"}

    pos = [t for t in clv_trades if t["clv"] > 0]
    neg = [t for t in clv_trades if t["clv"] <= 0]

    return {
        "available": True,
        "total_trades": len(clv_trades),
        "mean_clv": round(sum(t["clv"] for t in clv_trades) / len(clv_trades), 4),
        "positive_clv": len(pos),
        "negative_clv": len(neg),
        "pos_wr": round(sum(1 for t in pos if t["result"] == "win") / len(pos) * 100, 1) if pos else 0,
        "neg_wr": round(sum(1 for t in neg if t["result"] == "win") / len(neg) * 100, 1) if neg else 0,
    }


# ── Correlation Analysis ─────────────────────────────────────────────────────

def correlation_analysis(games: list) -> dict:
    """Analyze outcome correlation for multi-leg games."""
    multi = [g for g in games if g["leg_count"] >= 2]
    if not multi:
        return {"multi_leg_games": 0}

    # Count outcome combinations
    outcome_combos = defaultdict(int)
    for g in multi:
        results = tuple(l["result"] for l in g["legs"])
        outcome_combos[results] += 1

    # Specific: WIN_NO + DRAW_YES correlation
    wn_dy = [g for g in multi if g["combo"] == "WIN_NO_DRAW_YES"]
    wn_dy_both_win = sum(1 for g in wn_dy if all(l["result"] == "win" for l in g["legs"]))
    wn_dy_both_lose = sum(1 for g in wn_dy if all(l["result"] == "loss" for l in g["legs"]))
    wn_dy_mixed = len(wn_dy) - wn_dy_both_win - wn_dy_both_lose

    return {
        "multi_leg_games": len(multi),
        "outcome_combos": {str(k): v for k, v in outcome_combos.items()},
        "win_no_draw_yes": {
            "total": len(wn_dy),
            "both_win": wn_dy_both_win,
            "both_lose": wn_dy_both_lose,
            "mixed": wn_dy_mixed,
            "correlation": "high" if wn_dy and (wn_dy_both_win + wn_dy_both_lose) / len(wn_dy) > 0.7 else "low",
        },
    }


# ── Daily PnL ────────────────────────────────────────────────────────────────

def daily_pnl(games: list) -> list:
    """PnL per day."""
    days = defaultdict(lambda: {"pnl": 0, "games": 0, "wins": 0})
    for g in games:
        day = (g["resolved_at"] or "")[:10]
        if not day:
            continue
        days[day]["pnl"] += g["total_pnl"]
        days[day]["games"] += 1
        if g["game_won"]:
            days[day]["wins"] += 1

    return [
        {"date": d, "pnl": round(v["pnl"], 2), "games": v["games"],
         "wins": v["wins"], "wr": round(v["wins"] / v["games"] * 100, 1) if v["games"] else 0}
        for d, v in sorted(days.items())
    ]


# ── Summary ───────────────────────────────────────────────────────────────────

def build_summary(games: list) -> dict:
    """Top-level summary stats."""
    total = len(games)
    wins = sum(1 for g in games if g["game_won"])
    pnl = sum(g["total_pnl"] for g in games)
    invested = sum(g["total_invested"] for g in games)
    lo, hi = wilson_ci(wins, total)
    return {
        "total_games": total,
        "wins": wins,
        "losses": total - wins,
        "wr": round(wins / total * 100, 1) if total else 0,
        "wr_ci": [round(lo * 100, 1), round(hi * 100, 1)],
        "total_pnl": round(pnl, 2),
        "total_invested": round(invested, 2),
        "roi": round(pnl / invested * 100, 1) if invested else 0,
    }


# ── Full SSOT Report ─────────────────────────────────────────────────────────

def build_report(trades_path: Path, cannae_path: Path = None) -> dict:
    """Build the complete SSOT report."""
    trades = load_trades(trades_path)
    games = game_rollup(trades)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trades_file": str(trades_path),
        "trade_count": len(trades),
        "summary": build_summary(games),
        "by_combo": combo_stats(games),
        "by_league": league_stats(games),
        "clv": clv_analysis(games),
        "correlation": correlation_analysis(games),
        "daily": daily_pnl(games),
        "games": games,
    }

    return report


# ── Markdown Output ───────────────────────────────────────────────────────────

def report_to_markdown(r: dict) -> str:
    """Convert SSOT report to readable markdown."""
    s = r["summary"]
    lines = [
        f"# SSOT Analysis — {r['generated_at'][:10]}",
        f"",
        f"**{s['total_games']} games** | {s['wins']}W/{s['losses']}L | "
        f"WR: {s['wr']}% [{s['wr_ci'][0]}-{s['wr_ci'][1]}%] | "
        f"PnL: ${s['total_pnl']:+.2f} | ROI: {s['roi']}%",
        f"",
        f"## Per Combination Type",
        f"| Combo | Games | W/L | WR% | WR CI | PnL | ROI |",
        f"|-------|------:|-----|----:|-------|----:|----:|",
    ]
    for combo, cs in r["by_combo"].items():
        lines.append(
            f"| {combo} | {cs['games']} | {cs['wins']}W/{cs['losses']}L | "
            f"{cs['wr']}% | [{cs['wr_ci'][0]}-{cs['wr_ci'][1]}%] | "
            f"${cs['pnl']:+.2f} | {cs['roi']}% |"
        )

    lines += [
        f"",
        f"## Per League",
        f"| League | Sport | Games | W/L | WR% | WR CI | PnL | ROI |",
        f"|--------|-------|------:|-----|----:|-------|----:|----:|",
    ]
    for league, ls in r["by_league"].items():
        lines.append(
            f"| {league} | {ls['sport']} | {ls['games']} | {ls['wins']}W/{ls['losses']}L | "
            f"{ls['wr']}% | [{ls['wr_ci'][0]}-{ls['wr_ci'][1]}%] | "
            f"${ls['pnl']:+.2f} | {ls['roi']}% |"
        )

    # CLV
    clv = r["clv"]
    lines += [f"", f"## CLV Analysis"]
    if not clv.get("available"):
        lines.append(f"_{clv.get('message', 'No data')}_")
    else:
        lines.append(f"Mean CLV: {clv['mean_clv']:+.4f} | +CLV trades: {clv['positive_clv']} (WR {clv['pos_wr']}%) | -CLV: {clv['negative_clv']} (WR {clv['neg_wr']}%)")

    # Correlation
    corr = r["correlation"]
    lines += [f"", f"## Correlation"]
    lines.append(f"Multi-leg games: {corr['multi_leg_games']}")
    if corr.get("win_no_draw_yes", {}).get("total", 0) > 0:
        wn = corr["win_no_draw_yes"]
        lines.append(f"WIN_NO+DRAW_YES: {wn['total']} games — both win: {wn['both_win']}, both lose: {wn['both_lose']}, mixed: {wn['mixed']} ({wn['correlation']} correlation)")

    # Daily
    lines += [f"", f"## Daily PnL"]
    for d in r["daily"]:
        lines.append(f"  {d['date']}: ${d['pnl']:+.2f} ({d['games']} games, {d['wr']}% WR)")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    trades_path = PROJECT_ROOT / "data" / "trades.jsonl"
    cannae_path = PROJECT_ROOT / "data" / "cannae_closed_positions.json"

    # Allow VPS path override
    if Path("/opt/bottie/data/trades.jsonl").exists():
        trades_path = Path("/opt/bottie/data/trades.jsonl")
    if Path("/opt/bottie/data/cannae/closed_positions.json").exists():
        cannae_path = Path("/opt/bottie/data/cannae/closed_positions.json")

    report = build_report(trades_path, cannae_path)

    if "--json" in sys.argv:
        # JSON output (exclude full games list for brevity)
        compact = {k: v for k, v in report.items() if k != "games"}
        print(json.dumps(compact, indent=2))
    elif "--out" in sys.argv:
        idx = sys.argv.index("--out")
        out_path = Path(sys.argv[idx + 1])
        out_path.write_text(json.dumps(report, indent=2))
        print(f"Written to {out_path} ({len(report['games'])} games)")
    else:
        print(report_to_markdown(report))
