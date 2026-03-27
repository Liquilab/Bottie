#!/usr/bin/env python3
"""
Hauptbet Analysis — Per game line, per week: WR + ROI van de hoofdpositie.

Usage:
  python3 hauptbet_analysis.py                              # Cannae (all sports)
  python3 hauptbet_analysis.py 0xANY_WALLET_ADDRESS         # Any wallet
  python3 hauptbet_analysis.py --csv path/to/closed.csv     # From CSV only
  python3 hauptbet_analysis.py --sport nba                  # Filter sport
  python3 hauptbet_analysis.py --sport voetbal              # All football leagues
  python3 hauptbet_analysis.py --sport nhl --sport nba      # Multiple sports

Output: WR en ROI per game line (moneyline/spread/totals/btts/draw), per week.
        Wilson CI op WR, week-over-week trend.

Definitie "hauptbet": per conditionId, de SIDE (outcome) waar de trader de
meeste USDC op heeft gezet. De andere side is de hedge en telt NIET mee.
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
UA = {"User-Agent": "HauptbetAnalysis/1.0", "Accept": "application/json"}
CANNAE_CSV = Path(__file__).parent / "cannae_trades" / "cannae_closed_full.csv"

# Football leagues (for --sport voetbal filter)
FOOTBALL_LEAGUES = {
    "epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por",
    "bra", "itc", "ere", "es2", "bl2", "sea", "elc", "mex", "fr2", "aus",
    "spl", "efl", "tur", "uef", "ucl", "cdr", "acn", "cde", "ssc", "fif",
}

SPORT_ALIASES = {
    "voetbal": FOOTBALL_LEAGUES,
    "football": FOOTBALL_LEAGUES,
    "soccer": FOOTBALL_LEAGUES,
    "nba": {"nba"},
    "nhl": {"nhl"},
    "mlb": {"mlb"},
    "nfl": {"nfl"},
    "cbb": {"cbb"},
    "us": {"nba", "nhl", "mlb", "nfl", "cbb"},
}


# ── Classification ──────────────────────────────────────────────

def classify_game_line(title: str) -> str:
    t = (title or "").lower()
    if "spread" in t:
        return "spread"
    if "o/u " in t or "over/under" in t:
        return "totals"
    if "draw" in t or "end in a draw" in t:
        return "draw"
    if "both teams" in t or "btts" in t:
        return "btts"
    return "moneyline"


def detect_league(slug: str) -> str:
    return slug.split("-")[0] if slug else "?"


def iso_week(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"


def wilson_ci(wins: int, total: int, z=1.96) -> tuple:
    """Wilson score 95% confidence interval."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (round(max(0, center - spread), 4), round(min(1, center + spread), 4))


# ── Data Loading ────────────────────────────────────────────────

def load_from_csv(csv_path: str) -> list:
    positions = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            cid = row.get("condition_id", "")
            if not cid:
                continue
            cost = float(row.get("total_bought", 0) or 0)
            if cost <= 0:
                continue

            won_raw = row.get("won", "")
            rpnl = float(row.get("realized_pnl", 0) or 0)
            cur_price = float(row.get("cur_price", 0) or 0)

            if won_raw == "1" or cur_price == 1.0:
                result = "WIN"
            elif won_raw == "0" or cur_price == 0.0:
                result = "LOSS"
            else:
                continue

            if rpnl != 0:
                pnl = rpnl
            elif result == "WIN":
                avg_price = float(row.get("avg_price", 0) or 0)
                shares = cost / avg_price if avg_price > 0 else 0
                pnl = shares - cost
            else:
                pnl = -cost

            ts = int(row.get("timestamp", 0) or 0)
            slug = (row.get("event_slug", "") or "").split("-more-markets")[0]

            positions.append({
                "cid": cid,
                "outcome": row.get("outcome", ""),
                "title": row.get("title", ""),
                "event_slug": slug,
                "game_line": classify_game_line(row.get("title", "")),
                "league": detect_league(slug),
                "cost": cost,
                "result": result,
                "pnl": pnl,
                "first_ts": ts,
            })
    return positions


def load_from_api(address: str) -> list:
    if not HAS_HTTPX:
        print("ERROR: httpx not installed. Use --csv or pip install httpx")
        sys.exit(1)

    client = httpx.Client(headers=UA)

    def fetch_paginated(url_template, max_offset=10000):
        results = []
        offset = 0
        while offset < max_offset:
            url = url_template.format(offset=offset)
            try:
                resp = client.get(url, timeout=30)
                resp.raise_for_status()
                batch = resp.json()
            except Exception as e:
                print(f"  Stop at offset {offset}: {e}")
                break
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
        return results

    print("  Fetching trades...")
    trades = fetch_paginated(f"{API}/activity?user={address}&type=trade&limit=500&offset={{offset}}")
    print(f"  → {len(trades)} trades")

    print("  Fetching redeems...")
    redeems = fetch_paginated(f"{API}/activity?user={address}&type=redeem&limit=500&offset={{offset}}")
    print(f"  → {len(redeems)} redeems")

    print("  Fetching positions...")
    positions = fetch_paginated(f"{API}/positions?user={address}&limit=500&offset={{offset}}&sizeThreshold=0")
    print(f"  → {len(positions)} positions")

    # Redeems per conditionId (total USDC redeemed — the payout)
    redeem_by_cid = defaultdict(float)
    for r in redeems:
        cid = r.get("conditionId", "")
        usdc = float(r.get("usdcSize", 0) or 0)
        if cid:
            redeem_by_cid[cid] += usdc

    # Open positions (for OPEN detection)
    open_cids = set()
    for p in positions:
        cid = p.get("conditionId", "") or ""
        size = float(p.get("size", 0) or 0)
        cur = float(p.get("curPrice", 0) or 0)
        if cid and size > 0.01 and 0.02 < cur < 0.98:
            open_cids.add(cid)

    # Group trades by (conditionId, outcome)
    buys = defaultdict(list)
    sells = defaultdict(list)
    for t in trades:
        cid = t.get("conditionId", "")
        outcome = t.get("outcome", "") or ""
        side = t.get("side", "")
        if cid:
            if side == "BUY":
                buys[(cid, outcome)].append(t)
            elif side == "SELL":
                sells[(cid, outcome)].append(t)

    # Build per-conditionId data: all sides, costs, returns
    cid_sides = defaultdict(dict)
    for (cid, outcome), tlist in buys.items():
        buy_cost = sum(float(x.get("usdcSize", 0) or 0) for x in tlist)
        sl = sells.get((cid, outcome), [])
        sell_proceeds = sum(float(s.get("usdcSize", 0) or 0) for s in sl)
        t0 = tlist[0]
        title = t0.get("title", "") or ""
        slug = t0.get("eventSlug", "") or t0.get("slug", "") or ""
        event_slug = slug.split("-more-markets")[0] if slug else ""
        timestamps = [int(x.get("timestamp", 0) or 0) for x in tlist]
        first_ts = min(timestamps) if timestamps else 0
        cid_sides[cid][outcome] = {
            "buy_cost": buy_cost,
            "sell_proceeds": sell_proceeds,
            "title": title,
            "event_slug": event_slug,
            "first_ts": first_ts,
        }

    # Per conditionId: total returns (sells + redeems) vs total costs → PnL
    # Attribute to hauptbet side (largest buy_cost)
    positions_list = []
    for cid, sides in cid_sides.items():
        total_buy = sum(d["buy_cost"] for d in sides.values())
        total_sell = sum(d["sell_proceeds"] for d in sides.values())
        total_redeem = redeem_by_cid.get(cid, 0)
        total_returns = total_sell + total_redeem
        pnl = total_returns - total_buy

        has_redeem = cid in redeem_by_cid
        all_sold = total_sell > total_buy * 0.5 and not any(
            cid in open_cids for _ in [1]
        )
        resolved = has_redeem or all_sold

        if not resolved and cid in open_cids:
            # Still open — add each side as OPEN
            for outcome, d in sides.items():
                positions_list.append({
                    "cid": cid, "outcome": outcome, "title": d["title"],
                    "event_slug": d["event_slug"],
                    "game_line": classify_game_line(d["title"]),
                    "league": detect_league(d["event_slug"]),
                    "cost": d["buy_cost"], "result": "OPEN", "pnl": 0,
                    "first_ts": d["first_ts"],
                })
            continue

        if not resolved:
            # Unknown — skip
            continue

        # Resolved: attribute to hauptbet (largest buy_cost side)
        hauptbet_outcome = max(sides.keys(), key=lambda o: sides[o]["buy_cost"])
        h = sides[hauptbet_outcome]
        result = "WIN" if pnl > 0 else "LOSS"

        positions_list.append({
            "cid": cid, "outcome": hauptbet_outcome, "title": h["title"],
            "event_slug": h["event_slug"],
            "game_line": classify_game_line(h["title"]),
            "league": detect_league(h["event_slug"]),
            "cost": round(total_buy, 2), "result": result,
            "pnl": round(pnl, 2), "first_ts": h["first_ts"],
        })

    client.close()
    return positions_list


# ── Hauptbet Selection ──────────────────────────────────────────

def select_hauptbets(positions: list) -> tuple:
    """Per conditionId: merge all sides, compute PnL as returns - costs.

    CSV data comes as individual (cid, outcome) rows with realized_pnl.
    API data comes pre-merged per conditionId.
    This function handles both: groups by cid, sums costs and PnL,
    attributes to the hauptbet (largest cost side).
    """
    by_cid = defaultdict(list)
    for p in positions:
        by_cid[p["cid"]].append(p)

    hauptbets = []
    for cid, sides in by_cid.items():
        # Total cost and PnL across all sides of this conditionId
        total_cost = sum(s["cost"] for s in sides)
        total_pnl = sum(s["pnl"] for s in sides)

        # Hauptbet = side with largest cost
        sides.sort(key=lambda s: -s["cost"])
        haupt = sides[0].copy()

        # Override with conditionId-level totals
        haupt["cost"] = total_cost
        haupt["pnl"] = total_pnl
        haupt["hedge_cost"] = sum(s["cost"] for s in sides[1:])
        haupt["n_sides"] = len(sides)

        # Determine result from PnL (not from individual side labels)
        if any(s["result"] == "OPEN" for s in sides):
            haupt["result"] = "OPEN"
            haupt["pnl"] = 0
        elif total_pnl > 0:
            haupt["result"] = "WIN"
        else:
            haupt["result"] = "LOSS"

        hauptbets.append(haupt)

    resolved = [h for h in hauptbets if h["result"] in ("WIN", "LOSS")]
    open_bets = [h for h in hauptbets if h["result"] == "OPEN"]
    return resolved, open_bets


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
    """Simple trend: compare first half avg vs second half avg."""
    if len(weekly_wrs) < 4:
        return "  "
    mid = len(weekly_wrs) // 2
    first = sum(weekly_wrs[:mid]) / mid
    second = sum(weekly_wrs[mid:]) / (len(weekly_wrs) - mid)
    diff = second - first
    if diff > 0.05:
        return "↗"
    elif diff < -0.05:
        return "↘"
    return "→"


# ── Report ──────────────────────────────────────────────────────

def print_report(resolved, open_bets, label, sport_filter=None):
    by_line_week = defaultdict(lambda: defaultdict(list))
    by_line_total = defaultdict(list)

    for h in resolved:
        gl = h["game_line"]
        week = iso_week(h["first_ts"]) if h["first_ts"] else "unknown"
        by_line_week[gl][week].append(h)
        by_line_total[gl].append(h)

    filter_label = f" | filter: {sport_filter}" if sport_filter else ""

    print()
    print("=" * 95)
    print(f"HAUPTBET ANALYSE — {label}{filter_label}")
    print(f"Resolved: {len(resolved)} game lines | Open: {len(open_bets)}")
    print("=" * 95)
    print()
    print("Hauptbet = de SIDE met de meeste USDC per game line (conditionId).")
    print("Hedge-sides tellen NIET mee in WR/ROI.")
    print()

    # Overall per game line
    print(f"{'Game Line':<12} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'CI 95%':>13} {'ROI':>7} {'PnL':>12} {'Trend':>5}")
    print("-" * 82)

    game_lines = ["moneyline", "spread", "totals", "draw", "btts"]
    for gl in game_lines:
        if gl not in by_line_total:
            continue
        s = stats(by_line_total[gl])
        # Compute trend from weekly WRs
        weekly_wrs = []
        for week in sorted(by_line_week[gl].keys()):
            ws = stats(by_line_week[gl][week])
            if ws["n"] >= 3:
                weekly_wrs.append(ws["wr"])
        tr = trend_arrow(weekly_wrs)
        print(f"{gl:<12} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}    {tr}")

    s = stats(resolved)
    print("-" * 82)
    print(f"{'TOTAAL':<12} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")

    # Per week breakdown per game line
    all_weeks = sorted(set(
        week for gl_weeks in by_line_week.values() for week in gl_weeks.keys()
    ))

    for gl in game_lines:
        if gl not in by_line_week:
            continue
        print()
        print(f"--- {gl.upper()} per week ---")
        print(f"  {'Week':<10} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'CI 95%':>13} {'ROI':>7} {'PnL':>12}")

        for week in all_weeks:
            if week not in by_line_week[gl]:
                continue
            s = stats(by_line_week[gl][week])
            print(f"  {week:<10} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")

    # Per league
    by_league = defaultdict(list)
    for h in resolved:
        by_league[h["league"]].append(h)

    print()
    print("--- PER LEAGUE ---")
    print(f"{'League':<10} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'CI 95%':>13} {'ROI':>7} {'PnL':>12}")
    print("-" * 68)
    for league, bets in sorted(by_league.items(), key=lambda x: -sum(b["pnl"] for b in x[1])):
        s = stats(bets)
        print(f"{league:<10} {s['n']:>5} {s['w']:>5} {s['l']:>5} {s['wr']:>5.0%} [{s['ci'][0]:.0%}-{s['ci'][1]:.0%}] {s['roi']:>6.0%} ${s['pnl']:>11,.0f}")

    # Per league × game line
    print()
    print("--- LEAGUE x GAME LINE ---")
    print(f"{'League':<10} {'Line':<12} {'N':>5} {'W':>5} {'L':>5} {'WR':>6} {'ROI':>7} {'PnL':>12}")
    print("-" * 72)
    for league, bets in sorted(by_league.items(), key=lambda x: -sum(b["pnl"] for b in x[1])):
        by_gl = defaultdict(list)
        for b in bets:
            by_gl[b["game_line"]].append(b)
        for gl in game_lines:
            if gl not in by_gl:
                continue
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
            csv_path = sys.argv[i + 1]
            i += 2
        elif arg == "--sport" and i + 1 < len(sys.argv):
            sport_filters.append(sys.argv[i + 1].lower())
            i += 2
        elif arg.startswith("0x"):
            address = arg
            i += 1
        else:
            i += 1

    # Resolve sport filter to league set
    allowed_leagues = None
    if sport_filters:
        allowed_leagues = set()
        for sf in sport_filters:
            if sf in SPORT_ALIASES:
                allowed_leagues |= SPORT_ALIASES[sf]
            else:
                allowed_leagues.add(sf)

    all_positions = []

    if csv_path:
        print(f"Loading from CSV: {csv_path}")
        all_positions = load_from_csv(csv_path)
        print(f"  → {len(all_positions)} positions from CSV")
    else:
        if CANNAE_CSV.exists() and not address:
            print(f"Loading historical CSV: {CANNAE_CSV}")
            csv_positions = load_from_csv(str(CANNAE_CSV))
            print(f"  → {len(csv_positions)} positions from CSV")
            all_positions.extend(csv_positions)

        if address is None:
            address = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
            try:
                import yaml
                for path in [Path("config.yaml"), Path("/opt/bottie/config.yaml")]:
                    if path.exists():
                        cfg = yaml.safe_load(path.read_text())
                        for w in cfg.get("copy_trading", {}).get("watchlist", []):
                            if w.get("name", "").lower() == "cannae":
                                address = w["address"]
                                break
            except Exception:
                pass

        if HAS_HTTPX:
            print(f"Fetching from API: {address}")
            api_positions = load_from_api(address)
            print(f"  → {len(api_positions)} positions from API")
            all_positions.extend(api_positions)

    if not all_positions:
        print("No data loaded.")
        sys.exit(1)

    # Apply sport filter
    if allowed_leagues:
        before = len(all_positions)
        all_positions = [p for p in all_positions if p["league"] in allowed_leagues]
        print(f"Sport filter {sport_filters}: {before} → {len(all_positions)} positions")

    # Dedup
    seen = {}
    for p in all_positions:
        key = (p["cid"], p["outcome"])
        if key not in seen:
            seen[key] = p
        else:
            existing = seen[key]
            if existing["result"] in ("OPEN", "UNKNOWN") and p["result"] in ("WIN", "LOSS"):
                seen[key] = p

    deduped = list(seen.values())
    print(f"\nTotal positions after dedup: {len(deduped)}")

    resolved, open_bets = select_hauptbets(deduped)

    label = address[:10] + "..." + address[-6:] if address else "CSV"
    sport_label = ",".join(sport_filters) if sport_filters else None
    print_report(resolved, open_bets, label, sport_label)


if __name__ == "__main__":
    main()
