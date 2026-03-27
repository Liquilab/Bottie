#!/usr/bin/env python3
"""
Hauptbet Analysis — Per game line, per week: WR + ROI van de hoofdpositie.

Usage:
  python3 hauptbet_analysis.py                          # Cannae (from config)
  python3 hauptbet_analysis.py 0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b
  python3 hauptbet_analysis.py 0xANY_WALLET_ADDRESS

Output: WR en ROI per game line (moneyline/spread/totals/btts/draw), per week.

Definitie "hauptbet": per game, per game line (conditionId), de SIDE (outcome)
waar de trader de meeste USDC op heeft gezet. De andere side is de hedge.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

API = "https://data-api.polymarket.com"
UA = {"User-Agent": "HauptbetAnalysis/1.0", "Accept": "application/json"}


def fetch_all_activity(client, address, atype, max_offset=10000):
    """Paginated fetch of activity (trade/redeem)."""
    results = []
    offset = 0
    while offset < max_offset:
        url = f"{API}/activity?user={address}&type={atype}&limit=500&offset={offset}"
        try:
            resp = client.get(url, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"  Stop {atype} at offset {offset}: {e}")
            break
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return results


def fetch_all_positions(client, address, max_offset=10000):
    """Paginated fetch of positions."""
    results = []
    offset = 0
    while offset < max_offset:
        url = f"{API}/positions?user={address}&limit=500&offset={offset}&sizeThreshold=0"
        try:
            resp = client.get(url, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"  Stop positions at offset {offset}: {e}")
            break
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return results


def classify_game_line(title: str) -> str:
    """Classify market title into game line type."""
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
    """Timestamp → 'YYYY-Www' ISO week string."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"


def run(address: str):
    print(f"Analyzing wallet: {address}")
    print(f"Fetching data from Polymarket API...")

    client = httpx.Client(headers=UA)

    # 1. Fetch trades + redeems + positions
    print("  Fetching trades...")
    trades = fetch_all_activity(client, address, "trade")
    print(f"  → {len(trades)} trades")

    print("  Fetching redeems...")
    redeems = fetch_all_activity(client, address, "redeem")
    print(f"  → {len(redeems)} redeems")

    print("  Fetching positions...")
    positions = fetch_all_positions(client, address)
    print(f"  → {len(positions)} positions")

    # 2. Determine resolved status per conditionId
    redeemed_cids = {r.get("conditionId", "") for r in redeems if r.get("conditionId")}

    loser_cids = set()
    open_cids = set()
    for p in positions:
        cid = p.get("conditionId", "") or ""
        if not cid:
            continue
        size = float(p.get("size", 0) or 0)
        if size < 0.01:
            continue
        cur = float(p.get("curPrice", 0) or 0)
        if cur <= 0.05:
            loser_cids.add(cid)
        elif cur < 0.95:
            open_cids.add(cid)

    # 3. Group BUY trades by conditionId + outcome
    #    Key: (conditionId, outcome) → list of trades
    buys = defaultdict(list)
    for t in trades:
        if t.get("side") != "BUY":
            continue
        cid = t.get("conditionId", "")
        outcome = t.get("outcome", "") or ""
        if cid:
            buys[(cid, outcome)].append(t)

    # 4. Build positions per conditionId (both sides)
    #    Then determine hauptbet = side with most USDC
    positions_by_cid = defaultdict(list)
    for (cid, outcome), tlist in buys.items():
        cost = sum(float(x.get("usdcSize", 0) or 0) for x in tlist)
        shares = sum(float(x.get("size", 0) or 0) for x in tlist)
        t0 = tlist[0]
        title = t0.get("title", "") or ""
        slug = t0.get("eventSlug", "") or t0.get("slug", "") or ""
        event_slug = slug.split("-more-markets")[0] if slug else ""
        timestamps = [int(x.get("timestamp", 0) or 0) for x in tlist]
        first_ts = min(timestamps) if timestamps else 0

        # Resolve status
        if cid in redeemed_cids:
            # Need to check if THIS outcome was redeemed
            # Redeems don't always have outcome, so check if this cid redeemed
            # and this side won (shares returned > cost = WIN)
            result = "WIN"
            pnl = shares - cost
        elif cid in loser_cids:
            result = "LOSS"
            pnl = -cost
        elif cid in open_cids:
            result = "OPEN"
            pnl = 0
        else:
            result = "UNKNOWN"
            pnl = 0

        positions_by_cid[cid].append({
            "cid": cid,
            "outcome": outcome,
            "title": title,
            "event_slug": event_slug,
            "game_line": classify_game_line(title),
            "league": detect_league(event_slug),
            "cost": cost,
            "shares": shares,
            "result": result,
            "pnl": pnl,
            "first_ts": first_ts,
        })

    # 5. Per conditionId: determine hauptbet (largest cost side)
    hauptbets = []
    for cid, sides in positions_by_cid.items():
        # Sort by cost descending — first = hauptbet
        sides.sort(key=lambda s: -s["cost"])
        haupt = sides[0]
        hedge_cost = sum(s["cost"] for s in sides[1:])

        # For the hauptbet result: the redeemed_cids tells us the conditionId resolved.
        # But which SIDE won? If the hauptbet side was redeemed, it's a WIN.
        # If the conditionId was redeemed but our hauptbet side lost, we need to check.
        # Simplification: if cid redeemed AND hauptbet has positive PnL → WIN
        # If cid redeemed AND hauptbet has negative PnL → the OTHER side won → LOSS for hauptbet
        if haupt["result"] == "WIN" and haupt["pnl"] < 0:
            # Redeemed but this side lost (hedge won instead)
            haupt["result"] = "LOSS"
            haupt["pnl"] = -haupt["cost"]

        hauptbets.append({
            **haupt,
            "hedge_cost": hedge_cost,
            "total_game_line_cost": haupt["cost"] + hedge_cost,
            "n_sides": len(sides),
        })

    # Filter to resolved only
    resolved = [h for h in hauptbets if h["result"] in ("WIN", "LOSS")]
    open_bets = [h for h in hauptbets if h["result"] == "OPEN"]

    print(f"\nTotal conditionIds: {len(hauptbets)}")
    print(f"Resolved: {len(resolved)} | Open: {len(open_bets)}")

    if not resolved:
        print("No resolved bets found.")
        return

    # 6. Aggregate: per game line, per week
    # Structure: game_line → week → list of hauptbets
    by_line_week = defaultdict(lambda: defaultdict(list))
    by_line_total = defaultdict(list)

    for h in resolved:
        gl = h["game_line"]
        week = iso_week(h["first_ts"]) if h["first_ts"] else "unknown"
        by_line_week[gl][week].append(h)
        by_line_total[gl].append(h)

    # 7. Print report
    def stats(bets):
        n = len(bets)
        wins = sum(1 for b in bets if b["result"] == "WIN")
        losses = n - wins
        cost = sum(b["cost"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)
        wr = wins / n if n else 0
        roi = pnl / cost if cost > 0 else 0
        return n, wins, losses, wr, roi, cost, pnl

    print()
    print("=" * 90)
    print(f"HAUPTBET ANALYSE — {address[:10]}...{address[-6:]}")
    print(f"Resolved: {len(resolved)} game lines | Open: {len(open_bets)}")
    print("=" * 90)
    print()
    print("Definitie: hauptbet = de SIDE met de meeste USDC per game line (conditionId).")
    print("           Hedge-sides worden niet meegeteld in WR/ROI.")
    print()

    # Overall per game line
    print(f"{'Game Line':<12} {'N':>4} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8} {'Cost':>10} {'PnL':>10}")
    print("-" * 70)

    for gl in ["moneyline", "spread", "totals", "draw", "btts"]:
        if gl not in by_line_total:
            continue
        n, w, l, wr, roi, cost, pnl = stats(by_line_total[gl])
        print(f"{gl:<12} {n:>4} {w:>4} {l:>4} {wr:>6.0%} {roi:>7.0%} ${cost:>9,.0f} ${pnl:>9,.0f}")

    # Grand total
    n, w, l, wr, roi, cost, pnl = stats(resolved)
    print("-" * 70)
    print(f"{'TOTAAL':<12} {n:>4} {w:>4} {l:>4} {wr:>6.0%} {roi:>7.0%} ${cost:>9,.0f} ${pnl:>9,.0f}")

    # Per week breakdown
    all_weeks = sorted(set(
        week for gl_weeks in by_line_week.values() for week in gl_weeks.keys()
    ))

    for gl in ["moneyline", "spread", "totals", "draw", "btts"]:
        if gl not in by_line_week:
            continue
        print()
        print(f"--- {gl.upper()} per week ---")
        print(f"  {'Week':<10} {'N':>4} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8} {'Cost':>10} {'PnL':>10}")

        for week in all_weeks:
            if week not in by_line_week[gl]:
                continue
            n, w, l, wr, roi, cost, pnl = stats(by_line_week[gl][week])
            print(f"  {week:<10} {n:>4} {w:>4} {l:>4} {wr:>6.0%} {roi:>7.0%} ${cost:>9,.0f} ${pnl:>9,.0f}")

    # Per league summary
    by_league = defaultdict(list)
    for h in resolved:
        by_league[h["league"]].append(h)

    print()
    print("--- PER LEAGUE ---")
    print(f"{'League':<10} {'N':>4} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8} {'PnL':>10}")
    print("-" * 55)
    for league, bets in sorted(by_league.items(), key=lambda x: -sum(b["pnl"] for b in x[1])):
        n, w, l, wr, roi, cost, pnl = stats(bets)
        print(f"{league:<10} {n:>4} {w:>4} {l:>4} {wr:>6.0%} {roi:>7.0%} ${pnl:>9,.0f}")

    client.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("0x"):
        addr = sys.argv[1]
    else:
        # Default: Cannae
        addr = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
        # Try config
        try:
            import yaml
            for path in [Path("config.yaml"), Path("/opt/bottie/config.yaml")]:
                if path.exists():
                    cfg = yaml.safe_load(path.read_text())
                    for w in cfg.get("copy_trading", {}).get("watchlist", []):
                        if w.get("name", "").lower() == "cannae":
                            addr = w["address"]
                            break
        except Exception:
            pass

    run(addr)
