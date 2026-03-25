#!/usr/bin/env python3
"""Check if Bottie bought the right legs vs Cannae's hauptbet."""
import urllib.request, json
from collections import defaultdict

API = "https://data-api.polymarket.com"
CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
BOTTIE = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"

def g(u):
    req = urllib.request.Request(u, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def get_pos(addr):
    all_p = []
    offset = 0
    while offset < 10000:
        batch = g(API + "/positions?user=" + addr + "&limit=500&offset=" + str(offset) + "&sizeThreshold=0.1")
        if not batch:
            break
        all_p.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return [p for p in all_p if float(p.get("size", 0) or 0) > 0.1 and 0.01 < float(p.get("curPrice", 0) or 0) < 0.99]

cannae = get_pos(CANNAE)
bottie = get_pos(BOTTIE)

def group(positions):
    events = defaultdict(list)
    for p in positions:
        slug = (p.get("eventSlug", "") or p.get("slug", "") or "").split("-more-markets")[0]
        if slug:
            events[slug].append(p)
    return events

c_events = group(cannae)
b_events = group(bottie)

problems = []

for slug in sorted(b_events.keys()):
    b_legs = b_events[slug]
    c_legs = c_events.get(slug, [])
    league = slug.split("-")[0].upper()

    if not c_legs:
        print()
        print("[" + league + "] " + slug + " — Cannae heeft GEEN positie!")
        for p in b_legs:
            title = (p.get("title", "") or "")[:50]
            outcome = p.get("outcome", "") or ""
            cost = float(p.get("size", 0) or 0) * float(p.get("avgPrice", 0) or 0)
            print("  BOTTIE: " + outcome + " $" + str(round(cost, 2)) + " " + title)
        problems.append(slug + ": Cannae heeft geen positie")
        continue

    # Cannae: best per conditionId
    c_by_cid = defaultdict(list)
    for p in c_legs:
        cid = p.get("conditionId", "") or ""
        if cid:
            c_by_cid[cid].append(p)

    c_best = {}
    for cid, ps in c_by_cid.items():
        best = max(ps, key=lambda x: float(x.get("size", 0) or 0) * float(x.get("avgPrice", 0) or 0))
        c_best[cid] = best

    c_sorted = sorted(c_best.values(), key=lambda x: float(x.get("size", 0) or 0) * float(x.get("avgPrice", 0) or 0), reverse=True)
    c_haupt = c_sorted[0]
    c_haupt_cid = c_haupt.get("conditionId", "")
    c_haupt_outcome = c_haupt.get("outcome", "")
    c_haupt_cost = float(c_haupt.get("size", 0) or 0) * float(c_haupt.get("avgPrice", 0) or 0)

    print()
    print("[" + league + "] " + slug)
    print("  CANNAE hauptbet: " + c_haupt_outcome + " $" + str(round(c_haupt_cost)) + " " + (c_haupt.get("title", "") or "")[:50])

    for p in b_legs:
        b_outcome = p.get("outcome", "") or ""
        b_cid = p.get("conditionId", "") or ""
        b_cost = float(p.get("size", 0) or 0) * float(p.get("avgPrice", 0) or 0)
        b_title = (p.get("title", "") or "")[:50]

        c_match = c_best.get(b_cid)
        if c_match:
            c_out = c_match.get("outcome", "")
            if c_out == b_outcome:
                status = "OK — same side as Cannae"
            else:
                status = "WRONG SIDE! Cannae=" + c_out + " Bottie=" + b_outcome
                problems.append(slug + ": " + b_title + " wrong side")
        else:
            status = "NO MATCH — Cannae has no position on this conditionId"
            problems.append(slug + ": " + b_title + " no Cannae match")

        print("  BOTTIE: " + b_outcome.ljust(15) + " $" + str(round(b_cost, 2)).rjust(6) + " " + b_title)
        print("    -> " + status)

    # Show Cannae top legs
    print("  Cannae all legs:")
    for p in c_sorted[:5]:
        outcome = p.get("outcome", "") or ""
        cost = float(p.get("size", 0) or 0) * float(p.get("avgPrice", 0) or 0)
        title = (p.get("title", "") or "")[:45]
        print("    " + outcome.ljust(15) + " $" + str(round(cost)).rjust(7) + " " + title)

print()
print("=" * 60)
if problems:
    print("PROBLEMEN (" + str(len(problems)) + "):")
    for p in problems:
        print("  X " + p)
else:
    print("GEEN PROBLEMEN — alle legs matchen Cannae")
