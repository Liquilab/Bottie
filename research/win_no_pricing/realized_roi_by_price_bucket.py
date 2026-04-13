#!/usr/bin/env python3
"""
Realized ROI per (league, leg_type, entry_price_bucket) from Cannae closed positions.

Goal: validate the WIN_NO pricing thesis — at high prices the math breaks even
if WR is high. Show realized ROI per bucket so we can decide a max-price cap.

Data: research/cannae_trades/cannae_closed_full.csv
Output: stdout table.
"""
import csv
import re
from collections import defaultdict
from pathlib import Path

CSV = Path(__file__).resolve().parents[2] / "research/cannae_trades/cannae_closed_full.csv"

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
    """Return (sport, leg). Sport ∈ {FOOTBALL, NBA, OTHER}."""
    t = (title or "").lower()
    o = (outcome or "").strip()
    ol = o.lower()
    # Football draw: "Will X vs Y end in a draw?"
    if "draw" in t and t.startswith("will "):
        return "FOOTBALL", ("DRAW_YES" if ol == "yes" else "DRAW_NO")
    # Football WIN: "Will X win on YYYY-MM-DD?"
    if t.startswith("will ") and " win" in t and ol in ("yes", "no"):
        return "FOOTBALL", ("WIN_YES" if ol == "yes" else "WIN_NO")
    # Spread
    if t.startswith("spread:") or "spread" in t:
        # NBA spreads usually have team names; football too. Classify by sport later.
        return "UNK", "SPREAD"
    # O/U
    if "o/u" in t or "over/under" in t or ol in ("over", "under"):
        return "UNK", "OU"
    if "both teams to score" in t or "btts" in t:
        return "UNK", "BTTS"
    # NBA win: "Team A vs. Team B" with team-name outcome
    if (" vs. " in t or " vs " in t) and ol not in ("yes", "no"):
        return "NBA", "WIN"  # team pick (no yes/no semantic)
    return "OTHER", "OTHER"


def main():
    rows = []
    with CSV.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                price = float(r["avg_price"])
                bought = float(r["total_bought"])
                pnl = float(r["realized_pnl"])
            except (ValueError, KeyError):
                continue
            if bought <= 0 or price <= 0:
                continue
            sport, leg = classify(r.get("title", ""), r.get("outcome", ""))
            rows.append({
                "sport": sport,
                "leg": leg,
                "price": price,
                "bought": bought,
                "pnl": pnl,
                "won": r.get("won", "") == "1",
            })

    # Aggregate per (sport, leg, bucket). Sport=UNK rolls into both.
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "stake": 0.0, "pnl": 0.0})
    for r in rows:
        if r["leg"] in ("OTHER", "BTTS"):
            continue
        key = (r["sport"], r["leg"], bucket_for(r["price"]))
        a = agg[key]
        a["n"] += 1
        a["wins"] += int(r["won"])
        a["stake"] += r["bought"]
        a["pnl"] += r["pnl"]

    leg_order = ["WIN_YES", "WIN_NO", "WIN", "DRAW_YES", "DRAW_NO", "SPREAD", "OU"]
    bucket_order = [b[2] for b in PRICE_BUCKETS]

    print(f"\nRealized ROI per (sport, leg, price bucket) — Cannae closed positions")
    print(f"Source: {CSV.name}  ({len(rows)} valid rows)\n")
    print(f"{'sport':<9} {'leg':<9} {'bucket':<10} {'n':>5} {'WR':>6} "
          f"{'stake':>10} {'pnl':>11} {'ROI':>8}  break-even")
    print("-" * 88)

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
                print(f"{sport:<9} {leg:<9} {bucket:<10} {a['n']:>5} "
                      f"{wr*100:>5.1f}% ${a['stake']:>9,.0f} "
                      f"${a['pnl']:>10,.0f} {roi*100:>+7.1f}%  WR>{mid*100:>4.0f}%")
            if any_row:
                print()


if __name__ == "__main__":
    main()
