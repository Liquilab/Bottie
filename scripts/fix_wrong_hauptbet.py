#!/usr/bin/env python3
"""
Fix wrong hauptbet positions: sell wrong legs, buy correct ones.
RUS-260: Pre-Fix B positions selected wrong conditionId/side.

Usage:
  python3 fix_wrong_hauptbet.py --dry-run   # preview
  python3 fix_wrong_hauptbet.py             # execute on VPS
"""
import json
import os
import re
import sys
import time
import urllib.request

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
US = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"

# ── Positions to SELL (wrong legs) ──
SELLS = [
    {
        "label": "San Diego FC No (wrong side — Cannae has Yes)",
        "condition_id": "0x7de5afdb86eda9da74e651ae8ebbcff22f73e3a505f9647d8068bd4cfb801ad9",
        "asset": "3471980618711770704666880587021083896065131525793430649697215497634454580802",
        "outcome": "No",
        "shares": 20.6,
    },
    {
        "label": "Instituto AC Córdoba No (wrong conditionId — should be Boca No)",
        "condition_id": "0xd5eb73c025b59e80031e23e7be8ea4df43e2556318851d086fcedad7533df9fb",
        "asset": "89939432682494185115331614842683561619961368907787090196620196404745952367071",
        "outcome": "No",
        "shares": 11.68,
    },
]

# ── Positions to BUY (correct hauptbet from Cannae) ──
BUYS = [
    {
        "label": "San Diego FC Yes (Cannae hauptbet $39.7k)",
        "condition_id": "0x7de5afdb86eda9da74e651ae8ebbcff22f73e3a505f9647d8068bd4cfb801ad9",
        "asset": "9204655041052609958575930294858691106772655714954895828853854287804565135351",
        "outcome": "Yes",
        "target_usdc": 15.0,  # ~same as what we had invested
    },
    {
        "label": "Boca Juniors No (Cannae hauptbet $28.6k)",
        "condition_id": "0x2a1239fa63486ba942fa314ffeb54fdc00085f354089fc7d611a7743d0e83b97",
        "asset": "35007596652141314323305673059484566443966910770463971804379377002306371194380",
        "outcome": "No",
        "target_usdc": 15.0,
    },
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Bottie/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_orderbook(token_id):
    """Fetch CLOB orderbook for a token."""
    url = f"{CLOB_API}/book?token_id={token_id}"
    return fetch(url)


def best_bid(book):
    """Get best bid price from orderbook."""
    bids = book.get("bids", [])
    if not bids:
        return 0.0
    return max(float(b["price"]) for b in bids)


def best_ask(book):
    """Get best ask price from orderbook."""
    asks = book.get("asks", [])
    if not asks:
        return 1.0
    return min(float(a["price"]) for a in asks)


def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 80)
    print("FIX WRONG HAUPTBET — RUS-260 cleanup")
    print("=" * 80)

    # ── Step 1: Check orderbooks for sells ──
    print("\n── SELLS: checking orderbooks ──\n")
    sell_orders = []
    for s in SELLS:
        book = get_orderbook(s["asset"])
        bid = best_bid(book)
        print(f"  {s['label']}")
        print(f"    shares={s['shares']}, best_bid={bid*100:.1f}ct")

        if bid <= 0.01:
            print(f"    ⚠️  SKIP — bid is {bid*100:.1f}ct (empty orderbook)")
            continue

        value = s["shares"] * bid
        print(f"    → SELL {s['shares']:.1f} shares @ {bid*100:.0f}ct = ${value:.2f}")
        sell_orders.append({**s, "price": bid, "value": value})

    # ── Step 2: Check orderbooks for buys ──
    print("\n── BUYS: checking orderbooks ──\n")
    buy_orders = []
    for b in BUYS:
        book = get_orderbook(b["asset"])
        ask = best_ask(book)
        print(f"  {b['label']}")
        print(f"    target=${b['target_usdc']:.0f}, best_ask={ask*100:.1f}ct")

        if ask >= 0.99:
            print(f"    ⚠️  SKIP — ask is {ask*100:.1f}ct (no liquidity)")
            continue

        shares = b["target_usdc"] / ask
        print(f"    → BUY {shares:.1f} shares @ {ask*100:.0f}ct = ${b['target_usdc']:.2f}")
        buy_orders.append({**b, "price": ask, "shares": shares})

    # ── Summary ──
    print(f"\n{'='*80}")
    print(f"PLAN: {len(sell_orders)} sells + {len(buy_orders)} buys")
    sell_total = sum(s["value"] for s in sell_orders)
    buy_total = sum(b["target_usdc"] for b in buy_orders)
    print(f"  Sell proceeds: ~${sell_total:.2f}")
    print(f"  Buy cost:      ~${buy_total:.2f}")
    print(f"  Net cost:      ~${buy_total - sell_total:.2f}")
    print(f"{'='*80}")

    if dry_run:
        print("\n--dry-run: geen orders geplaatst.")
        return

    # ── Execute ──
    print("\nInitializing CLOB client...")
    from py_clob_client.client import ClobClient
    from py_clob_client.order_builder.constants import BUY, SELL
    from py_clob_client.clob_types import OrderArgs, OrderType as OT
    from dotenv import load_dotenv
    load_dotenv("/opt/bottie/.env")

    pk = os.environ.get("PRIVATE_KEY", "")
    if not pk.startswith("0x"):
        pk = "0x" + pk

    client = ClobClient(
        CLOB_API,
        key=pk,
        chain_id=137,
        signature_type=2,
        funder=US,
    )
    client.set_api_creds(client.derive_api_key())
    print("CLOB client ready.\n")

    def place_with_retry(token_id, price, size, side, fee_bps=0):
        order_args = OrderArgs(price=round(price, 2), size=round(size, 1), side=side, token_id=token_id)
        signed = client.create_order(order_args)
        try:
            return client.post_order(signed, OT.FOK)
        except Exception as e:
            err = str(e)
            if "taker fee" in err:
                m = re.search(r'taker fee: (\d+)', err)
                if m:
                    fee = int(m.group(1))
                    print(f"    retry fee={fee}...", end=" ", flush=True)
                    order_args2 = OrderArgs(price=round(price, 2), size=round(size, 1), side=side, token_id=token_id, fee_rate_bps=fee)
                    signed2 = client.create_order(order_args2)
                    return client.post_order(signed2, OT.FOK)
            raise

    # Sells first
    print("── Executing SELLS (FOK) ──\n")
    for s in sell_orders:
        print(f"  SELL {s['shares']:.1f}sh {s['label'][:50]} @ {s['price']*100:.0f}ct...", end=" ", flush=True)
        try:
            resp = place_with_retry(s["asset"], s["price"], s["shares"], SELL)
            matched = resp.get("size_matched", "0")
            print(f"→ matched={matched}")
        except Exception as e:
            print(f"FAIL: {str(e)[:80]}")
        time.sleep(0.5)

    # Then buys
    print("\n── Executing BUYS (FOK) ──\n")
    for b in buy_orders:
        print(f"  BUY {b['shares']:.1f}sh {b['label'][:50]} @ {b['price']*100:.0f}ct...", end=" ", flush=True)
        try:
            resp = place_with_retry(b["asset"], b["price"], b["shares"], BUY)
            matched = resp.get("size_matched", "0")
            print(f"→ matched={matched}")
        except Exception as e:
            print(f"FAIL: {str(e)[:80]}")
        time.sleep(0.5)

    print(f"\n{'='*50}")
    print("DONE. Check PM portfolio to verify.")


if __name__ == "__main__":
    main()
