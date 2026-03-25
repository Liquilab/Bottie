#!/usr/bin/env python3
"""Analyze Cannae's USDC deposits and withdrawals on Polygon using Blockscout API."""

import json
import requests
import time
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlencode

CANNAE_ADDRESS = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b".lower()
BASE_URL = f"https://polygon.blockscout.com/api/v2/addresses/{CANNAE_ADDRESS}/token-transfers"

USDC_CONTRACTS = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e (bridged)
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC (native)
}

# Known Polymarket-related addresses (exchanges, proxies)
# We want to identify which transfers are external deposits vs Polymarket trading
POLYMARKET_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"  # CTF Exchange
POLYMARKET_NEG_RISK = "0xc5d563a36ae78145c45a50134d48a1215220f80a"  # NegRiskCtfExchange


def fetch_all_usdc_transfers():
    """Fetch all ERC-20 token transfers and filter for USDC."""
    all_transfers = []
    url = BASE_URL
    params = {"type": "ERC-20"}
    page = 0

    while True:
        page += 1
        if page > 1:
            time.sleep(0.3)  # Rate limit

        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()

        items = data.get("items", [])
        if not items:
            break

        # Filter for USDC tokens
        for item in items:
            token = item.get("token", {})
            token_addr = token.get("address", "").lower()
            if token_addr in USDC_CONTRACTS:
                all_transfers.append(item)

        print(f"  Page {page}: {len(items)} transfers fetched, {len(all_transfers)} USDC total")

        # Pagination
        next_params = data.get("next_page_params")
        if not next_params:
            break

        params = {"type": "ERC-20", **next_params}

    return all_transfers


def parse_transfer(item):
    """Parse a Blockscout transfer item into a clean dict."""
    token = item.get("token", {})
    decimals = int(token.get("decimals", "6"))
    raw_value = int(item.get("total", {}).get("value", "0"))
    value = raw_value / (10 ** decimals)

    from_addr = item.get("from", {}).get("hash", "").lower()
    to_addr = item.get("to", {}).get("hash", "").lower()

    timestamp = item.get("timestamp", "")
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None

    tx_hash = item.get("tx_hash", "")
    token_symbol = token.get("symbol", "?")

    return {
        "date": dt,
        "value": value,
        "hash": tx_hash,
        "token": token_symbol,
        "from": from_addr,
        "to": to_addr,
    }


def is_polymarket_address(addr):
    """Check if address is a known Polymarket contract."""
    return addr in (POLYMARKET_EXCHANGE, POLYMARKET_NEG_RISK)


def analyze():
    print("Fetching all USDC transfers for Cannae...")
    raw_transfers = fetch_all_usdc_transfers()
    print(f"\nTotal USDC transfers found: {len(raw_transfers)}")

    deposits = []  # USDC received by Cannae
    withdrawals = []  # USDC sent by Cannae

    for item in raw_transfers:
        entry = parse_transfer(item)
        if entry["value"] == 0:
            continue

        if entry["to"] == CANNAE_ADDRESS:
            deposits.append(entry)
        elif entry["from"] == CANNAE_ADDRESS:
            withdrawals.append(entry)

    # Sort chronologically
    deposits.sort(key=lambda x: x["date"] if x["date"] else datetime.min)
    withdrawals.sort(key=lambda x: x["date"] if x["date"] else datetime.min)

    # Separate trading transfers from external deposits/withdrawals
    external_deposits = [d for d in deposits if not is_polymarket_address(d["from"])]
    trading_deposits = [d for d in deposits if is_polymarket_address(d["from"])]
    external_withdrawals = [w for w in withdrawals if not is_polymarket_address(w["to"])]
    trading_withdrawals = [w for w in withdrawals if is_polymarket_address(w["to"])]

    total_deposited = sum(d["value"] for d in deposits)
    total_withdrawn = sum(w["value"] for w in withdrawals)
    total_ext_deposited = sum(d["value"] for d in external_deposits)
    total_ext_withdrawn = sum(w["value"] for w in external_withdrawals)
    total_trading_in = sum(d["value"] for d in trading_deposits)
    total_trading_out = sum(w["value"] for w in trading_withdrawals)

    # === EXTERNAL DEPOSITS ===
    print("\n" + "=" * 110)
    print("EXTERNAL USDC DEPOSITS (non-Polymarket sources) >= $50")
    print("=" * 110)

    monthly_ext_dep = defaultdict(float)
    sig_ext_dep = [d for d in external_deposits if d["value"] >= 50]

    for d in sig_ext_dep:
        dt_str = d["date"].strftime("%Y-%m-%d %H:%M") if d["date"] else "unknown"
        mk = d["date"].strftime("%Y-%m") if d["date"] else "unknown"
        monthly_ext_dep[mk] += d["value"]
        print(f"  {dt_str}  ${d['value']:>12,.2f}  {d['token']:<8}  from: {d['from'][:14]}...  tx: {d['hash'][:20]}...")

    small = [d for d in external_deposits if d["value"] < 50]
    print(f"\n  Shown: {len(sig_ext_dep)} transfers >= $50")
    print(f"  Hidden: {len(small)} smaller transfers (total: ${sum(d['value'] for d in small):,.2f})")

    # === EXTERNAL WITHDRAWALS ===
    print("\n" + "=" * 110)
    print("EXTERNAL USDC WITHDRAWALS (non-Polymarket destinations) >= $50")
    print("=" * 110)

    monthly_ext_wit = defaultdict(float)
    sig_ext_wit = [w for w in external_withdrawals if w["value"] >= 50]

    for w in sig_ext_wit:
        dt_str = w["date"].strftime("%Y-%m-%d %H:%M") if w["date"] else "unknown"
        mk = w["date"].strftime("%Y-%m") if w["date"] else "unknown"
        monthly_ext_wit[mk] += w["value"]
        print(f"  {dt_str}  ${w['value']:>12,.2f}  {w['token']:<8}  to: {w['to'][:14]}...  tx: {w['hash'][:20]}...")

    small_w = [w for w in external_withdrawals if w["value"] < 50]
    print(f"\n  Shown: {len(sig_ext_wit)} transfers >= $50")
    print(f"  Hidden: {len(small_w)} smaller transfers (total: ${sum(w['value'] for w in small_w):,.2f})")

    # === MONTHLY SUMMARY ===
    all_months = sorted(set(list(monthly_ext_dep.keys()) + list(monthly_ext_wit.keys())))

    if all_months:
        print("\n" + "=" * 110)
        print("MONTHLY SUMMARY - EXTERNAL TRANSFERS (non-Polymarket)")
        print("=" * 110)
        print(f"  {'Month':<10} {'Deposits':>14} {'Withdrawals':>14} {'Net':>14}")
        print(f"  {'-'*10} {'-'*14} {'-'*14} {'-'*14}")
        for month in all_months:
            dep = monthly_ext_dep.get(month, 0)
            wit = monthly_ext_wit.get(month, 0)
            print(f"  {month:<10} ${dep:>12,.2f} ${wit:>12,.2f} ${dep - wit:>12,.2f}")

    # === GRAND TOTALS ===
    print("\n" + "=" * 110)
    print("TOTALS")
    print("=" * 110)
    print(f"\n  EXTERNAL (deposits/withdrawals from outside Polymarket):")
    print(f"    Deposited:         ${total_ext_deposited:>14,.2f}  ({len(external_deposits)} transfers)")
    print(f"    Withdrawn:         ${total_ext_withdrawn:>14,.2f}  ({len(external_withdrawals)} transfers)")
    print(f"    Net External:      ${total_ext_deposited - total_ext_withdrawn:>14,.2f}")

    print(f"\n  POLYMARKET TRADING (to/from CTF Exchange & NegRisk):")
    print(f"    Received (sells):  ${total_trading_in:>14,.2f}  ({len(trading_deposits)} transfers)")
    print(f"    Sent (buys):       ${total_trading_out:>14,.2f}  ({len(trading_withdrawals)} transfers)")
    print(f"    Net Trading P&L:   ${total_trading_in - total_trading_out:>14,.2f}")

    print(f"\n  ALL TRANSFERS:")
    print(f"    Total In:          ${total_deposited:>14,.2f}  ({len(deposits)} transfers)")
    print(f"    Total Out:         ${total_withdrawn:>14,.2f}  ({len(withdrawals)} transfers)")
    print(f"    Net:               ${total_deposited - total_withdrawn:>14,.2f}")

    # Show unique external counterparties
    print("\n" + "=" * 110)
    print("UNIQUE EXTERNAL COUNTERPARTIES")
    print("=" * 110)

    dep_sources = defaultdict(float)
    for d in external_deposits:
        dep_sources[d["from"]] += d["value"]

    wit_dests = defaultdict(float)
    for w in external_withdrawals:
        wit_dests[w["to"]] += w["value"]

    print("\n  Deposit sources:")
    for addr, total in sorted(dep_sources.items(), key=lambda x: -x[1]):
        if total >= 10:
            print(f"    {addr}  ${total:>14,.2f}")

    print("\n  Withdrawal destinations:")
    for addr, total in sorted(wit_dests.items(), key=lambda x: -x[1]):
        if total >= 10:
            print(f"    {addr}  ${total:>14,.2f}")


if __name__ == "__main__":
    analyze()
