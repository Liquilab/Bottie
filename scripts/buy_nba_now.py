#!/usr/bin/env python3
"""Buy Cannae NBA moneyline legs we missed. FOK taker on ASK."""
import os, re, time, json, urllib.request

from py_clob_client.client import ClobClient
from py_clob_client.order_builder.constants import BUY
from py_clob_client.clob_types import OrderArgs, OrderType as OT
from dotenv import load_dotenv

load_dotenv("/opt/bottie/.env")

pk = os.environ.get("PRIVATE_KEY", "")
if not pk.startswith("0x"):
    pk = "0x" + pk

client = ClobClient(
    "https://clob.polymarket.com",
    key=pk, chain_id=137, signature_type=2,
    funder="0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15",
)
client.set_api_creds(client.derive_api_key())

BUDGET_PER_TRADE = 13.50  # ~1% bankroll

trades = [
    {
        "label": "Nuggets WIN (DEN-PHX)",
        "token": "23233394988453138676453756568931964683203148302915027263468721213424953033444",
    },
    {
        "label": "Knicks WIN (NOP-NYK)",
        "token": "56684785511292775056526874919523573033709028315047249779514026874801501110177",
    },
    {
        "label": "Cavaliers WIN (ORL-CLE)",
        "token": "82162335245384915830214727777598327360549892486286572930820029431907406804211",
    },
    {
        "label": "Hornets WIN (SAC-CHA)",
        "token": "97747274531683402808496972324286989903538719810376477370291929261366387114981",
    },
]

def get_best_ask(token_id):
    url = "https://clob.polymarket.com/book?token_id=" + token_id
    req = urllib.request.Request(url, headers={"User-Agent": "B/1"})
    book = json.loads(urllib.request.urlopen(req, timeout=10).read())
    asks = book.get("asks", [])
    if not asks:
        return None, 0
    best = min(asks, key=lambda a: float(a.get("price", 99)))
    return float(best["price"]), float(best.get("size", 0))

for t in trades:
    ask, depth = get_best_ask(t["token"])
    if ask is None:
        print("SKIP " + t["label"] + ": no ASK in orderbook")
        continue
    if ask >= 0.95:
        print("SKIP " + t["label"] + ": ASK=" + str(ask) + " too high")
        continue

    price = round(ask + 0.01, 2)  # ASK+1ct to sweep deeper
    shares = int(BUDGET_PER_TRADE / price)

    print("BUY " + t["label"] + " -- " + str(shares) + "sh @ " + str(round(price*100)) + "ct = $" + str(BUDGET_PER_TRADE) + " (depth=" + str(round(depth)) + "sh)...", end=" ", flush=True)

    try:
        order_args = OrderArgs(price=price, size=shares, side=BUY, token_id=t["token"])
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OT.GTC)
        status = resp.get("status", "?")
        matched = resp.get("size_matched", "0")
        oid = resp.get("orderID", "?")[:16]
        print("-> " + status + " matched=" + matched + " oid=" + oid)
    except Exception as e:
        err = str(e)
        m = re.search(r"(?:taker|maker) fee: (\d+)", err)
        if m:
            fee = int(m.group(1))
            print("retry fee=" + str(fee) + "bps...", end=" ", flush=True)
            try:
                order_args = OrderArgs(price=price, size=shares, side=BUY, token_id=t["token"], fee_rate_bps=fee)
                signed = client.create_order(order_args)
                resp = client.post_order(signed, OT.GTC)
                print("-> " + resp.get("status","?") + " matched=" + resp.get("size_matched","0"))
            except Exception as e2:
                print("FAIL: " + str(e2)[:100])
        elif "fully filled" in err:
            price2 = round(ask + 0.01, 2)
            shares2 = int(BUDGET_PER_TRADE / price2)
            print("FOK killed, retry @" + str(round(price2*100)) + "ct...", end=" ", flush=True)
            try:
                order_args = OrderArgs(price=price2, size=shares2, side=BUY, token_id=t["token"])
                signed = client.create_order(order_args)
                resp = client.post_order(signed, OT.GTC)
                print("-> " + resp.get("status","?") + " matched=" + resp.get("size_matched","0"))
            except Exception as e3:
                print("FAIL: " + str(e3)[:100])
        else:
            print("FAIL: " + err[:100])
    time.sleep(1)

print("\nDone.")
