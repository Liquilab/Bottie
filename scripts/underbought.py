#!/usr/bin/env python3
"""Check welke posities te laag zijn en hoeveel bijkopen."""
import json, urllib.request, os

# Cannae posities
addr = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
positions = []
offset = 0
while True:
    url = f"https://data-api.polymarket.com/positions?user={addr}&limit=500&sizeThreshold=0.1&sortBy=CURRENT&sortOrder=desc&offset={offset}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        page = json.loads(resp.read())
    positions.extend(page)
    if len(page) < 500: break
    offset += 500
    if offset > 10000: break

# Onze posities
our_addr = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"
our_pos = []
offset = 0
while True:
    url = f"https://data-api.polymarket.com/positions?user={our_addr}&limit=500&sizeThreshold=0.1&sortBy=CURRENT&sortOrder=desc&offset={offset}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        page = json.loads(resp.read())
    our_pos.extend(page)
    if len(page) < 500: break
    offset += 500

our_by_cid = {}
for p in our_pos:
    cid = p.get("conditionId","")
    if cid and float(p.get("size") or 0) > 0.01:
        our_by_cid[cid] = float(p.get("size") or 0) * float(p.get("avgPrice") or p.get("curPrice") or 0)

def mt(title):
    tl = title.lower()
    if "o/u" in tl or "over/under" in tl: return "ou"
    elif "spread" in tl: return "spread"
    elif "draw" in tl: return "draw"
    elif "both teams" in tl or "btts" in tl: return "btts"
    else: return "win"

bankroll = 1300  # na vanavond's inkopen
max_pct = 0.07

# Vandaag's actieve games
active_slugs = set()
for p in positions:
    slug = (p.get("eventSlug") or p.get("slug") or "").replace("-more-markets","")
    if "2026-03-26" in slug or "2026-03-27" in slug:
        cur = float(p.get("curPrice") or 0)
        if 0.01 < cur < 0.99:
            active_slugs.add(slug)

print("BIJKOPEN CHECKLIST")
print("=" * 80)

total_extra = 0
for slug in sorted(active_slugs):
    game_pos = [p for p in positions if (p.get("eventSlug") or p.get("slug") or "").replace("-more-markets","") == slug
                and 0.01 < float(p.get("curPrice") or 0) < 0.99]
    if not game_pos: continue

    is_soccer = not slug.startswith(("nba-","nhl-","mlb-","nfl-","fif-"))
    allowed = ["win", "draw"] if is_soccer else ["win", "spread"]

    # FIXED: game_total alleen op ALLOWED types
    filtered_total = sum(float(p.get("initialValue") or 0) for p in game_pos if mt(p.get("title","")) in allowed)
    if filtered_total <= 0: continue

    by_cid = {}
    for p in game_pos:
        cid = p.get("conditionId","")
        if not cid: continue
        if cid not in by_cid: by_cid[cid] = []
        by_cid[cid].append(p)

    game_buys = []
    for cid, cl in by_cid.items():
        sl = sorted(cl, key=lambda x: float(x.get("initialValue") or 0), reverse=True)
        best = sl[0]
        title = best.get("title","")
        mtype = mt(title)
        if mtype not in allowed: continue

        best_usdc = float(best.get("initialValue") or 0)
        second = sl[1] if len(sl) > 1 else None
        second_usdc = float(second.get("initialValue") or 0) if second else 0
        conv = best_usdc / (best_usdc + second_usdc) if (best_usdc + second_usdc) > 0 else 1.0
        lw = best_usdc / filtered_total
        should_be = bankroll * lw * conv * max_pct
        if should_be < 2.50: continue

        # Wat hebben we?
        we_have = our_by_cid.get(cid, 0)
        outcome = best.get("outcome","")
        cur = float(best.get("curPrice") or 0)

        if we_have < should_be * 0.7:  # meer dan 30% te laag
            extra = should_be - we_have
            game_buys.append((mtype, outcome, title[:45], should_be, we_have, extra, cur))

    if game_buys:
        print(f"\n{slug} (filtered total ${filtered_total:,.0f})")
        for mtype, outcome, title, should, have, extra, cur in sorted(game_buys, key=lambda x: -x[5]):
            print(f"  ${extra:>5.0f} extra | {mtype:<6} {outcome:<15} {title:<45} | should=${should:.0f} have=${have:.0f} @{cur*100:.0f}ct")
            total_extra += extra

print(f"\n{'=' * 80}")
print(f"TOTAAL BIJ TE KOPEN: ${total_extra:.0f}")
