#!/usr/bin/env python3
import json, urllib.request
addr = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
pos = []
offset = 0
while True:
    url = f"https://data-api.polymarket.com/positions?user={addr}&limit=500&sizeThreshold=0.1&sortBy=CURRENT&sortOrder=desc&offset={offset}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        page = json.loads(resp.read())
    pos.extend(page)
    if len(page) < 500: break
    offset += 500
    if offset > 10000: break

print("CANNAE HAUPTBET vs ONZE FILLS")
print("=" * 70)
for slug in ["nhl-chi-phi-2026-03-26","nhl-dal-nyi-2026-03-26","nhl-cbj-mon-2026-03-26",
             "nhl-min-fla-2026-03-26","nba-nop-det-2026-03-26","nba-nyk-cha-2026-03-26"]:
    game = [p for p in pos if slug in (p.get("eventSlug") or p.get("slug") or "").replace("-more-markets","")
            and 0.01 < float(p.get("curPrice") or 0) < 0.99]
    if not game: continue
    best = max(game, key=lambda p: float(p.get("initialValue") or 0))
    print(f"{slug}")
    print(f"  Cannae hauptbet: {best.get('outcome','')} {(best.get('title') or '')[:45]} ${float(best.get('initialValue') or 0):,.0f} @{float(best.get('avgPrice') or 0)*100:.0f}ct")
    # Show all legs sorted
    for p in sorted(game, key=lambda x: float(x.get("initialValue") or 0), reverse=True)[:3]:
        print(f"    {p.get('outcome',''):<15} ${float(p.get('initialValue') or 0):>6,.0f} @{float(p.get('avgPrice') or 0)*100:.0f}ct now={float(p.get('curPrice') or 0)*100:.0f}ct")
    print()
