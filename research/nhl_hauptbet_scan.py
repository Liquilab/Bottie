#!/usr/bin/env python3
"""
Scan top Polymarket sports traders for NHL hauptbet strategy viability.
1. Fetch top 50 sports leaderboard wallets
2. For each, fetch all positions (open + closed)
3. Filter NHL only (by eventSlug prefix "nhl-")
4. Group by event, identify hauptbet (largest position)
5. Calculate WR, ROI, PnL
6. Check directionality (not market maker)
"""

import json, time, sys
import urllib.request
from collections import defaultdict

HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

def g(url, retries=3):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read())
        except Exception as e:
            if i == retries:
                print(f"    FAIL: {url[:100]}... -> {e}", file=sys.stderr)
                return None
            time.sleep(0.5 * (i + 1))

def fetch_leaderboard():
    """Fetch top 50 sports traders from leaderboard API."""
    url = "https://data-api.polymarket.com/v1/leaderboard?category=SPORTS&timePeriod=ALL&orderBy=PNL&limit=50&offset=0"
    data = g(url)
    if not data:
        print("ERROR: Could not fetch leaderboard")
        sys.exit(1)
    wallets = []
    for entry in data:
        addr = entry.get("userAddress", entry.get("address", ""))
        name = entry.get("userName", entry.get("username", addr[:12]))
        pnl = float(entry.get("pnl", 0))
        vol = float(entry.get("volume", 0))
        wallets.append({"address": addr, "name": name, "pnl": pnl, "volume": vol})
    return wallets

def fetch_all_positions(addr):
    """Fetch all open positions, paginated."""
    all_pos = []
    offset = 0
    while True:
        url = f"https://data-api.polymarket.com/positions?user={addr}&limit=500&offset={offset}&sizeThreshold=0"
        data = g(url)
        if not data:
            break
        all_pos.extend(data)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.3)
    return all_pos

def fetch_all_closed(addr):
    """Fetch all closed positions, paginated."""
    all_pos = []
    offset = 0
    while True:
        url = f"https://data-api.polymarket.com/closed-positions?user={addr}&limit=500&sortBy=TIMESTAMP&sortOrder=DESC&offset={offset}"
        data = g(url)
        if not data:
            break
        all_pos.extend(data)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.3)
    return all_pos

def is_nhl(pos):
    """Check if position is NHL by eventSlug."""
    slug = (pos.get("eventSlug") or "").lower()
    return slug.startswith("nhl-")

def classify_result(cur_price):
    if cur_price is None:
        return "unknown"
    price = float(cur_price)
    if price >= 0.99:
        return "WIN"
    elif price <= 0.01:
        return "LOSS"
    return "ACTIVE"

def analyze_wallet_nhl(addr, name):
    """Analyze a single wallet for NHL hauptbet strategy."""
    open_pos = fetch_all_positions(addr)
    closed_pos = fetch_all_closed(addr)

    # Filter NHL
    nhl_positions = []

    for p in open_pos:
        if is_nhl(p):
            nhl_positions.append({
                "source": "open",
                "title": p.get("title", ""),
                "conditionId": p.get("conditionId", ""),
                "size": float(p.get("size", 0)),
                "cashPaid": float(p.get("initialValue", 0) or p.get("totalBought", 0) or 0),
                "avgPrice": float(p.get("avgPrice", 0) or 0),
                "curPrice": float(p.get("curPrice", 0) or 0),
                "outcome": p.get("outcome", ""),
                "eventSlug": p.get("eventSlug", ""),
                "cashPnl": float(p.get("cashPnl", 0) or 0),
            })

    for p in closed_pos:
        if is_nhl(p):
            size = float(p.get("totalBought", 0) or 0)
            avg = float(p.get("avgPrice", 0) or 0)
            nhl_positions.append({
                "source": "closed",
                "title": p.get("title", ""),
                "conditionId": p.get("conditionId", ""),
                "size": size,
                "cashPaid": avg * size if avg > 0 else size,
                "avgPrice": avg,
                "curPrice": float(p.get("curPrice", 0) or 0),
                "outcome": p.get("outcome", ""),
                "eventSlug": p.get("eventSlug", ""),
                "cashPnl": float(p.get("realizedPnl", 0) or 0),
            })

    if len(nhl_positions) < 5:
        return None  # Not enough NHL activity

    # Group by event
    events = defaultdict(list)
    for p in nhl_positions:
        key = p["eventSlug"]
        events[key].append(p)

    # Analyze hauptbet per event
    hb_wins = 0
    hb_losses = 0
    hb_active = 0
    hb_pnl = 0.0
    hb_invested = 0.0
    all_pnl = 0.0
    all_invested = 0.0
    both_sides_count = 0
    total_events = len(events)

    # Market type breakdown
    moneyline_events = 0
    spread_events = 0
    ou_events = 0

    for event_key, legs in events.items():
        legs_sorted = sorted(legs, key=lambda x: x["size"], reverse=True)
        hauptbet = legs_sorted[0]

        hb_result = classify_result(hauptbet["curPrice"])

        if hb_result == "WIN":
            hb_wins += 1
        elif hb_result == "LOSS":
            hb_losses += 1
        else:
            hb_active += 1

        hb_pnl += hauptbet["cashPnl"]
        hb_invested += hauptbet["cashPaid"]

        for leg in legs_sorted:
            all_pnl += leg["cashPnl"]
            all_invested += leg["cashPaid"]

        # Check both sides (market maker indicator)
        outcomes = set(leg["outcome"] for leg in legs_sorted)
        conditionIds = set(leg["conditionId"] for leg in legs_sorted)
        # If same conditionId has both outcomes → both sides
        for cid in conditionIds:
            cid_legs = [l for l in legs_sorted if l["conditionId"] == cid]
            cid_outcomes = set(l["outcome"] for l in cid_legs)
            if len(cid_outcomes) > 1:
                both_sides_count += 1
                break

        # Classify market type from title
        title = hauptbet["title"].lower()
        if "o/u" in title or "over" in title or "under" in title:
            ou_events += 1
        elif "spread" in title:
            spread_events += 1
        else:
            moneyline_events += 1

    resolved = hb_wins + hb_losses
    if resolved < 5:
        return None  # Not enough resolved NHL events

    wr = hb_wins / resolved * 100 if resolved > 0 else 0
    roi = hb_pnl / hb_invested * 100 if hb_invested > 0 else 0
    all_roi = all_pnl / all_invested * 100 if all_invested > 0 else 0
    both_sides_pct = both_sides_count / total_events * 100 if total_events > 0 else 0
    is_directional = both_sides_pct < 20  # Less than 20% both-side events

    avg_hb_size = hb_invested / total_events if total_events > 0 else 0

    return {
        "address": addr,
        "name": name,
        "total_nhl_positions": len(nhl_positions),
        "total_events": total_events,
        "resolved_events": resolved,
        "active_events": hb_active,
        "hb_wins": hb_wins,
        "hb_losses": hb_losses,
        "hb_wr": wr,
        "hb_pnl": hb_pnl,
        "hb_invested": hb_invested,
        "hb_roi": roi,
        "all_pnl": all_pnl,
        "all_invested": all_invested,
        "all_roi": all_roi,
        "both_sides_pct": both_sides_pct,
        "is_directional": is_directional,
        "avg_hb_size": avg_hb_size,
        "moneyline_events": moneyline_events,
        "spread_events": spread_events,
        "ou_events": ou_events,
    }

def main():
    print("=" * 100)
    print("  NHL HAUPTBET STRATEGY SCAN — Top 50 Sports Traders")
    print("=" * 100)

    print("\nFetching sports leaderboard...")
    wallets = fetch_leaderboard()
    print(f"Found {len(wallets)} wallets")

    results = []

    for i, w in enumerate(wallets):
        print(f"\n[{i+1}/{len(wallets)}] {w['name'][:30]} ({w['address'][:12]}...) — Sports PnL: ${w['pnl']:,.0f}")
        result = analyze_wallet_nhl(w["address"], w["name"])
        if result:
            results.append(result)
            print(f"  → NHL: {result['total_events']} events, {result['resolved_events']} resolved, "
                  f"HB WR: {result['hb_wr']:.0f}%, HB ROI: {result['hb_roi']:+.1f}%, "
                  f"Directional: {'YES' if result['is_directional'] else 'NO'}")
        else:
            print(f"  → Insufficient NHL data (<5 resolved events)")
        time.sleep(0.2)

    # Sort by ROI
    results.sort(key=lambda x: x["hb_roi"], reverse=True)

    # Print ranking table
    print("\n\n" + "=" * 140)
    print("  NHL HAUPTBET STRATEGY — RANKED BY ROI")
    print("=" * 140)
    print(f"{'#':>3} {'Name':<25} {'Address':<14} {'Events':>7} {'Resolved':>9} {'HB Wins':>8} {'HB WR':>6} {'HB PnL':>10} {'HB Inv':>10} {'HB ROI':>8} {'All ROI':>8} {'Dir?':>5} {'Avg Size':>9} {'ML/Sp/OU':>10}")
    print("-" * 140)

    for i, r in enumerate(results):
        dir_str = "YES" if r["is_directional"] else "NO"
        print(f"{i+1:>3} {r['name'][:24]:<25} {r['address'][:12]+'...':<14} "
              f"{r['total_events']:>7} {r['resolved_events']:>9} {r['hb_wins']:>8} "
              f"{r['hb_wr']:>5.0f}% ${r['hb_pnl']:>+9,.0f} ${r['hb_invested']:>9,.0f} "
              f"{r['hb_roi']:>+7.1f}% {r['all_roi']:>+7.1f}% {dir_str:>5} "
              f"${r['avg_hb_size']:>8,.0f} {r['moneyline_events']}/{r['spread_events']}/{r['ou_events']:>3}")

    # Filter: directional + profitable + 30+ events
    print("\n\n" + "=" * 140)
    print("  FILTERED: Directional + Profitable + 30+ Resolved NHL Events")
    print("=" * 140)

    filtered = [r for r in results if r["is_directional"] and r["hb_roi"] > 0 and r["resolved_events"] >= 30]
    filtered.sort(key=lambda x: x["hb_roi"], reverse=True)

    if not filtered:
        # Relax criteria
        print("  (Relaxing to 10+ resolved events)")
        filtered = [r for r in results if r["is_directional"] and r["hb_roi"] > 0 and r["resolved_events"] >= 10]
        filtered.sort(key=lambda x: x["hb_roi"], reverse=True)

    if not filtered:
        print("  (Relaxing to any directional + profitable)")
        filtered = [r for r in results if r["is_directional"] and r["hb_roi"] > 0]
        filtered.sort(key=lambda x: x["hb_roi"], reverse=True)

    for i, r in enumerate(filtered):
        print(f"\n--- #{i+1}: {r['name']} ---")
        print(f"  Address: {r['address']}")
        print(f"  NHL Events: {r['total_events']} total, {r['resolved_events']} resolved, {r['active_events']} active")
        print(f"  Hauptbet: W{r['hb_wins']}/L{r['hb_losses']} = {r['hb_wr']:.1f}% WR")
        print(f"  Hauptbet PnL: ${r['hb_pnl']:+,.2f} on ${r['hb_invested']:,.2f} invested = {r['hb_roi']:+.1f}% ROI")
        print(f"  All-legs PnL: ${r['all_pnl']:+,.2f} on ${r['all_invested']:,.2f} invested = {r['all_roi']:+.1f}% ROI")
        print(f"  Avg HB size: ${r['avg_hb_size']:,.2f}")
        print(f"  Directional: {'YES' if r['is_directional'] else 'NO'} (both-sides in {r['both_sides_pct']:.0f}% of events)")
        print(f"  Market types: {r['moneyline_events']} moneyline, {r['spread_events']} spread, {r['ou_events']} O/U")

    if not filtered:
        print("\n  NO WALLETS MATCH CRITERIA.")

    # Save raw results
    with open("/Users/koen/Projects/ Bottie/research/nhl_hauptbet_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n\nRaw results saved to research/nhl_hauptbet_results.json ({len(results)} wallets)")

if __name__ == "__main__":
    main()
