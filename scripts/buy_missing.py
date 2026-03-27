#!/usr/bin/env python3
"""Buy missing UEF legs NOW via CLOB FOK orders."""
import os, json, urllib.request
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

client = ClobClient("https://clob.polymarket.com", key=os.environ["PRIVATE_KEY"], chain_id=137, funder=os.environ["FUNDER_ADDRESS"], signature_type=2)
client.set_api_creds(client.create_or_derive_api_creds())

# Cannae UEF posities
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
our_addr = os.environ["FUNDER_ADDRESS"]
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
our_cids = {}
for p in our_pos:
    cid = p.get("conditionId","")
    outcome = (p.get("outcome","") or "").lower()
    if cid and float(p.get("size") or 0) > 0.01:
        our_cids[(cid, outcome)] = True

def mt(title):
    tl = title.lower()
    if "o/u" in tl or "over/under" in tl: return "ou"
    elif "spread" in tl: return "spread"
    elif "draw" in tl: return "draw"
    elif "both teams" in tl or "btts" in tl: return "btts"
    else: return "win"

uef = {}
for p in positions:
    slug = (p.get("eventSlug") or p.get("slug") or "").replace("-more-markets","")
    if not slug.startswith("uef-") or "2026-03-26" not in slug: continue
    cur = float(p.get("curPrice") or 0)
    if cur <= 0.01 or cur >= 0.99: continue
    if slug not in uef: uef[slug] = []
    uef[slug].append(p)

to_buy = []
for slug, legs in uef.items():
    game_total = sum(float(p.get("initialValue") or 0) for p in legs)
    by_cid = {}
    for p in legs:
        cid = p.get("conditionId","")
        if not cid: continue
        if cid not in by_cid: by_cid[cid] = []
        by_cid[cid].append(p)

    for cid, cl in by_cid.items():
        sl = sorted(cl, key=lambda x: float(x.get("initialValue") or 0), reverse=True)
        best = sl[0]
        title = best.get("title","")
        mtype = mt(title)
        if mtype not in ["win","draw"]: continue
        outcome = (best.get("outcome","") or "").lower()
        if (cid, outcome) in our_cids: continue
        usdc_val = float(best.get("initialValue") or 0)
        lw = usdc_val / game_total if game_total > 0 else 0
        second = sl[1] if len(sl) > 1 else None
        second_usdc = float(second.get("initialValue") or 0) if second else 0
        conv = usdc_val / (usdc_val + second_usdc) if (usdc_val + second_usdc) > 0 else 1.0
        our_usdc = 1410 * lw * conv * 0.08
        if our_usdc < 2.50: continue
        to_buy.append((cid, outcome, title[:55], our_usdc))

print(f"Te kopen: {len(to_buy)} legs")
filled = 0
failed = 0
for cid, outcome, title, usdc in to_buy:
    try:
        market = client.get_market(cid)
        tokens = market.get("tokens", [])
        clob_token = None
        for tok in tokens:
            if tok.get("outcome","").lower() == outcome:
                clob_token = tok.get("token_id")
                break
        if not clob_token and tokens:
            clob_token = tokens[0].get("token_id")
        if not clob_token:
            print(f"NO TOKEN: {title}")
            failed += 1
            continue

        book = client.get_order_book(clob_token)
        asks = book.asks if book.asks else []
        if not asks:
            print(f"NO ASK: {title}")
            failed += 1
            continue
        ask = float(asks[0].price)
        buy_shares = max(5, int(usdc / ask))

        order_args = OrderArgs(price=ask, size=buy_shares, side="BUY", token_id=clob_token)
        signed = client.create_order(order_args)
        resp = client.post_order(signed, "FOK")
        matched = resp.get("size_matched", "0")
        print(f"FILLED: {outcome} {title} | {buy_shares}sh @{ask*100:.0f}ct | matched={matched}")
        filled += 1
    except Exception as e:
        print(f"FAILED: {title} | {str(e)[:120]}")
        failed += 1

print(f"\nDONE: {filled} filled, {failed} failed")
