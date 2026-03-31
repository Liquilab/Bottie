#!/usr/bin/env python3
"""
Analyse F3 hauptbet strategy for Tennis and MLB.
Fetches all positions (open + closed), groups by event, identifies hauptbet.
"""

import requests
import json
import time
from collections import defaultdict

USER = "0x01c78F8873C0C86D6B6b92ff627E3802237EE995"
HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

def fetch_all_positions():
    """Fetch all open positions, paginated."""
    all_pos = []
    offset = 0
    while True:
        url = f"https://data-api.polymarket.com/positions?user={USER}&limit=500&offset={offset}&sizeThreshold=0"
        resp = requests.get(url, headers=HEADERS)
        data = resp.json()
        if not data:
            break
        all_pos.extend(data)
        print(f"  Open positions: fetched {len(data)} at offset {offset}, total {len(all_pos)}")
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.3)
    return all_pos

def fetch_all_closed():
    """Fetch all closed positions, paginated."""
    all_pos = []
    offset = 0
    while True:
        url = f"https://data-api.polymarket.com/closed-positions?user={USER}&limit=500&sortBy=TIMESTAMP&sortOrder=DESC&offset={offset}"
        resp = requests.get(url, headers=HEADERS)
        data = resp.json()
        if not data:
            break
        all_pos.extend(data)
        print(f"  Closed positions: fetched {len(data)} at offset {offset}, total {len(all_pos)}")
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.3)
    return all_pos

def classify_sport(event_slug):
    """Classify sport by eventSlug prefix — the only reliable method."""
    if not event_slug:
        return None
    slug = event_slug.lower()
    if slug.startswith("atp-") or slug.startswith("wta-"):
        return "Tennis"
    elif slug.startswith("mlb-"):
        return "MLB"
    return None

def classify_result(cur_price):
    """Classify position result."""
    if cur_price is None:
        return "unknown"
    price = float(cur_price)
    if price >= 0.99:
        return "WIN"
    elif price <= 0.01:
        return "LOSS"
    else:
        return "ACTIVE"

def extract_event_key(pos):
    """Extract event grouping key from position."""
    # Use eventSlug if available, otherwise derive from title
    event_slug = pos.get("eventSlug", "")
    if event_slug:
        return event_slug
    # Fallback: use title root
    title = pos.get("title", "")
    return title

def process_positions(open_positions, closed_positions):
    """Process and filter Tennis/MLB positions."""
    all_positions = []

    # Process open positions
    for p in open_positions:
        title = p.get("title", "")
        sport = classify_sport(p.get("eventSlug", ""))

        if sport:
            all_positions.append({
                "source": "open",
                "sport": sport,
                "title": title,
                "conditionId": p.get("conditionId", ""),
                "size": float(p.get("size", 0)),
                "cashPaid": float(p.get("initialValue", 0) or 0),
                "avgPrice": float(p.get("avgPrice", 0) or 0),
                "curPrice": float(p.get("curPrice", 0) or 0),
                "outcome": p.get("outcome", ""),
                "eventSlug": p.get("eventSlug", ""),
                "cashPnl": float(p.get("cashPnl", 0) or 0),
                "endDate": p.get("endDate", ""),
                "raw": p
            })

    # Process closed positions
    for p in closed_positions:
        title = p.get("title", "")
        sport = classify_sport(p.get("eventSlug", ""))

        if sport:
            size = float(p.get("totalBought", 0) or 0)
            avg = float(p.get("avgPrice", 0) or 0)
            cash_paid = avg * size
            all_positions.append({
                "source": "closed",
                "sport": sport,
                "title": title,
                "conditionId": p.get("conditionId", ""),
                "size": size,
                "cashPaid": cash_paid,
                "avgPrice": avg,
                "curPrice": float(p.get("curPrice", 0) or 0),
                "outcome": p.get("outcome", ""),
                "eventSlug": p.get("eventSlug", ""),
                "cashPnl": float(p.get("realizedPnl", 0) or 0),
                "endDate": p.get("endDate", ""),
                "raw": p
            })

    return all_positions

def group_by_event(positions):
    """Group positions by event."""
    events = defaultdict(list)
    for p in positions:
        key = p["eventSlug"] if p["eventSlug"] else p["title"]
        events[key].append(p)
    return events

def analyze_events(events, sport_name):
    """Analyze hauptbet strategy for grouped events."""
    print(f"\n{'='*120}")
    print(f"  {sport_name} — HAUPTBET ANALYSIS")
    print(f"{'='*120}")

    total_hauptbet_wins = 0
    total_hauptbet_losses = 0
    total_hauptbet_active = 0
    total_hauptbet_pnl = 0.0
    total_hauptbet_invested = 0.0
    total_all_pnl = 0.0
    total_all_invested = 0.0

    event_rows = []

    for event_key, legs in sorted(events.items()):
        # Sort legs by size (largest first) to find hauptbet
        legs_sorted = sorted(legs, key=lambda x: x["size"], reverse=True)
        hauptbet = legs_sorted[0]
        other_legs = legs_sorted[1:]

        # Hauptbet result
        hb_result = classify_result(hauptbet["curPrice"])
        hb_pnl = hauptbet["cashPnl"]

        # All legs PnL
        all_legs_pnl = 0.0
        all_legs_invested = 0.0
        for leg in legs_sorted:
            all_legs_pnl += leg["cashPnl"]
            all_legs_invested += leg["cashPaid"]

        # Track stats
        if hb_result == "WIN":
            total_hauptbet_wins += 1
        elif hb_result == "LOSS":
            total_hauptbet_losses += 1
        else:
            total_hauptbet_active += 1

        total_hauptbet_pnl += hb_pnl
        total_hauptbet_invested += hauptbet["cashPaid"]
        total_all_pnl += all_legs_pnl
        total_all_invested += all_legs_invested

        # Check if both sides of same market
        outcomes = set(leg["outcome"] for leg in legs_sorted)
        both_sides = len(outcomes) > 1 and len(legs_sorted) > 1

        # Size ratio if both sides
        size_ratio = ""
        if len(legs_sorted) > 1:
            size_ratio = f"{hauptbet['size']:.0f} vs {sum(l['size'] for l in other_legs):.0f}"

        other_legs_str = ""
        for ol in other_legs:
            ol_result = classify_result(ol["curPrice"])
            other_legs_str += f"  {ol['outcome']} sz={ol['size']:.0f} @{ol['avgPrice']:.2f} → {ol_result}"

        event_rows.append({
            "event": event_key[:60],
            "n_legs": len(legs_sorted),
            "hb_outcome": hauptbet["outcome"],
            "hb_size": hauptbet["size"],
            "hb_avg_price": hauptbet["avgPrice"],
            "hb_cash": hauptbet["cashPaid"],
            "hb_result": hb_result,
            "hb_pnl": hb_pnl,
            "all_pnl": all_legs_pnl,
            "both_sides": both_sides,
            "size_ratio": size_ratio,
            "other_legs": other_legs_str,
            "title": hauptbet["title"],
        })

    # Print per-event table
    print(f"\n{'Event':<62} {'Legs':>4} {'Hauptbet Side':<25} {'Size':>8} {'AvgPx':>6} {'Cash':>8} {'Result':>7} {'HB PnL':>9} {'All PnL':>9} {'Both?':>5} {'Ratio':<20}")
    print("-" * 180)

    for r in event_rows:
        print(f"{r['event']:<62} {r['n_legs']:>4} {r['hb_outcome']:<25} {r['hb_size']:>8.0f} {r['hb_avg_price']:>6.2f} {r['hb_cash']:>8.1f} {r['hb_result']:>7} {r['hb_pnl']:>9.1f} {r['all_pnl']:>9.1f} {'YES' if r['both_sides'] else 'NO':>5} {r['size_ratio']:<20}")
        if r["other_legs"]:
            print(f"  └ Other legs:{r['other_legs']}")

    # Summary
    resolved = total_hauptbet_wins + total_hauptbet_losses
    print(f"\n--- {sport_name} SUMMARY ---")
    print(f"Total events: {len(event_rows)}")
    print(f"Resolved events: {resolved}")
    print(f"Active events: {total_hauptbet_active}")
    print(f"Hauptbet wins: {total_hauptbet_wins}")
    print(f"Hauptbet losses: {total_hauptbet_losses}")
    if resolved > 0:
        print(f"Hauptbet WR: {total_hauptbet_wins/resolved*100:.1f}%")
    print(f"Hauptbet total PnL: ${total_hauptbet_pnl:.2f}")
    print(f"Hauptbet total invested: ${total_hauptbet_invested:.2f}")
    if total_hauptbet_invested > 0:
        print(f"Hauptbet ROI: {total_hauptbet_pnl/total_hauptbet_invested*100:.1f}%")
    print(f"All-legs total PnL: ${total_all_pnl:.2f}")
    print(f"All-legs total invested: ${total_all_invested:.2f}")
    if total_all_invested > 0:
        print(f"All-legs ROI: {total_all_pnl/total_all_invested*100:.1f}%")

    return event_rows

def main():
    print("Fetching F3 open positions...")
    open_pos = fetch_all_positions()
    print(f"Total open positions: {len(open_pos)}")

    print("\nFetching F3 closed positions...")
    closed_pos = fetch_all_closed()
    print(f"Total closed positions: {len(closed_pos)}")

    # Dump raw data for inspection
    with open("/Users/koen/Projects/ Bottie/research/f3_raw_open.json", "w") as f:
        json.dump(open_pos, f, indent=2)
    with open("/Users/koen/Projects/ Bottie/research/f3_raw_closed.json", "w") as f:
        json.dump(closed_pos, f, indent=2)
    print("\nRaw data saved to research/f3_raw_open.json and f3_raw_closed.json")

    # Print ALL titles to check sport classification
    print("\n--- ALL POSITION TITLES (for classification check) ---")
    seen = set()
    for p in open_pos + closed_pos:
        slug = p.get("eventSlug", "")
        title = p.get("title", "")
        key = (slug, title)
        if key in seen:
            continue
        seen.add(key)
        sport = classify_sport(slug)
        if sport:
            print(f"  [{sport}] {slug} -> {title}")

    # Process
    all_filtered = process_positions(open_pos, closed_pos)
    print(f"\nFiltered Tennis+MLB positions: {len(all_filtered)}")

    tennis_pos = [p for p in all_filtered if p["sport"] == "Tennis"]
    mlb_pos = [p for p in all_filtered if p["sport"] == "MLB"]

    print(f"  Tennis: {len(tennis_pos)}")
    print(f"  MLB: {len(mlb_pos)}")

    if tennis_pos:
        tennis_events = group_by_event(tennis_pos)
        analyze_events(tennis_events, "TENNIS")
    else:
        print("\nNo Tennis positions found for F3.")

    if mlb_pos:
        mlb_events = group_by_event(mlb_pos)
        analyze_events(mlb_events, "MLB")
    else:
        print("\nNo MLB positions found for F3.")

if __name__ == "__main__":
    main()
