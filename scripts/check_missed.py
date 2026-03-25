#!/usr/bin/env python3
"""Check why we're missing Cannae games."""
import urllib.request, json, yaml
from collections import defaultdict

API = "https://data-api.polymarket.com"
CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"

def g(u):
    req = urllib.request.Request(u, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

with open("/opt/bottie/config.yaml") as f:
    config = yaml.safe_load(f)
for w in config["copy_trading"]["watchlist"]:
    if w.get("name") == "Cannae":
        our_leagues = set(w["leagues"])
        our_mt = set(w["market_types"])
        min_p = w.get("min_price", 0.4)
        max_p = w.get("max_price", 0.95)
        break

# Fetch top 500 positions by value
cannae = g(API + "/positions?user=" + CANNAE + "&limit=500&sizeThreshold=1&sortBy=CURRENT&sortDirection=DESC")
active = [p for p in cannae if 0.01 < float(p.get("curPrice", 0) or 0) < 0.99]

attempted = set()
for line in open("/opt/bottie/data/trades.jsonl"):
    t = json.loads(line)
    s = (t.get("event_slug", "") or "").split("-more-markets")[0]
    if s:
        attempted.add(s)

def classify(title):
    t = (title or "").lower()
    if "o/u" in t or "over/under" in t:
        return "ou"
    if "spread" in t:
        return "spread"
    if "btts" in t or "both teams" in t:
        return "btts"
    if "draw" in t:
        return "draw"
    return "win"

events = defaultdict(list)
for p in active:
    slug = (p.get("eventSlug", "") or "").split("-more-markets")[0]
    if slug:
        events[slug].append(p)

miss_reasons = defaultdict(int)
miss_detail = defaultdict(list)
have = 0
miss = 0

for slug, legs in events.items():
    league = slug.split("-")[0]
    if slug in attempted:
        have += 1
        continue
    miss += 1

    # Check each leg: would it pass our filters?
    has_copyable = False
    leg_reasons = set()
    for p in legs:
        mt = classify(p.get("title", ""))
        cur = float(p.get("curPrice", 0) or 0)
        if mt not in our_mt:
            leg_reasons.add("mt:" + mt)
        elif league not in our_leagues:
            leg_reasons.add("league:" + league)
        elif cur < min_p:
            leg_reasons.add("price<0.40 (" + str(round(cur, 2)) + ")")
        elif cur > max_p:
            leg_reasons.add("price>0.95")
        else:
            has_copyable = True

    if has_copyable:
        reason = "PASSABLE — T-30 not triggered yet"
    elif leg_reasons:
        reason = " + ".join(sorted(leg_reasons))
    else:
        reason = "unknown"

    cost = sum(float(p.get("size", 0) or 0) * float(p.get("avgPrice", 0) or 0) for p in legs)
    miss_reasons[reason] += 1
    miss_detail[reason].append((slug, cost))

print("Cannae: " + str(have + miss) + " active games | Wij: " + str(have) + " | Missen: " + str(miss))
print()
print("WAAROM MISSEN WE GAMES:")
print("=" * 80)
for reason in sorted(miss_reasons.keys(), key=lambda x: -miss_reasons[x]):
    n = miss_reasons[reason]
    total = sum(c for _, c in miss_detail[reason])
    print()
    print(reason)
    print("  " + str(n) + " games, Cannae $" + str(round(total)))
    for slug, cost in sorted(miss_detail[reason], key=lambda x: -x[1])[:5]:
        print("    " + slug + " ($" + str(round(cost)) + ")")
