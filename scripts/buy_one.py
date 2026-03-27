#!/usr/bin/env python3
"""Buy one specific leg manually."""
import os, sys, json, urllib.request
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

client = ClobClient("https://clob.polymarket.com", key=os.environ["PRIVATE_KEY"], chain_id=137, funder=os.environ["FUNDER_ADDRESS"], signature_type=2)
client.set_api_creds(client.create_or_derive_api_creds())

# nhl-chi-phi: Cannae's biggest = Flyers win
# Find condition_id and CLOB token
addr = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
url = f"https://data-api.polymarket.com/positions?user={addr}&limit=500&sizeThreshold=0.1&sortBy=CURRENT&sortOrder=desc"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req) as resp:
    positions = json.loads(resp.read())

target_slug = "nhl-chi-phi-2026-03-26"
target_usd = 180

# Find Cannae's biggest position on this game
game_pos = [p for p in positions if (p.get("eventSlug") or p.get("slug") or "").replace("-more-markets","") == target_slug]
if not game_pos:
    print("Game not found in Cannae positions")
    sys.exit(1)

best = max(game_pos, key=lambda p: float(p.get("initialValue") or 0))
cid = best.get("conditionId","")
outcome = (best.get("outcome","") or "").lower()
title = best.get("title","")
print(f"Target: {outcome} {title}")
print(f"Cannae: ${float(best.get('initialValue',0)):,.0f} | conditionId: {cid}")

# Get CLOB token ID
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
    print("No CLOB token found")
    sys.exit(1)

# Get ASK
book = client.get_order_book(clob_token)
asks = book.asks if book.asks else []
if not asks:
    print("No asks in orderbook")
    sys.exit(1)

ask = float(asks[0].price)
shares = int(target_usd / ask)
print(f"Buying {shares} shares @{ask*100:.0f}ct = ${shares*ask:.0f}")

order_args = OrderArgs(price=ask, size=shares, side="BUY", token_id=clob_token)
signed = client.create_order(order_args)
resp = client.post_order(signed, "FOK")
matched = resp.get("size_matched", "0")
oid = resp.get("orderID", "?")
print(f"RESULT: matched={matched} orderID={oid}")
