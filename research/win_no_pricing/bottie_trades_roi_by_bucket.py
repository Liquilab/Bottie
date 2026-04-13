#!/usr/bin/env python3
"""
Realized ROI per (sport, leg, price bucket) from Bottie's OWN production trades.

Bias-free alternative to cannae_closed_full.csv (which has football survivorship gap).
Source: /opt/bottie/data/trades.jsonl (pulled to /tmp/vps_trades.jsonl).

Each Bottie trade is a real bet with a real outcome — losses included.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

SRC = Path("/tmp/vps_trades.jsonl")

PRICE_BUCKETS = [
    (0.00, 0.35, "0.00-0.35"),
    (0.35, 0.50, "0.35-0.50"),
    (0.50, 0.65, "0.50-0.65"),
    (0.65, 0.75, "0.65-0.75"),
    (0.75, 0.85, "0.75-0.85"),
    (0.85, 1.01, "0.85+    "),
]


def bucket_for(price: float) -> str:
    for lo, hi, label in PRICE_BUCKETS:
        if lo <= price < hi:
            return label
    return "??"


def classify(title: str, outcome: str) -> tuple[str, str]:
    t = (title or "").lower()
    o = (outcome or "").strip().lower()
    if "draw" in t and t.startswith("will "):
        return "FOOTBALL", ("DRAW_YES" if o == "yes" else "DRAW_NO")
    if t.startswith("will ") and " win" in t and o in ("yes", "no"):
        return "FOOTBALL", ("WIN_YES" if o == "yes" else "WIN_NO")
    if t.startswith("spread:") or "spread" in t:
        return "UNK", "SPREAD"
    if "o/u" in t or "over/under" in t or o in ("over", "under"):
        return "UNK", "OU"
    if (" vs. " in t or " vs " in t) and o not in ("yes", "no"):
        return "NBA", "WIN"
    return "OTHER", "OTHER"


def main():
    rows = []
    with SRC.open() as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not t.get("filled"):
                continue
            if t.get("dry_run"):
                continue
            if t.get("resolved_at") is None:
                continue  # unresolved — exclude from ROI calc
            price = float(t.get("price") or 0)
            stake = float(t.get("size_usdc") or 0)
            pnl = t.get("actual_pnl")
            if pnl is None:
                pnl = t.get("pnl")
            if pnl is None or stake <= 0 or price <= 0:
                continue
            pnl = float(pnl)
            result = (t.get("result") or "").lower()
            sport, leg = classify(t.get("market_title", ""), t.get("outcome", ""))
            rows.append({
                "sport": sport,
                "leg": leg,
                "price": price,
                "stake": stake,
                "pnl": pnl,
                "won": result == "win",
            })

    if not rows:
        print("No resolved Bottie trades found", file=sys.stderr)
        sys.exit(1)

    agg = defaultdict(lambda: {"n": 0, "wins": 0, "stake": 0.0, "pnl": 0.0})
    for r in rows:
        if r["leg"] in ("OTHER", "BTTS"):
            continue
        key = (r["sport"], r["leg"], bucket_for(r["price"]))
        a = agg[key]
        a["n"] += 1
        a["wins"] += int(r["won"])
        a["stake"] += r["stake"]
        a["pnl"] += r["pnl"]

    leg_order = ["WIN_YES", "WIN_NO", "WIN", "DRAW_YES", "DRAW_NO", "SPREAD", "OU"]
    bucket_order = [b[2] for b in PRICE_BUCKETS]

    print(f"\nBottie own-trades realized ROI per (sport, leg, price bucket)")
    print(f"Source: {SRC}  ({len(rows)} resolved trades)\n")
    print(f"{'sport':<9} {'leg':<9} {'bucket':<10} {'n':>4} {'WR':>6} "
          f"{'stake':>10} {'pnl':>10} {'ROI':>8}  break-even  bias-flag")
    print("-" * 95)

    for sport in ("FOOTBALL", "NBA", "UNK"):
        for leg in leg_order:
            any_row = False
            for bucket in bucket_order:
                a = agg.get((sport, leg, bucket))
                if not a or a["n"] == 0:
                    continue
                any_row = True
                wr = a["wins"] / a["n"]
                roi = a["pnl"] / a["stake"] if a["stake"] > 0 else 0.0
                mid = next((lo + (hi - lo) / 2 for lo, hi, lbl in PRICE_BUCKETS
                            if lbl == bucket), 0)
                # Survivorship bias guardrail (Stap C from earlier recommendation)
                bias_flag = ""
                if a["n"] >= 20 and wr > 0.95:
                    bias_flag = " ⚠ BIAS?"
                print(f"{sport:<9} {leg:<9} {bucket:<10} {a['n']:>4} "
                      f"{wr*100:>5.1f}% ${a['stake']:>9,.0f} "
                      f"${a['pnl']:>9,.2f} {roi*100:>+7.1f}%  WR>{mid*100:>4.0f}%{bias_flag}")
            if any_row:
                print()


if __name__ == "__main__":
    main()
