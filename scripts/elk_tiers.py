#!/usr/bin/env python3
"""Analyze ELK positions by conviction tier."""
import json, urllib.request
from collections import defaultdict

API = "https://data-api.polymarket.com"
ELK = "0xead152b855effa6b5b5837f53b24c0756830c76a"
BANKROLL = 1631

pos = json.loads(urllib.request.urlopen(urllib.request.Request(
    "%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, ELK),
    headers={"User-Agent": "S/1", "Accept": "application/json"}
), timeout=20).read())

by_event = defaultdict(list)
for p in pos:
    slug = p.get("eventSlug", "") or ""
    if not slug:
        continue
    title = (p.get("title", "") or "").lower()
    if any(kw in title for kw in ["spread", "o/u", "over", "under", "draw", "both teams", "corner", "halftime", "exact"]):
        continue
    by_event[slug].append(p)

tiers = {"<1K": {"pct": 0.5, "n": 0, "iv": 0},
         "1K-5K": {"pct": 1.0, "n": 0, "iv": 0},
         "5K-20K": {"pct": 2.0, "n": 0, "iv": 0},
         "20K+": {"pct": 3.0, "n": 0, "iv": 0}}

print("%-35s %8s %8s %6s %8s" % ("Event", "HB IV", "ELK tot", "Tier", "Our bet"))
print("-" * 70)

for slug, positions in sorted(by_event.items(), key=lambda x: -max(float(p.get("initialValue", 0) or 0) for p in x[1])):
    if any(kw in slug for kw in ["winner", "season", "trophy", "champion", "mvp", "award"]):
        continue

    hauptbet = max(positions, key=lambda p: float(p.get("initialValue", 0) or 0))
    hb_iv = float(hauptbet.get("initialValue", 0) or 0)
    total_iv = sum(float(p.get("initialValue", 0) or 0) for p in positions)

    if hb_iv < 1000:
        tier = "<1K"
    elif hb_iv < 5000:
        tier = "1K-5K"
    elif hb_iv < 20000:
        tier = "5K-20K"
    else:
        tier = "20K+"

    pct = tiers[tier]["pct"]
    our_bet = BANKROLL * pct / 100
    tiers[tier]["n"] += 1
    tiers[tier]["iv"] += hb_iv

    print("%-35s $%7.0f $%7.0f  %-6s  $%5.1f" % (slug[:34], hb_iv, total_iv, tier, our_bet))

print()
print("=== SAMENVATTING ===")
print("%-8s %5s %10s %6s %8s" % ("Tier", "Games", "ELK invest", "Pct", "Onze bet"))
print("-" * 45)
for tier in ["<1K", "1K-5K", "5K-20K", "20K+"]:
    t = tiers[tier]
    our = BANKROLL * t["pct"] / 100
    print("%-8s %5d $%9.0f  %4.1f%%  $%6.1f" % (tier, t["n"], t["iv"], t["pct"], our))

print()
total_events = sum(t["n"] for t in tiers.values())
total_deployed = sum(BANKROLL * t["pct"] / 100 * t["n"] for t in tiers.values())
print("Totaal: %d events, $%.0f deployed als alles tegelijk" % (total_events, total_deployed))
print("Bankroll: $%d" % BANKROLL)
print("Max deployment: %.0f%%" % (total_deployed / BANKROLL * 100))
