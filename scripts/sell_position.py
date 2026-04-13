#!/usr/bin/env python3
"""Sell a position on Polymarket CLOB.

Usage: python3 sell_position.py <token_id> <shares> [min_price]

Uses py-clob-client with credentials from environment (loaded via .env).
Returns JSON: {"ok": true, "order_id": "...", "filled": N} or {"ok": false, "error": "..."}
"""
import os, sys, json

def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "Usage: sell_position.py <token_id> <shares> [min_price]"}))
        sys.exit(1)

    token_id = sys.argv[1]
    shares = float(sys.argv[2])
    min_price = float(sys.argv[3]) if len(sys.argv) > 3 else 0.01

    pk = os.environ.get("PRIVATE_KEY", "")

    if not pk:
        print(json.dumps({"ok": False, "error": "Missing PRIVATE_KEY in environment"}))
        sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            signature_type=2,
            funder="0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42",
        )
        # Derive API creds from private key (POLY_API_KEY etc are empty in .env)
        client.set_api_creds(client.create_or_derive_api_creds())

        # Get best bid to determine sell price
        book = client.get_order_book(token_id)
        bids = book.bids or []
        if not bids:
            print(json.dumps({"ok": False, "error": "No bids in orderbook"}))
            sys.exit(1)

        best_bid = float(bids[0].price)
        if best_bid < min_price:
            print(json.dumps({"ok": False, "error": f"Best bid {best_bid:.3f} below min_price {min_price:.3f}"}))
            sys.exit(1)

        # Place sell order at best bid (FOK = immediate fill or cancel)
        order_args = OrderArgs(
            token_id=token_id,
            price=best_bid,
            size=shares,
            side="SELL",
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.FOK)

        order_id = resp.get("orderID", "")
        success = resp.get("success", False)

        if success:
            usdc = best_bid * shares
            print(json.dumps({
                "ok": True,
                "order_id": order_id,
                "price": best_bid,
                "shares": shares,
                "usdc": round(usdc, 2),
            }))
        else:
            print(json.dumps({"ok": False, "error": f"Order rejected: {resp}"}))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
