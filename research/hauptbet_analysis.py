#!/usr/bin/env python3
"""
Hauptbet Analysis v3 — Per GAME, per GAME LINE: WR + ROI van de hoofdpositie.

Usage:
  python3 hauptbet_analysis.py                              # Cannae (all sports)
  python3 hauptbet_analysis.py 0xANY_WALLET_ADDRESS         # Any wallet
  python3 hauptbet_analysis.py --sport nba                  # Filter sport
  python3 hauptbet_analysis.py --sport voetbal              # All football leagues

Per GAME (event_slug), per GAME LINE (moneyline/spread/totals/draw/btts):
  → Hauptbet = de SIDE met de meeste USDC across all conditionIds of that game line
  → WR, ROI, PnL per game line per week
  → Conviction: moneyline + draw hauptbet aligned (both backing same team to win)
"""

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

API = "https://data-api.polymarket.com"
UA = {"User-Agent": "HauptbetAnalysis/3.0", "Accept": "application/json"}
CANNAE_CSV = Path(__file__).parent / "cannae_trades" / "cannae_closed_full.csv"

FOOTBALL_LEAGUES = {
    "epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por",
    "bra", "itc", "ere", "es2", "bl2", "sea", "elc", "mex", "fr2", "aus",
    "spl", "efl", "tur", "uef", "ucl", "cdr", "acn", "cde", "ssc", "fif",
}

SPORT_ALIASES = {
    "voetbal": FOOTBALL_LEAGUES, "football": FOOTBALL_LEAGUES, "soccer": FOOTBALL_LEAGUES,
    "nba": {"nba"}, "nhl": {"nhl"}, "mlb": {"mlb"}, "nfl": {"nfl"},
    "us": {"nba", "nhl", "mlb", "nfl", "cbb"},
}


# ── Helpers ─────────────────────────────────────────────────────

def classify_game_line(title: str) -> str:
    t = (title or "").lower()
    if "spread" in t: return "spread"
    if "o/u " in t or "over/under" in t: return "totals"
    if "draw" in t or "end in a draw" in t: return "draw"
    if "both teams" in t or "btts" in t: return "btts"
    return "moneyline"


def detect_league(slug: str) -> str:
    return slug.split("-")[0] if slug else "?"


def iso_week(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"


def wilson_ci(wins: int, total: int, z=1.96) -> tuple:
    if total == 0: return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (round(max(0, center - spread), 4), round(min(1, center + spread), 4))


# ── Data Loading ────────────────────────────────────────────────

def load_legs_from_csv(csv_path: str) -> list:
    """Load individual legs from CSV. Each row = one (conditionId, outcome) position."""
    legs = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            cid = row.get("condition_id", "")
            if not cid: continue
            cost = float(row.get("total_bought", 0) or 0)
            if cost <= 0: continue

            won_raw = row.get("won", "")
            rpnl = float(row.get("realized_pnl", 0) or 0)
            cur_price = float(row.get("cur_price", 0) or 0)

            if won_raw == "1" or cur_price == 1.0:
                result = "WIN"
            elif won_raw == "0" or cur_price == 0.0:
                result = "LOSS"
            else:
                continue

            pnl = rpnl if rpnl != 0 else (-cost if result == "LOSS" else 0)
            if result == "WIN" and rpnl == 0:
                avg_price = float(row.get("avg_price", 0) or 0)
                shares = cost / avg_price if avg_price > 0 else 0
                pnl = shares - cost

            ts = int(row.get("timestamp", 0) or 0)
            slug = (row.get("event_slug", "") or "").split("-more-markets")[0]

            legs.append({
                "cid": cid,
                "outcome": row.get("outcome", ""),
                "title": row.get("title", ""),
                "event_slug": slug,
                "game_line": classify_game_line(row.get("title", "")),
                "league": detect_league(slug),
                "cost": cost,
                "pnl": pnl,
                "result": result,
                "first_ts": ts,
            })
    return legs


def load_legs_from_api(address: str) -> list:
    """Fetch from PM API. Returns individual legs per (cid, outcome)."""
    if not HAS_HTTPX:
        print("ERROR: httpx not installed")
        sys.exit(1)

    client = httpx.Client(headers=UA)

    def fetch(url_tpl, max_off=10000):
        results = []
        off = 0
        while off < max_off:
            try:
                resp = client.get(url_tpl.format(offset=off), timeout=30)
                resp.raise_for_status()
                batch = resp.json()
            except Exception as e:
                print(f"  Stop at offset {off}: {e}")
                break
            if not isinstance(batch, list) or not batch: break
            results.extend(batch)
            if len(batch) < 500: break
            off += 500
        return results

    print("  Fetching trades...")
    trades = fetch(f"{API}/activity?user={address}&type=trade&limit=500&offset={{offset}}")
    print(f"  → {len(trades)} trades")
    print("  Fetching redeems...")
    redeems = fetch(f"{API}/activity?user={address}&type=redeem&limit=500&offset={{offset}}")
    print(f"  → {len(redeems)} redeems")

    # Redeem totals per conditionId
    redeem_by_cid = defaultdict(float)
    for r in redeems:
        cid = r.get("conditionId", "")
        if cid: redeem_by_cid[cid] += float(r.get("usdcSize", 0) or 0)

    # Group trades by (cid, outcome)
    buys = defaultdict(list)
    sells = defaultdict(list)
    for t in trades:
        cid = t.get("conditionId", "")
        outcome = t.get("outcome", "") or ""
        side = t.get("side", "")
        if cid:
            if side == "BUY": buys[(cid, outcome)].append(t)
            elif side == "SELL": sells[(cid, outcome)].append(t)

    # Per conditionId: compute total PnL, then assign to each (cid, outcome)
    # First group by cid
    cid_outcomes = defaultdict(dict)
    for (cid, outcome), tlist in buys.items():
        bc = sum(float(x.get("usdcSize", 0) or 0) for x in tlist)
        sl = sells.get((cid, outcome), [])
        sp = sum(float(s.get("usdcSize", 0) or 0) for s in sl)
        t0 = tlist[0]
        ts_list = [int(x.get("timestamp", 0) or 0) for x in tlist]
        cid_outcomes[cid][outcome] = {
            "buy_cost": bc, "sell_proceeds": sp,
            "title": t0.get("title", ""),
            "slug": (t0.get("eventSlug", "") or t0.get("slug", "") or "").split("-more-markets")[0],
            "first_ts": min(ts_list) if ts_list else 0,
        }

    legs = []
    for cid, outcomes in cid_outcomes.items():
        total_buy = sum(d["buy_cost"] for d in outcomes.values())
        total_sell = sum(d["sell_proceeds"] for d in outcomes.values())
        total_redeem = redeem_by_cid.get(cid, 0)
        cid_pnl = total_sell + total_redeem - total_buy
        resolved = cid in redeem_by_cid or total_sell > total_buy * 0.5

        for outcome, d in outcomes.items():
            if not resolved:
                result = "OPEN"
                pnl = 0
            else:
                # This outcome's share of the conditionId PnL
                # Hauptbet (largest cost) gets attributed the full PnL in select_hauptbets
                # Here we store per-outcome cost and the cid-level PnL for the hauptbet
                result = "WIN" if cid_pnl > 0 else "LOSS"
                pnl = cid_pnl  # will be deduplicated in select_hauptbets

            legs.append({
                "cid": cid, "outcome": outcome, "title": d["title"],
                "event_slug": d["slug"],
                "game_line": classify_game_line(d["title"]),
                "league": detect_league(d["slug"]),
                "cost": d["buy_cost"], "pnl": pnl, "result": result,
                "first_ts": d["first_ts"],
            })

    client.close()
    return legs


# ── Game-Level Hauptbet Selection ───────────────────────────────

def build_game_hauptbets(legs: list) -> list:
    """Per game (event_slug), per game line: find the hauptbet.

    Returns one record per (game, game_line) with:
    - hauptbet outcome, cost, pnl, result
    - conviction flag (moneyline + draw aligned)
    """
    # Group: event_slug → game_line → list of (cid, outcome, cost, pnl, result)
    games = defaultdict(lambda: defaultdict(list))
    for leg in legs:
        slug = leg["event_slug"]
        gl = leg["game_line"]
        if not slug: continue
        games[slug][gl].append(leg)

    results = []
    game_conviction = {}  # slug → conviction info

    for slug, game_lines in games.items():
        league = detect_league(slug)
        is_football = league in FOOTBALL_LEAGUES

        # Per game line: find hauptbet (largest cost across all cids)
        game_hauptbets = {}
        for gl, gl_legs in game_lines.items():
            # Group by conditionId first, pick largest side per cid
            by_cid = defaultdict(list)
            for leg in gl_legs:
                by_cid[leg["cid"]].append(leg)

            # Per conditionId: hauptbet = largest cost side
            # Only use hauptbet's own cost and PnL (we don't copy the hedge)
            cid_hauptbets = []
            for cid, sides in by_cid.items():
                sides.sort(key=lambda s: -s["cost"])
                haupt = sides[0]
                cid_hauptbets.append({
                    "cid": cid,
                    "outcome": haupt["outcome"],
                    "cost": haupt["cost"],
                    "haupt_cost": haupt["cost"],
                    "pnl": haupt["pnl"] if haupt["result"] != "OPEN" else 0,
                    "result": haupt["result"],
                    "title": haupt["title"],
                    "first_ts": haupt["first_ts"],
                })

            # Game line hauptbet = cid with largest haupt_cost
            if not cid_hauptbets: continue
            cid_hauptbets.sort(key=lambda c: -c["haupt_cost"])
            gl_haupt = cid_hauptbets[0]

            # Total cost and PnL across ALL cids in this game line
            gl_total_cost = sum(c["cost"] for c in cid_hauptbets)
            gl_total_pnl = sum(c["pnl"] for c in cid_hauptbets)
            any_open = any(c["result"] == "OPEN" for c in cid_hauptbets)

            if any_open:
                gl_result = "OPEN"
                gl_total_pnl = 0
            elif gl_total_pnl > 0:
                gl_result = "WIN"
            else:
                gl_result = "LOSS"

            game_hauptbets[gl] = {
                "outcome": gl_haupt["outcome"],
                "title": gl_haupt["title"],
                "result": gl_result,
                "cost": gl_total_cost,
                "pnl": gl_total_pnl,
                "first_ts": gl_haupt["first_ts"],
            }

        # Conviction detection (football only):
        # Moneyline hauptbet backs Team A (e.g. "Chelsea NO" = Arsenal wins)
        # Draw hauptbet = "Draw NO" (someone wins outright)
        # Both aligned → conviction
        conviction = False
        if is_football and "moneyline" in game_hauptbets and "draw" in game_hauptbets:
            draw_outcome = game_hauptbets["draw"]["outcome"].lower()
            if draw_outcome == "no":
                conviction = True  # Draw NO + moneyline bet = backing a winner

        # Output one record per game line
        for gl, haupt in game_hauptbets.items():
            if haupt["result"] == "OPEN": continue
            results.append({
                "event_slug": slug,
                "league": league,
                "is_football": is_football,
                "game_line": gl,
                "outcome": haupt["outcome"],
                "title": haupt["title"],
                "result": haupt["result"],
                "cost": round(haupt["cost"], 2),
                "pnl": round(haupt["pnl"], 2),
                "first_ts": haupt["first_ts"],
                "conviction": conviction,
            })

    return results


# ── Stats ───────────────────────────────────────────────────────

def stats(bets):
    n = len(bets)
    if n == 0:
        return {"n": 0, "w": 0, "l": 0, "wr": 0, "roi": 0, "cost": 0, "pnl": 0, "ci": (0, 0)}
    wins = sum(1 for b in bets if b["result"] == "WIN")
    losses = n - wins
    cost = sum(b["cost"] for b in bets)
    pnl = sum(b["pnl"] for b in bets)
    wr = wins / n
    roi = pnl / cost if cost > 0 else 0
    ci = wilson_ci(wins, n)
    return {"n": n, "w": wins, "l": losses, "wr": wr, "roi": roi, "cost": cost, "pnl": pnl, "ci": ci}


def trend_arrow(weekly_wrs: list) -> str:
    if len(weekly_wrs) < 4: return "  "
    mid = len(weekly_wrs) // 2
    first = sum(weekly_wrs[:mid]) / mid
    second = sum(weekly_wrs[mid:]) / (len(weekly_wrs) - mid)
    diff = second - first
    if diff > 0.05: return "↗"
    elif diff < -0.05: return "↘"
    return "→"


# ── Report ──────────────────────────────────────────────────────

def print_report(records, label, sport_filter=None):
    by_line_week = defaultdict(lambda: defaultdict(list))
    by_line_total = defaultdict(list)

    for r in records:
        gl = r["game_line"]
        week = iso_week(r["first_ts"]) if r["first_ts"] else "unknown"
        by_line_week[gl][week].append(r)
        by_line_total[gl].append(r)

    fl = f" | filter: {sport_filter}" if sport_filter else ""
    print()
    print("=" * 95)
    print(f"HAUPTBET ANALYSE v3 — {label}{fl}")
    print(f"Resolved: {len(records)} game line bets")
    print("=" * 95)
    print()
    print("Per GAME, per GAME LINE: hauptbet = grootste positie.")
    print("PnL = alle returns (sells+redeems) - alle kosten per conditionId.")
    print()

    # Overall per game line
    hdr = f"{'Game Line':<12} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'CI 95%':>13} {'ROI':>7} {'PnL':>12} {'Trend':>5}"
    print(hdr)
    print("-" * len(hdr))

    game_lines = ["moneyline", "spread", "totals", "draw", "btts"]
    for gl in game_lines:
        if gl not in by_line_total: continue
        s = stats(by_line_total[gl])
        weekly_wrs = [stats(by_line_week[gl][w])["wr"] for w in sorted(by_line_week[gl]) if stats(by_line_week[gl][w])["n"] >= 3]
        tr = trend_arrow(weekly_wrs)
        print(f"{gl:<12} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}    {tr}")

    s = stats(records)
    print("-" * len(hdr))
    print(f"{'TOTAAL':<12} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")

    # Conviction analysis (football only)
    conviction_bets = [r for r in records if r["conviction"] and r["game_line"] in ("moneyline", "draw")]
    no_conviction = [r for r in records if not r["conviction"] and r["game_line"] in ("moneyline", "draw") and r["is_football"]]

    if conviction_bets:
        print()
        print("--- CONVICTION (moneyline + draw NO aligned) ---")
        s_conv = stats(conviction_bets)
        s_noconv = stats(no_conviction)
        print(f"  With conviction:    {s_conv['n']:>4} bets  WR={s_conv['wr']:.0%} [{s_conv['ci'][0]:.0%}-{s_conv['ci'][1]:.0%}]  ROI={s_conv['roi']:.0%}  PnL=${s_conv['pnl']:>11,.0f}")
        print(f"  Without conviction: {s_noconv['n']:>4} bets  WR={s_noconv['wr']:.0%} [{s_noconv['ci'][0]:.0%}-{s_noconv['ci'][1]:.0%}]  ROI={s_noconv['roi']:.0%}  PnL=${s_noconv['pnl']:>11,.0f}")

        # Split conviction by game line
        conv_ml = [r for r in conviction_bets if r["game_line"] == "moneyline"]
        conv_draw = [r for r in conviction_bets if r["game_line"] == "draw"]
        if conv_ml:
            s = stats(conv_ml)
            print(f"    Conviction ML:    {s['n']:>4} bets  WR={s['wr']:.0%}  ROI={s['roi']:.0%}  PnL=${s['pnl']:>11,.0f}")
        if conv_draw:
            s = stats(conv_draw)
            print(f"    Conviction Draw:  {s['n']:>4} bets  WR={s['wr']:.0%}  ROI={s['roi']:.0%}  PnL=${s['pnl']:>11,.0f}")

    # Per week per game line
    all_weeks = sorted(set(w for glw in by_line_week.values() for w in glw))
    for gl in game_lines:
        if gl not in by_line_week: continue
        print()
        print(f"--- {gl.upper()} per week ---")
        print(f"  {'Week':<10} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'CI 95%':>13} {'ROI':>7} {'PnL':>12}")
        for week in all_weeks:
            if week not in by_line_week[gl]: continue
            s = stats(by_line_week[gl][week])
            print(f"  {week:<10} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")

    # Per league
    by_league = defaultdict(list)
    for r in records:
        by_league[r["league"]].append(r)

    print()
    print("--- PER LEAGUE ---")
    print(f"{'League':<10} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'CI 95%':>13} {'ROI':>7} {'PnL':>12}")
    print("-" * 68)
    for league, bets in sorted(by_league.items(), key=lambda x: -sum(b["pnl"] for b in x[1])):
        s = stats(bets)
        print(f"{league:<10} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")

    # League × game line
    print()
    print("--- LEAGUE x GAME LINE ---")
    print(f"{'League':<10} {'Line':<12} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'ROI':>7} {'PnL':>12}")
    print("-" * 72)
    for league, bets in sorted(by_league.items(), key=lambda x: -sum(b["pnl"] for b in x[1])):
        by_gl = defaultdict(list)
        for b in bets: by_gl[b["game_line"]].append(b)
        for gl in game_lines:
            if gl not in by_gl: continue
            s = stats(by_gl[gl])
            print(f"{league:<10} {gl:<12} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")


# ── Main ────────────────────────────────────────────────────────

def main():
    csv_path = None
    address = None
    sport_filters = []

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--csv" and i + 1 < len(sys.argv):
            csv_path = sys.argv[i + 1]; i += 2
        elif arg == "--sport" and i + 1 < len(sys.argv):
            sport_filters.append(sys.argv[i + 1].lower()); i += 2
        elif arg.startswith("0x"):
            address = arg; i += 1
        else:
            i += 1

    allowed_leagues = None
    if sport_filters:
        allowed_leagues = set()
        for sf in sport_filters:
            if sf in SPORT_ALIASES: allowed_leagues |= SPORT_ALIASES[sf]
            else: allowed_leagues.add(sf)

    all_legs = []

    if csv_path:
        print(f"Loading from CSV: {csv_path}")
        all_legs = load_legs_from_csv(csv_path)
        print(f"  → {len(all_legs)} legs from CSV")
    else:
        if CANNAE_CSV.exists() and not address:
            print(f"Loading historical CSV: {CANNAE_CSV}")
            csv_legs = load_legs_from_csv(str(CANNAE_CSV))
            print(f"  → {len(csv_legs)} legs from CSV")
            all_legs.extend(csv_legs)

        if address is None:
            address = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
            try:
                import yaml
                for path in [Path("config.yaml"), Path("/opt/bottie/config.yaml")]:
                    if path.exists():
                        cfg = yaml.safe_load(path.read_text())
                        for w in cfg.get("copy_trading", {}).get("watchlist", []):
                            if w.get("name", "").lower() == "cannae":
                                address = w["address"]; break
            except Exception: pass

        if HAS_HTTPX:
            print(f"Fetching from API: {address}")
            api_legs = load_legs_from_api(address)
            print(f"  → {len(api_legs)} legs from API")
            all_legs.extend(api_legs)

    if not all_legs:
        print("No data loaded."); sys.exit(1)

    # Sport filter
    if allowed_leagues:
        before = len(all_legs)
        all_legs = [l for l in all_legs if l["league"] in allowed_leagues]
        print(f"Sport filter {sport_filters}: {before} → {len(all_legs)} legs")

    # Dedup: same (cid, outcome) → prefer resolved over open
    seen = {}
    for l in all_legs:
        key = (l["cid"], l["outcome"])
        if key not in seen:
            seen[key] = l
        else:
            if seen[key]["result"] in ("OPEN", "UNKNOWN") and l["result"] in ("WIN", "LOSS"):
                seen[key] = l
    deduped = list(seen.values())
    print(f"\nTotal legs after dedup: {len(deduped)}")

    # Build game-level hauptbets
    records = build_game_hauptbets(deduped)
    print(f"Game line bets: {len(records)}")

    label = address[:10] + "..." + address[-6:] if address else "CSV"
    sport_label = ",".join(sport_filters) if sport_filters else None
    print_report(records, label, sport_label)


if __name__ == "__main__":
    main()
