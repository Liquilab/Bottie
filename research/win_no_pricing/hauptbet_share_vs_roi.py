#!/usr/bin/env python3
"""
For each Bottie executed trade, find Cannae's hauptbet-share at execution time
(from the T5/T1 POSITIONS block in journalctl just before EXECUTE), then bucket
trades by share and compute realized ROI per bucket.

Inputs:
  /tmp/bottie_logs.txt   — pulled journalctl since 2026-04-01
  /tmp/vps_trades.jsonl  — pulled trades.jsonl
"""
import json
import re
from collections import defaultdict
from pathlib import Path

LOGS = Path("/tmp/bottie_logs.txt")
TRADES = Path("/tmp/vps_trades.jsonl")

# Strip ANSI escapes from journalctl colorized output
RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Patterns (after ANSI strip)
RE_HEADER = re.compile(
    r"INFO (T[51]) POSITIONS: (\S+) — (\d+) positions:")
RE_LEG = re.compile(
    r"INFO\s+(.+?)\s+\|\s+iv=\$([\d.]+)\s+cv=\$([\d.]+)")


def parse_positions_blocks():
    """Walk the log file. Whenever we hit a T5/T1 POSITIONS header, capture
    the next N legs and store them keyed by event_slug. Keep the LATEST block
    per slug (overwrite older ones), since EXECUTE uses the most recent
    snapshot."""
    latest = {}  # slug -> {"phase":..., "legs":[(title, cv)]}
    with LOGS.open() as f:
        lines = [RE_ANSI.sub("", l) for l in f.readlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        m = RE_HEADER.search(line)
        if m:
            phase, slug, n = m.group(1), m.group(2), int(m.group(3))
            legs = []
            j = i + 1
            consumed = 0
            while j < len(lines) and consumed < n:
                lm = RE_LEG.search(lines[j])
                if lm:
                    legs.append((lm.group(1).strip(), float(lm.group(3))))
                    consumed += 1
                j += 1
            latest[slug] = {"phase": phase, "legs": legs}
            i = j
            continue
        i += 1
    return latest


def main():
    snapshots = parse_positions_blocks()
    print(f"Parsed {len(snapshots)} unique game snapshots from logs")

    # Load trades
    trades = []
    with TRADES.open() as f:
        for line in f:
            t = json.loads(line)
            if not t.get("filled") or t.get("dry_run"):
                continue
            if t.get("resolved_at") is None:
                continue
            slug = t.get("event_slug") or ""
            if not slug or slug not in snapshots:
                continue
            stake = float(t.get("size_usdc") or 0)
            pnl = t.get("actual_pnl") if t.get("actual_pnl") is not None else t.get("pnl")
            if pnl is None or stake <= 0:
                continue
            trades.append({
                "slug": slug,
                "stake": stake,
                "pnl": float(pnl),
                "won": t.get("result") == "win",
                "title": t.get("market_title", ""),
                "outcome": t.get("outcome", ""),
                "price": t.get("price", 0),
            })

    print(f"Matched {len(trades)} resolved Bottie trades to a Cannae snapshot\n")

    # Compute hauptbet share per trade
    BUCKETS = [
        (0.00, 0.40, "<40%"),
        (0.40, 0.50, "40-50%"),
        (0.50, 0.60, "50-60%"),
        (0.60, 0.70, "60-70%"),
        (0.70, 0.80, "70-80%"),
        (0.80, 0.90, "80-90%"),
        (0.90, 1.01, "90+ %"),
    ]
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "stake": 0.0, "pnl": 0.0, "trades": []})

    for tr in trades:
        snap = snapshots[tr["slug"]]
        legs = snap["legs"]
        if not legs:
            continue
        total = sum(cv for _, cv in legs)
        if total <= 0:
            continue
        biggest = max(cv for _, cv in legs)
        share = biggest / total
        bucket = next((lbl for lo, hi, lbl in BUCKETS if lo <= share < hi), "??")
        a = agg[bucket]
        a["n"] += 1
        a["wins"] += int(tr["won"])
        a["stake"] += tr["stake"]
        a["pnl"] += tr["pnl"]
        a["trades"].append((share, tr))

    print(f"{'bucket':<9} {'n':>4} {'WR':>6} {'stake':>10} {'pnl':>10} {'ROI':>8}")
    print("-" * 60)
    cum_n = 0
    cum_stake = 0.0
    cum_pnl = 0.0
    for lo, hi, label in BUCKETS:
        a = agg.get(label)
        if not a or a["n"] == 0:
            continue
        wr = a["wins"] / a["n"] * 100
        roi = a["pnl"] / a["stake"] * 100 if a["stake"] else 0
        print(f"{label:<9} {a['n']:>4} {wr:>5.1f}% ${a['stake']:>9,.0f} "
              f"${a['pnl']:>9,.2f} {roi:>+7.1f}%")
        cum_n += a["n"]
        cum_stake += a["stake"]
        cum_pnl += a["pnl"]
    print("-" * 60)
    print(f"{'TOTAL':<9} {cum_n:>4} {'':>6} ${cum_stake:>9,.0f} "
          f"${cum_pnl:>9,.2f} {cum_pnl/cum_stake*100 if cum_stake else 0:+7.1f}%")

    # Cumulative pass at threshold (what would happen if we filtered)
    print(f"\nCumulative ROI for trades passing threshold (≥X%):")
    for thr in (0.40, 0.50, 0.60, 0.70, 0.80):
        passing = [tr for b, ad in agg.items() for s, tr in ad["trades"]
                   if any(s >= thr for s, _ in [(s, tr)])]
        # Recompute properly
        passing_trades = []
        for ad in agg.values():
            for s, tr in ad["trades"]:
                if s >= thr:
                    passing_trades.append(tr)
        if not passing_trades:
            continue
        n = len(passing_trades)
        s = sum(t["stake"] for t in passing_trades)
        p = sum(t["pnl"] for t in passing_trades)
        wr = sum(int(t["won"]) for t in passing_trades) / n * 100
        roi = p / s * 100 if s else 0
        print(f"  ≥{thr*100:>3.0f}%:  n={n:>3}  WR={wr:>5.1f}%  "
              f"stake=${s:>8,.0f}  pnl=${p:>9,.2f}  ROI={roi:+6.1f}%")

    # Sport split
    print(f"\nPer-sport breakdown of all matched trades:")
    sport_agg = defaultdict(lambda: {"n": 0, "stake": 0.0, "pnl": 0.0})
    for ad in agg.values():
        for s, tr in ad["trades"]:
            slug = tr["slug"]
            if slug.startswith("nba-"):
                sp = "NBA"
            elif slug.startswith(("mlb-", "nhl-", "nfl-")):
                sp = "US"
            else:
                sp = "FOOTBALL"
            sa = sport_agg[sp]
            sa["n"] += 1
            sa["stake"] += tr["stake"]
            sa["pnl"] += tr["pnl"]
    for sp, sa in sport_agg.items():
        roi = sa["pnl"] / sa["stake"] * 100 if sa["stake"] else 0
        print(f"  {sp:<10} n={sa['n']:>3}  stake=${sa['stake']:>8,.0f}  "
              f"pnl=${sa['pnl']:>9,.2f}  ROI={roi:+6.1f}%")


if __name__ == "__main__":
    main()
