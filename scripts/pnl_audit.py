#!/usr/bin/env python3
"""
PnL Audit — Demonstrates why positions API gives WRONG PnL
and shows the CORRECT approach using lb-api.

ROOT CAUSE: Polymarket's /positions endpoint only returns UNREDEEMED positions.
When a winner is redeemed (shares → USDC), the position DISAPPEARS completely.
This means summing cashPnl or (currentValue - initialValue) across positions
only counts losses (which stay visible) but misses most winners (which vanish).

CORRECT APPROACH: Use https://lb-api.polymarket.com/profit?address=<addr>
This is Polymarket's own profit calculation and accounts for all activity.

Usage:
  python3 scripts/pnl_audit.py                # Audit Cannae + Bottie
  python3 scripts/pnl_audit.py 0xADDRESS      # Audit specific wallet
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
BOTTIE = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"


def fetch_all_positions(client, address, size_threshold=0.01):
    """Paginate through ALL positions with size > threshold."""
    all_pos = []
    offset = 0
    while True:
        url = f"{DATA_API}/positions?user={address}&limit=500&offset={offset}&sizeThreshold={size_threshold}"
        resp = client.get(url, timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_pos.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(0.2)
    return all_pos


def audit_wallet(client, address, name):
    """Compare WRONG (positions API) vs CORRECT (lb-api) PnL."""
    print(f"\n{'='*60}")
    print(f"  {name}: {address}")
    print(f"{'='*60}")

    # === CORRECT: lb-api ===
    resp = client.get(f"{LB_API}/profit?address={address}", timeout=15)
    lb_data = resp.json()
    true_profit = float(lb_data[0]["amount"]) if lb_data else 0.0
    pseudonym = lb_data[0].get("pseudonym", name) if lb_data else name

    resp = client.get(f"{LB_API}/volume?address={address}", timeout=15)
    vol_data = resp.json()
    volume = float(vol_data[0]["amount"]) if vol_data else 0.0

    resp = client.get(f"{DATA_API}/value?user={address}", timeout=15)
    val_data = resp.json()
    portfolio = float(val_data[0]["value"]) if val_data else 0.0

    print(f"\n  CORRECT (lb-api, source of truth):")
    print(f"    Profit:          ${true_profit:>12,.2f}")
    print(f"    Portfolio value:  ${portfolio:>12,.2f}")
    print(f"    Volume:          ${volume:>12,.2f}")
    if volume > 0:
        print(f"    ROI (on capital): {true_profit/(volume/2)*100:>11.2f}%")

    # === WRONG: positions API ===
    positions = fetch_all_positions(client, address)
    if not positions:
        print(f"\n  No positions found (all redeemed).")
        return

    total_initial = sum(float(p.get("initialValue", 0) or 0) for p in positions)
    total_current = sum(float(p.get("currentValue", 0) or 0) for p in positions)
    total_cashpnl = sum(float(p.get("cashPnl", 0) or 0) for p in positions)

    # Classify
    losers = sum(1 for p in positions if float(p.get("curPrice", 0) or 0) <= 0.005)
    winners = sum(1 for p in positions if float(p.get("curPrice", 0) or 0) >= 0.99)
    active = len(positions) - losers - winners

    print(f"\n  WRONG (positions API, survivorship bias):")
    print(f"    Positions visible:  {len(positions)}")
    print(f"      Losers (curPrice=0): {losers}")
    print(f"      Winners (curPrice=1): {winners}")
    print(f"      Active: {active}")
    print(f"    Sum cashPnl:     ${total_cashpnl:>12,.2f}")
    print(f"    initialValue:    ${total_initial:>12,.2f}")
    print(f"    currentValue:    ${total_current:>12,.2f}")

    # Error magnitude
    error = total_cashpnl - true_profit
    print(f"\n  ERROR MAGNITUDE:")
    print(f"    Wrong PnL:    ${total_cashpnl:>12,.2f}")
    print(f"    Correct PnL:  ${true_profit:>12,.2f}")
    print(f"    Difference:   ${error:>12,.2f}")
    if total_initial > 0:
        print(f"    Wrong ROI:    {total_cashpnl/total_initial*100:>11.2f}%")
    if volume > 0:
        print(f"    Correct ROI:  {true_profit/(volume/2)*100:>11.2f}%")


def main():
    if len(sys.argv) > 1:
        wallets = {"Custom": sys.argv[1]}
    else:
        wallets = {"Cannae": CANNAE, "Bottie": BOTTIE}

    with httpx.Client(headers=HEADERS) as client:
        for name, addr in wallets.items():
            try:
                audit_wallet(client, addr, name)
            except Exception as e:
                print(f"Error for {name}: {e}")

    print(f"\n{'='*60}")
    print("  CONCLUSION: ALWAYS use lb-api.polymarket.com/profit")
    print("  NEVER sum cashPnl from positions API for total PnL")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
