#!/usr/bin/env python3
"""Sell wrong-side positions and buy correct side."""
import os, re, time, json, urllib.request

from py_clob_client.client import ClobClient
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.clob_types import OrderArgs, OrderType as OT
from dotenv import load_dotenv

load_dotenv("/opt/bottie/.env")

pk = os.environ.get("PRIVATE_KEY", "")
if not pk.startswith("0x"):
    pk = "0x" + pk

FUNDER = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"
CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

client = ClobClient(
    CLOB, key=pk, chain_id=137, signature_type=2, funder=FUNDER,
)
client.set_api_creds(client.derive_api_key())

def g(u):
    req = urllib.request.Request(u, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def get_pos(addr):
    all_p = []
    offset = 0
    while offset < 10000:
        batch = g(API + "/positions?user=" + addr + "&limit=500&offset=" + str(offset) + "&sizeThreshold=0.1")
        if not batch: break
        all_p.extend(batch)
        if len(batch) < 500: break
        offset += 500
    return all_p

def get_best_bid(token_id):
    url = CLOB + "/book?token_id=" + token_id
    req = urllib.request.Request(url, headers={"User-Agent": "B/1"})
    book = json.loads(urllib.request.urlopen(req, timeout=10).read())
    bids = book.get("bids", [])
    if not bids:
        return None, 0
    best = max(bids, key=lambda b: float(b.get("price", 0)))
    return float(best["price"]), float(best.get("size", 0))

def get_best_ask(token_id):
    url = CLOB + "/book?token_id=" + token_id
    req = urllib.request.Request(url, headers={"User-Agent": "B/1"})
    book = json.loads(urllib.request.urlopen(req, timeout=10).read())
    asks = book.get("asks", [])
    if not asks:
        return None, 0
    best = min(asks, key=lambda a: float(a.get("price", 99)))
    return float(best["price"]), float(best.get("size", 0))

# Wrong positions to sell (our positions on wrong side)
wrong_sides = [
    {"slug": "nba-nop-nyk-2026-03-24", "our_outcome": "Pelicans", "correct_outcome": "Knicks", "title_contains": "Spread: Knicks"},
    {"slug": "nba-orl-cle-2026-03-24", "our_outcome": "Magic", "correct_outcome": "Cavaliers", "title_contains": "Spread: Cavaliers"},
    {"slug": "nba-sac-cha-2026-03-24", "our_outcome": "Kings", "correct_outcome": "Hornets", "title_contains": "Kings vs. Hornets"},
    {"slug": "nhl-wsh-stl-2026-03-24", "our_outcome": "Capitals", "correct_outcome": "Blues", "title_contains": "Capitals vs. Blues"},
]

# Get our positions
our_pos = get_pos(FUNDER)
cannae_pos = get_pos(CANNAE)

BUDGET = 13.50

for fix in wrong_sides:
    slug = fix["slug"]
    print()
    print("=" * 70)
    print("FIX: " + slug)
    print("=" * 70)

    # Find our wrong position
    our_wrong = None
    for p in our_pos:
        p_slug = (p.get("eventSlug", "") or p.get("slug", "") or "").split("-more-markets")[0]
        p_outcome = p.get("outcome", "") or ""
        p_title = p.get("title", "") or ""
        if p_slug == slug and p_outcome == fix["our_outcome"] and fix["title_contains"].lower() in p_title.lower():
            our_wrong = p
            break

    if not our_wrong:
        print("  Our wrong position not found — maybe already sold?")
        # Still try to buy correct side
    else:
        token_sell = our_wrong.get("asset", "") or ""
        shares_sell = int(float(our_wrong.get("size", 0) or 0))
        title_sell = (our_wrong.get("title", "") or "")[:50]

        if shares_sell < 1 or not token_sell:
            print("  Nothing to sell (shares=" + str(shares_sell) + ")")
        else:
            # Get bid price
            bid, bid_depth = get_best_bid(token_sell)
            if bid is None or bid <= 0.01:
                print("  SKIP SELL: no bid for " + title_sell)
            else:
                print("  SELL " + fix["our_outcome"] + " " + str(shares_sell) + "sh @ " + str(round(bid * 100)) + "ct " + title_sell + "...", end=" ", flush=True)
                try:
                    order_args = OrderArgs(price=bid, size=shares_sell, side=SELL, token_id=token_sell)
                    signed = client.create_order(order_args)
                    resp = client.post_order(signed, OT.GTC)
                    print("-> " + resp.get("status", "?") + " matched=" + resp.get("size_matched", "0"))
                except Exception as e:
                    err = str(e)
                    m = re.search(r"(?:taker|maker) fee: (\d+)", err)
                    if m:
                        fee = int(m.group(1))
                        print("retry fee=" + str(fee) + "...", end=" ", flush=True)
                        try:
                            order_args = OrderArgs(price=bid, size=shares_sell, side=SELL, token_id=token_sell, fee_rate_bps=fee)
                            signed = client.create_order(order_args)
                            resp = client.post_order(signed, OT.GTC)
                            print("-> " + resp.get("status", "?") + " matched=" + resp.get("size_matched", "0"))
                        except Exception as e2:
                            print("FAIL: " + str(e2)[:100])
                    else:
                        print("FAIL: " + err[:100])
                time.sleep(1)

    # Find Cannae's correct position to get the right token_id
    cannae_correct = None
    for p in cannae_pos:
        p_slug = (p.get("eventSlug", "") or p.get("slug", "") or "").split("-more-markets")[0]
        p_outcome = p.get("outcome", "") or ""
        p_title = p.get("title", "") or ""
        if p_slug == slug and p_outcome == fix["correct_outcome"] and fix["title_contains"].lower() in p_title.lower():
            cost = float(p.get("size", 0) or 0) * float(p.get("avgPrice", 0) or 0)
            if cannae_correct is None or cost > float(cannae_correct.get("size", 0) or 0) * float(cannae_correct.get("avgPrice", 0) or 0):
                cannae_correct = p

    if not cannae_correct:
        print("  Cannae correct position not found for " + fix["correct_outcome"])
        continue

    token_buy = cannae_correct.get("asset", "") or ""
    if not token_buy:
        print("  No token_id for correct side")
        continue

    # Buy correct side
    ask, ask_depth = get_best_ask(token_buy)
    if ask is None or ask >= 0.95:
        print("  SKIP BUY: ASK=" + str(ask) + " (resolved or no liquidity)")
        continue

    shares_buy = int(BUDGET / ask)
    title_buy = (cannae_correct.get("title", "") or "")[:50]

    print("  BUY " + fix["correct_outcome"] + " " + str(shares_buy) + "sh @ " + str(round(ask * 100)) + "ct " + title_buy + "...", end=" ", flush=True)
    try:
        order_args = OrderArgs(price=ask, size=shares_buy, side=BUY, token_id=token_buy)
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OT.GTC)
        print("-> " + resp.get("status", "?") + " matched=" + resp.get("size_matched", "0"))
    except Exception as e:
        err = str(e)
        m = re.search(r"(?:taker|maker) fee: (\d+)", err)
        if m:
            fee = int(m.group(1))
            print("retry fee=" + str(fee) + "...", end=" ", flush=True)
            try:
                order_args = OrderArgs(price=ask, size=shares_buy, side=BUY, token_id=token_buy, fee_rate_bps=fee)
                signed = client.create_order(order_args)
                resp = client.post_order(signed, OT.GTC)
                print("-> " + resp.get("status", "?") + " matched=" + resp.get("size_matched", "0"))
            except Exception as e2:
                print("FAIL: " + str(e2)[:100])
        else:
            print("FAIL: " + err[:100])
    time.sleep(1)

print()
print("Done. Check positions to verify.")
