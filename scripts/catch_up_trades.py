import json, urllib.request
from collections import defaultdict

API = "https://data-api.polymarket.com"

CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
SOVEREIGN = "0xee613b3fc183ee44f9da9c05f53e2da107e3debf"
US = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"

def g(u):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"}),
        timeout=30
    ).read())

# Get our open positions (to check what we already have)
our_pos = g(f"{API}/positions?user={US}&limit=500&sizeThreshold=0.01") or []
our_keys = set()
our_events = defaultdict(set)  # event_slug -> set of outcomes
for p in our_pos:
    if float(p.get("size", 0)) > 0.01:
        cid = p.get("conditionId", "")
        out = p.get("outcome", "")
        our_keys.add(cid + ":" + out)
        slug = p.get("eventSlug", "") or ""
        if slug:
            our_events[slug].add(out)

print(f"Onze open posities: {len(our_keys)}")

def detect_market_type(title):
    if "O/U" in title:
        return "ou"
    if "win on" in title or "win the" in title:
        return "win"
    if "draw" in title.lower():
        return "draw"
    if "Spread" in title:
        return "spread"
    if "vs." in title or "vs " in title:
        return "ml"
    return "other"

# Process both wallets
today = "2026-03-17"
tomorrow = "2026-03-18"

all_candidates = []
events_claimed = defaultdict(set)  # event_slug -> set of outcomes we'll trade

for addr, name, allowed_types, min_p, max_p in [
    (CANNAE, "Cannae", {"win", "draw", "spread", "ml"}, 0.25, 0.65),
    (SOVEREIGN, "sovereign", {"ou"}, 0.50, 0.60),
]:
    positions = g(f"{API}/positions?user={addr}&limit=500&sizeThreshold=0.01") or []
    open_pos = [p for p in positions if float(p.get("size", 0)) > 0.01]

    for p in open_pos:
        title = p.get("title", "")
        outcome = p.get("outcome", "")
        entry = float(p.get("avgPrice", 0))
        cur_price = float(p.get("curPrice", 0))
        end_date = (p.get("endDate") or "")[:10]
        cid = p.get("conditionId", "")
        slug = p.get("eventSlug", "") or ""
        size = float(p.get("size", 0))
        value = size * cur_price
        mtype = detect_market_type(title)

        # Filter 1: resolves today or tomorrow
        if end_date not in [today, tomorrow]:
            continue

        # Filter 2: market type allowed for this wallet
        if mtype not in allowed_types:
            continue

        # Filter 3: entry price in range
        if entry < min_p or entry > max_p:
            continue

        # Filter 4: we don't already have this position
        key = cid + ":" + outcome
        if key in our_keys:
            continue

        # Filter 5: no contradiction (skip "up or down")
        title_lower = title.lower()
        if "up or down" in title_lower:
            continue

        price_diff = (cur_price - entry) / entry * 100 if entry > 0 else 0

        all_candidates.append({
            "wallet": name,
            "title": title[:55],
            "outcome": outcome[:15],
            "mtype": mtype,
            "entry": entry,
            "current": cur_price,
            "diff_pct": price_diff,
            "end_date": end_date,
            "slug": slug,
            "cid": cid,
        })

# Sort: today first, then by price diff (closest to original entry)
all_candidates.sort(key=lambda x: (x["end_date"], abs(x["diff_pct"])))

# Apply contradiction check: if same event_slug, different wallet, skip second
final = []
event_wallet = {}  # slug -> first wallet that claimed it

for c in all_candidates:
    slug = c["slug"]
    if slug and slug in event_wallet and event_wallet[slug] != c["wallet"]:
        # Different wallet on same event — contradiction risk, skip
        continue
    # Also check our existing positions for contradictions
    if slug and slug in our_events:
        # We already have a position on this event
        continue
    if slug:
        event_wallet[slug] = c["wallet"]
    final.append(c)

print(f"Candidates na alle filters: {len(final)}")
print()

# Group by wallet
cannae_trades = [c for c in final if c["wallet"] == "Cannae"]
sov_trades = [c for c in final if c["wallet"] == "sovereign"]

bankroll = 230
max_concurrent = 20
avg_price = 0.45

print(f"=== CANNAE TRADES ({len(cannae_trades)}) ===")
print(f"{'Markt':<55s} {'Outcome':<15s} {'Entry':>6s} {'Nu':>6s} {'Diff':>6s} {'Bet':>6s}")
print("-" * 100)
total_bet = 0
for c in cannae_trades:
    bet = (bankroll / max_concurrent) * (c["current"] / avg_price)
    bet = max(bet, 5 * c["current"])  # PM minimum 5 shares
    total_bet += bet
    print(f"{c['title']:<55s} {c['outcome']:<15s} {c['entry']*100:>5.1f}ct {c['current']*100:>5.1f}ct {c['diff_pct']:>+5.1f}% ${bet:>5.2f}")

print(f"\nTotaal Cannae: ${total_bet:.2f}")

print(f"\n=== SOVEREIGN TRADES ({len(sov_trades)}) ===")
print(f"{'Markt':<55s} {'Outcome':<15s} {'Entry':>6s} {'Nu':>6s} {'Diff':>6s} {'Bet':>6s}")
print("-" * 100)
sov_bet = 0
for c in sov_trades:
    bet = (bankroll / max_concurrent) * (c["current"] / avg_price)
    bet = max(bet, 5 * c["current"])  # PM minimum 5 shares
    sov_bet += bet
    print(f"{c['title']:<55s} {c['outcome']:<15s} {c['entry']*100:>5.1f}ct {c['current']*100:>5.1f}ct {c['diff_pct']:>+5.1f}% ${bet:>5.2f}")

print(f"\nTotaal sovereign: ${sov_bet:.2f}")
print(f"\nTOTAAL: ${total_bet + sov_bet:.2f} | Resterend cash: ${bankroll - total_bet - sov_bet:.2f}")
