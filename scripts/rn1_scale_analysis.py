#!/usr/bin/env python3
"""Analyze RN1's spread trading: scale, profitability, capital deployment.

Checks for spreads at TWO levels:
1. Same conditionId (Yes + No on same market)
2. Same eventSlug (opposite sides of different sub-markets within an event)
"""
import json, urllib.request, time
from collections import defaultdict

API = "https://data-api.polymarket.com"
RN1 = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"

def api_get(url):
    time.sleep(0.15)
    req = urllib.request.Request(url, headers={"User-Agent": "S/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def fetch_all_positions(endpoint, params=""):
    all_pos = []
    offset = 0
    limit = 500
    while True:
        url = f"{API}/{endpoint}?user={RN1}&limit={limit}&offset={offset}&sizeThreshold=0.1{params}"
        batch = api_get(url)
        if not batch:
            break
        all_pos.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_pos


print("Fetching RN1 open positions...")
open_pos = fetch_all_positions("positions")
print(f"Fetched {len(open_pos)} open positions\n")

# ============================================================
# SECTION 1: Same-conditionId spreads (Yes+No on same market)
# ============================================================
by_cid = defaultdict(list)
for p in open_pos:
    cid = p.get("conditionId", "")
    if cid:
        by_cid[cid].append(p)

cid_spreads = []
for cid, pos_list in by_cid.items():
    outcomes = set(p.get("outcome", "") for p in pos_list)
    if "Yes" in outcomes and "No" in outcomes:
        cid_spreads.append((cid, pos_list))

print(f"{'='*80}")
print(f"  SAME-MARKET SPREADS (Yes+No on same conditionId)")
print(f"{'='*80}")
print(f"Found: {len(cid_spreads)} spread pairs out of {len(by_cid)} unique conditionIds")

total_spread_cap = 0
for cid, pos_list in cid_spreads:
    yes_pos = [p for p in pos_list if p.get("outcome") == "Yes"]
    no_pos = [p for p in pos_list if p.get("outcome") == "No"]
    yes_iv = sum(float(p.get("initialValue", 0) or 0) for p in yes_pos)
    no_iv = sum(float(p.get("initialValue", 0) or 0) for p in no_pos)
    total_spread_cap += yes_iv + no_iv

print(f"Capital in same-market spreads: ${total_spread_cap:,.2f}")


# ============================================================
# SECTION 2: Event-level analysis
# ============================================================
print(f"\n{'='*80}")
print(f"  EVENT-LEVEL ANALYSIS")
print(f"{'='*80}")

by_event = defaultdict(list)
for p in open_pos:
    slug = p.get("eventSlug", "") or ""
    if slug:
        by_event[slug].append(p)

# Find events with multiple positions
multi_pos_events = {slug: plist for slug, plist in by_event.items() if len(plist) >= 2}
print(f"\nTotal events: {len(by_event)}")
print(f"Events with 2+ positions: {len(multi_pos_events)}")

# Classify events
event_details = []
for slug, plist in multi_pos_events.items():
    total_iv = sum(float(p.get("initialValue", 0) or 0) for p in plist)
    titles = set(p.get("title", "")[:50] for p in plist)
    outcomes = [(p.get("outcome", ""), p.get("title", "")[:40], float(p.get("initialValue", 0) or 0), float(p.get("size", 0))) for p in plist]
    event_details.append({
        "slug": slug,
        "positions": len(plist),
        "total_iv": total_iv,
        "outcomes": outcomes,
    })

event_details.sort(key=lambda x: x["total_iv"], reverse=True)

print(f"\nTop events by capital (multi-position):")
print(f"{'Event slug':<55} {'#pos':>5} {'Capital':>10}")
print("-" * 75)
for e in event_details[:40]:
    print(f"{e['slug']:<55} {e['positions']:>5} ${e['total_iv']:>9,.0f}")
    for outcome, title, iv, size in sorted(e['outcomes'], key=lambda x: -x[2]):
        if iv > 1:
            avg = iv/size if size > 0 else 0
            print(f"    {outcome:<6} {title:<40} ${iv:>7,.0f}  {size:>7,.0f}sh  @{avg:.2f}")


# ============================================================
# SECTION 3: Overall portfolio breakdown
# ============================================================
print(f"\n{'='*80}")
print(f"  PORTFOLIO BREAKDOWN")
print(f"{'='*80}")

total_capital = sum(float(p.get("initialValue", 0) or 0) for p in open_pos)
print(f"\nTotal open capital: ${total_capital:,.2f}")
print(f"Total open positions: {len(open_pos)}")
print(f"Unique conditionIds: {len(by_cid)}")
print(f"Unique events: {len(by_event)}")

# Sport/category breakdown by slug prefix
sport_capital = defaultdict(float)
sport_count = defaultdict(int)
for p in open_pos:
    slug = p.get("eventSlug", "") or ""
    parts = slug.split("-")
    sport = parts[0] if parts else "unknown"
    # Map common prefixes
    if sport in ("atp", "wta"):
        sport = "tennis"
    elif sport in ("nhl",):
        sport = "nhl"
    elif sport in ("mlb",):
        sport = "mlb"
    elif sport in ("nba",):
        sport = "nba"
    elif sport in ("nfl",):
        sport = "nfl"
    elif sport in ("lol", "dota2", "cs2", "counter"):
        sport = "esports"
    iv = float(p.get("initialValue", 0) or 0)
    sport_capital[sport] += iv
    sport_count[sport] += 1

print(f"\nCapital by sport/category:")
for sport, cap in sorted(sport_capital.items(), key=lambda x: -x[1]):
    pct = cap / total_capital * 100 if total_capital > 0 else 0
    print(f"  {sport:<15} ${cap:>10,.0f}  ({pct:5.1f}%)  {sport_count[sport]:>4} positions")


# ============================================================
# SECTION 4: Position size distribution
# ============================================================
print(f"\n{'='*80}")
print(f"  POSITION SIZE DISTRIBUTION")
print(f"{'='*80}")

ivs = [float(p.get("initialValue", 0) or 0) for p in open_pos]
buckets = {"<$10": 0, "$10-50": 0, "$50-100": 0, "$100-500": 0, "$500-1K": 0, "$1K-5K": 0, "$5K+": 0}
bucket_cap = {"<$10": 0, "$10-50": 0, "$50-100": 0, "$100-500": 0, "$500-1K": 0, "$1K-5K": 0, "$5K+": 0}

for iv in ivs:
    if iv < 10:
        k = "<$10"
    elif iv < 50:
        k = "$10-50"
    elif iv < 100:
        k = "$50-100"
    elif iv < 500:
        k = "$100-500"
    elif iv < 1000:
        k = "$500-1K"
    elif iv < 5000:
        k = "$1K-5K"
    else:
        k = "$5K+"
    buckets[k] += 1
    bucket_cap[k] += iv

print(f"\n{'Bucket':<12} {'Count':>6} {'Capital':>12} {'Avg':>8}")
print("-" * 42)
for k in ["<$10", "$10-50", "$50-100", "$100-500", "$500-1K", "$1K-5K", "$5K+"]:
    avg = bucket_cap[k] / buckets[k] if buckets[k] > 0 else 0
    print(f"{k:<12} {buckets[k]:>6} ${bucket_cap[k]:>10,.0f} ${avg:>7,.0f}")

# ============================================================
# SECTION 5: Average price analysis
# ============================================================
print(f"\n{'='*80}")
print(f"  PRICE ANALYSIS (what prices does RN1 buy at?)")
print(f"{'='*80}")

price_buckets = defaultdict(lambda: {"count": 0, "capital": 0})
for p in open_pos:
    iv = float(p.get("initialValue", 0) or 0)
    size = float(p.get("size", 0))
    if size > 0 and iv > 0:
        avg = iv / size
        bucket = f"{int(avg*10)*10}-{int(avg*10)*10+10}c"
        price_buckets[bucket]["count"] += 1
        price_buckets[bucket]["capital"] += iv

print(f"\n{'Price range':<12} {'Count':>6} {'Capital':>12}")
print("-" * 34)
for k in sorted(price_buckets.keys()):
    b = price_buckets[k]
    print(f"{k:<12} {b['count']:>6} ${b['capital']:>10,.0f}")


# ============================================================
# SECTION 6: Closed positions analysis
# ============================================================
print(f"\n{'='*80}")
print(f"  CLOSED POSITIONS")
print(f"{'='*80}")

closed_pos = fetch_all_positions("closed-positions")
print(f"Fetched {len(closed_pos)} closed positions")

total_closed_pnl = 0
wins = 0
losses = 0
for p in closed_pos:
    pnl = float(p.get("cashPnl", 0) or 0)
    total_closed_pnl += pnl
    if pnl > 0:
        wins += 1
    elif pnl < 0:
        losses += 1

print(f"Total cashPnl: ${total_closed_pnl:,.2f}")
print(f"Wins: {wins}, Losses: {losses}, Neutral: {len(closed_pos) - wins - losses}")

# Show closed details
closed_details = []
for p in closed_pos:
    pnl = float(p.get("cashPnl", 0) or 0)
    title = p.get("title", "")[:55]
    outcome = p.get("outcome", "?")
    size = float(p.get("size", 0))
    closed_details.append({"title": title, "outcome": outcome, "pnl": pnl, "size": size})

closed_details.sort(key=lambda x: x["pnl"])
print(f"\nBiggest losses:")
for d in closed_details[:10]:
    print(f"  ${d['pnl']:>8.2f}  {d['outcome']:<6} {d['title']}")

print(f"\nBiggest wins:")
for d in sorted(closed_details, key=lambda x: -x["pnl"])[:10]:
    print(f"  ${d['pnl']:>8.2f}  {d['outcome']:<6} {d['title']}")

# Group closed by event to find spread pairs
by_event_closed = defaultdict(list)
for p in closed_pos:
    slug = p.get("eventSlug", "") or ""
    if slug:
        by_event_closed[slug].append(p)

multi_closed = {s: pl for s, pl in by_event_closed.items() if len(pl) >= 2}
print(f"\nClosed events with 2+ positions: {len(multi_closed)}")
for slug, plist in sorted(multi_closed.items(), key=lambda x: sum(float(p.get("cashPnl",0) or 0) for p in x[1])):
    total_pnl = sum(float(p.get("cashPnl", 0) or 0) for p in plist)
    print(f"  {slug:<50} PnL: ${total_pnl:>8.2f}  ({len(plist)} pos)")
    for p in plist:
        pnl = float(p.get("cashPnl", 0) or 0)
        print(f"    {p.get('outcome','?'):<6} {p.get('title','')[:45]:<45} ${pnl:>8.2f}")
