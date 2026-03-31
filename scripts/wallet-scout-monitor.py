#!/usr/bin/env python3
"""Wallet Scout — Daily monitor with unbiased hauptbet analysis.

All analysis logic lives in lib/analyse.py (single source of truth).
This script only handles: wallet list, output formatting, file saving.
"""

import json, os, sys
from datetime import datetime, timezone

# Import canonical analysis library
sys.path.insert(0, os.path.dirname(__file__))
from lib.analyse import analyse_wallet, classify_sport

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "wallet-scout")

WATCHLIST = [
    {"name": "beachboy4", "address": "0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "sport": "football", "status": "watching"},
    {"name": "ewelmealt", "address": "0x07921379f7b31ef93da634b688b2fe36897db778", "sport": "football", "status": "live_1pct"},
    {"name": "Blessed-Sunshine", "address": "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09", "sport": "nba", "status": "watching"},
    {"name": "CERTuo", "address": "0xf195721ad850377c96cd634457c70cd9e8308057", "sport": "nhl", "status": "watching"},
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = []
    for w in WATCHLIST:
        print(f"Scanning {w['name']}...", flush=True)
        try:
            r = analyse_wallet(w["address"], w["sport"])
            r["name"] = w["name"]
            r["address"] = w["address"]
            r["sport"] = w["sport"]
            r["status"] = w["status"]

            hb = r["hauptbet"]
            gap = f" (lb-gap: {r['sanity_gap_pct']}%)" if r.get("sanity_gap_pct") is not None else ""
            print(f"  {w['sport']}: {hb['games']} games | HB WR={hb['wr']}% | HB ROI={hb['roi']}%{gap}")
            for line, ls in hb.get("per_line", {}).items():
                print(f"    {line}: {ls['games']}g WR={ls['wr']}% ROI={ls['roi']}%")

            r["activation_ready"] = hb["games"] >= 100 and hb["wr"] > 52 and r.get("lb_api_total_pnl", 0) and r["lb_api_total_pnl"] > 0
            r["inactive_alert"] = r["active"] == 0
            results.append(r)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"name": w["name"], "error": str(e)})

    output = {
        "date": today,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wallets": results,
    }

    outpath = os.path.join(OUT_DIR, f"monitor-{today}.json")
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {outpath}")

    latest = os.path.join(OUT_DIR, "latest-monitor.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2)


if __name__ == "__main__":
    main()
