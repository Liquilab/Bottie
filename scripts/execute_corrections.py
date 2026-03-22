"""
Execute sell+buy corrections: sell wrong-side bets, buy correct side + missing games.
Uses FOK (Fill or Kill) orders via py_clob_client.

Usage:
  python3 execute_corrections.py --dry-run   # preview only
  python3 execute_corrections.py             # execute live
"""
import json
import os
import re
import sys
import time

CLOB_API = "https://clob.polymarket.com"
FUNDER = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"


def load_trades(path):
    with open(path) as f:
        return json.load(f)


def init_client():
    from py_clob_client.client import ClobClient
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
        funder=FUNDER,
    )
    client.set_api_creds(client.derive_api_key())
    return client


def place_sell(client, token_id, price, shares, fee_bps=0):
    """Place a FOK SELL order."""
    from py_clob_client.order_builder.constants import SELL
    from py_clob_client.clob_types import OrderArgs, OrderType as OT

    order_args = OrderArgs(
        price=round(price, 2),
        size=round(shares, 1),
        side=SELL,
        token_id=token_id,
        fee_rate_bps=fee_bps,
    )
    signed = client.create_order(order_args)
    resp = client.post_order(signed, OT.FOK)
    return resp


def place_buy(client, token_id, price, shares, fee_bps=0):
    """Place a FOK BUY order."""
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType as OT

    order_args = OrderArgs(
        price=round(price, 2),
        size=round(shares, 1),
        side=BUY,
        token_id=token_id,
        fee_rate_bps=fee_bps,
    )
    signed = client.create_order(order_args)
    resp = client.post_order(signed, OT.FOK)
    return resp


def get_fee_bps(client, token_id):
    """Fetch fee rate for token."""
    try:
        resp = client.get_fee_rate_bps(token_id)
        return int(resp) if resp else 0
    except Exception:
        return 0


def execute_order(client, order, dry_run=False):
    """Execute a single order with fee retry."""
    action = order["action"]
    token_id = order["token_id"]
    price = order["price"]
    outcome = order["outcome"]
    market = order["market"]

    if action == "SELL":
        shares = order["shares"]
        label = f"SELL {shares:.1f} {outcome} @ {price:.2f} → ${order['proceeds']:.2f}"
    else:
        shares = order["shares"]
        label = f"BUY  {shares:.1f} {outcome} @ {price:.2f} = ${order['cost']:.2f}"

    short_title = market[:50]
    print(f"  {label} | {short_title}", end="", flush=True)

    if dry_run:
        print(" → DRY RUN")
        return True

    fee_bps = get_fee_bps(client, token_id)
    place_fn = place_sell if action == "SELL" else place_buy

    try:
        resp = place_fn(client, token_id, price, shares, fee_bps)
        matched = resp.get("size_matched", "0")
        status = resp.get("status", "?")
        if float(matched) > 0 or "matched" in str(status).lower():
            print(f" → FILLED (matched={matched})")
            return True
        else:
            print(f" → {status} (matched={matched})")
            return False
    except Exception as e:
        err = str(e)
        # Retry with correct fee
        fee_match = re.search(r'(?:taker|maker) fee: (\d+)', err)
        if fee_match:
            correct_fee = int(fee_match.group(1))
            print(f" → fee retry ({correct_fee})...", end="", flush=True)
            try:
                resp = place_fn(client, token_id, price, shares, correct_fee)
                matched = resp.get("size_matched", "0")
                if float(matched) > 0:
                    print(f" FILLED (matched={matched})")
                    return True
                else:
                    print(f" {resp.get('status', '?')}")
                    return False
            except Exception as e2:
                print(f" FAILED: {e2}")
                return False
        else:
            print(f" → FAILED: {err[:80]}")
            return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Execute sell+buy corrections")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--trades-file", default="/tmp/paper_trades.json", help="Paper trades JSON")
    args = parser.parse_args()

    trades = load_trades(args.trades_file)
    sells = trades.get("sells", [])
    buys = trades.get("buys", [])

    print(f"Loaded {len(sells)} sells + {len(buys)} buys from {args.trades_file}")
    print()

    if not args.dry_run:
        print("Initializing CLOB client...")
        client = init_client()
        print("Client ready.\n")
    else:
        client = None

    # Execute sells first
    sell_ok = 0
    sell_fail = 0
    if sells:
        print(f"{'='*80}")
        print(f"STAP 1: SELLS ({len(sells)} orders)")
        print(f"{'='*80}")
        for order in sells:
            if execute_order(client, order, args.dry_run):
                sell_ok += 1
            else:
                sell_fail += 1
            time.sleep(0.5)

    # Execute buys
    buy_ok = 0
    buy_fail = 0
    if buys:
        print(f"\n{'='*80}")
        print(f"STAP 2+3: BUYS ({len(buys)} orders)")
        print(f"{'='*80}")
        for order in buys:
            if execute_order(client, order, args.dry_run):
                buy_ok += 1
            else:
                buy_fail += 1
            time.sleep(0.5)

    # Summary
    total_sell = sum(s["proceeds"] for s in sells)
    total_buy = sum(b["cost"] for b in buys)
    print(f"\n{'='*80}")
    print(f"RESULTAAT")
    print(f"{'='*80}")
    print(f"  Sells: {sell_ok} OK, {sell_fail} failed → ${total_sell:.2f}")
    print(f"  Buys:  {buy_ok} OK, {buy_fail} failed → ${total_buy:.2f}")
    print(f"  Netto: ${total_sell - total_buy:+.2f}")


if __name__ == "__main__":
    main()
