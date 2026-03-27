#!/usr/bin/env python3
"""Plan: wat gaat de bot inkopen vannacht? Proportionele sizing."""
import json, urllib.request

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

bankroll = 1432
max_pct = 0.08

# Games die nog niet gestart zijn (01:00+ CET)
future_slugs = [
    "nhl-chi-phi-2026-03-26",
    "nhl-dal-nyi-2026-03-26",
    "nhl-min-fla-2026-03-26",
    "nba-nop-det-2026-03-26",
    "nba-nyk-cha-2026-03-26",
    "nba-sac-orl-2026-03-26",
    "fif-col-hrv-2026-03-26",
    "nhl-col-wpg-2026-03-26",
    "mlb-ari-lad-2026-03-26",
    "nhl-edm-las-2026-03-26",
    "nhl-ana-cal-2026-03-26",
    "nhl-wsh-utah-2026-03-26",
    "nhl-sj-stl-2026-03-26",
    "nhl-cbj-mon-2026-03-26",
    "mlb-laa-hou-2026-03-26",
    "mlb-cle-sea-2026-03-26",
]

def mt(title):
    tl = title.lower()
    if "o/u" in tl or "over/under" in tl: return "ou"
    elif "spread" in tl: return "spread"
    elif "draw" in tl: return "draw"
    elif "both teams" in tl or "btts" in tl: return "btts"
    else: return "win"

total_planned = 0
for slug in future_slugs:
    game_pos = [p for p in positions if (p.get("eventSlug") or p.get("slug") or "").replace("-more-markets","") == slug]
    if not game_pos: continue

    game_total = sum(float(p.get("initialValue") or 0) for p in game_pos)
    if game_total <= 0: continue

    # Allowed: win + spread (US sports), win + draw (FIFA)
    is_fifa = slug.startswith("fif-")
    allowed = ["win", "draw"] if is_fifa else ["win", "spread"]

    # Group by conditionId, pick best
    by_cid = {}
    for p in game_pos:
        cid = p.get("conditionId","")
        if not cid: continue
        if cid not in by_cid: by_cid[cid] = []
        by_cid[cid].append(p)

    game_buy = []
    for cid, cl in by_cid.items():
        sl = sorted(cl, key=lambda x: float(x.get("initialValue") or 0), reverse=True)
        best = sl[0]
        second = sl[1] if len(sl) > 1 else None
        title = best.get("title","")
        mtype = mt(title)
        if mtype not in allowed: continue

        best_usdc = float(best.get("initialValue") or 0)
        second_usdc = float(second.get("initialValue") or 0) if second else 0
        conv = best_usdc / (best_usdc + second_usdc) if (best_usdc + second_usdc) > 0 else 1.0
        lw = best_usdc / game_total
        our = bankroll * lw * conv * max_pct
        if our < 2.50: continue

        outcome = best.get("outcome","")
        cur = float(best.get("curPrice") or 0)
        game_buy.append((mtype, outcome, title[:50], our, cur, conv, lw))

    if not game_buy: continue
    game_total_ours = sum(x[3] for x in game_buy)
    total_planned += game_total_ours

    print(f"{slug} | Cannae ${game_total:,.0f} | ONZE INZET: ${game_total_ours:.0f}")
    for mtype, outcome, title, our, cur, conv, lw in sorted(game_buy, key=lambda x: -x[3]):
        print(f"  ${our:>5.0f}  {mtype:<6} {outcome:<15} {title:<50} @{cur*100:.0f}ct conv={conv:.0f}% wt={lw:.0%}")
    print()

print("=" * 70)
print(f"TOTAAL GEPLAND VANNACHT: ${total_planned:.0f}")
print(f"BANKROLL: ${bankroll}")
print(f"DEPLOYMENT: {total_planned/bankroll:.0%}")
