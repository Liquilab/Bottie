#!/usr/bin/env python3
"""
Correct wallet profit calculation using Polymarket's lb-api.

The positions API has SURVIVORSHIP BIAS:
- Redeemed winners vanish from /positions
- Only unredeemed losers + unredeemed winners + active positions are visible
- This makes profit look catastrophically negative

The lb-api.polymarket.com/profit endpoint is the SOURCE OF TRUTH.
It calculates: current_value + total_redeemed - total_cost (across all positions ever).

Usage:
  python3 scripts/wallet_profit.py                    # Check Cannae + Bottie
  python3 scripts/wallet_profit.py 0xADDRESS          # Check specific wallet
"""

import json
import sys

import httpx

LB_API = "https://lb-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

# Default wallets
WALLETS = {
    "Cannae": "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b",
    "Bottie": "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15",
}


def get_profit(client: httpx.Client, address: str) -> dict:
    """Get true profit from lb-api (source of truth)."""
    resp = client.get(f"{LB_API}/profit?address={address}", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data and isinstance(data, list):
        return data[0]
    return {}


def get_volume(client: httpx.Client, address: str) -> float:
    """Get total trading volume."""
    resp = client.get(f"{LB_API}/volume?address={address}", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data and isinstance(data, list):
        return float(data[0].get("amount", 0))
    return 0.0


def get_portfolio_value(client: httpx.Client, address: str) -> float:
    """Get current portfolio value (open positions only)."""
    resp = client.get(f"{DATA_API}/value?user={address}", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data and isinstance(data, list):
        return float(data[0].get("value", 0))
    return 0.0


def analyze_wallet(client: httpx.Client, name: str, address: str):
    """Print correct profit analysis for a wallet."""
    profile = get_profit(client, address)
    profit = float(profile.get("amount", 0))
    pseudonym = profile.get("pseudonym", name)
    volume = get_volume(client, address)
    portfolio = get_portfolio_value(client, address)

    print(f"\n{'='*50}")
    print(f"{pseudonym} ({address[:10]}...)")
    print(f"{'='*50}")
    print(f"  Profit (lb-api):     ${profit:>12,.2f}")
    print(f"  Portfolio value:     ${portfolio:>12,.2f}")
    print(f"  Volume:              ${volume:>12,.2f}")
    if volume > 0:
        # ROI on volume is not meaningful (counts both sides)
        # Better: ROI on capital deployed ≈ volume/2
        roi = profit / (volume / 2) * 100
        print(f"  ROI (profit/capital): {roi:>11.2f}%")
    print()


def main():
    if len(sys.argv) > 1:
        addr = sys.argv[1]
        wallets = {"Custom": addr}
    else:
        wallets = WALLETS

    with httpx.Client(headers=HEADERS) as client:
        for name, addr in wallets.items():
            try:
                analyze_wallet(client, name, addr)
            except Exception as e:
                print(f"Error for {name}: {e}")


if __name__ == "__main__":
    main()
