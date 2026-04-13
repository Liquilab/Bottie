#!/usr/bin/env python3
"""Realized ROI on Bottie's actual fills, filtered to the council-recommended subset:
- Whitelist leagues (sport/league derived from event_slug or sport field)
- Exclude EPL, LAL, MLB
- Split by WIN_NO vs WIN_YES vs DRAW vs other
- Show overall, per league, per leg-type

Reads /opt/bottie/data/trades.jsonl on the VPS.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime

PATH = "/opt/bottie/data/trades.jsonl"

WHITELIST = {
    "epl","bun","lal","fl1","uel","arg","rou1","efa","por","bra","itc",
    "ere","es2","bl2","elc","mex","fr2","spl","efl","tur","acn","cde",
    "nba","mlb","nfl","aus","fif",
}
BLACKLIST = {"epl", "lal", "mlb"}

def league_of(t):
    slug = (t.get("event_slug") or "").replace("-more-markets", "")
    if slug:
        return slug.split("-", 1)[0]
    return ""

def leg_type(t):
    title = (t.get("market_title") or "").lower()
    outcome = (t.get("outcome") or "").lower()
    if "end in a draw" in title:
        return f"DRAW_{outcome.upper()}"
    if "win on" in title:
        return f"WIN_{outcome.upper()}"
    if "o/u" in title or "over/under" in title:
        return "O/U"
    if "spread" in title:
        return "SPREAD"
    return "OTHER"

def fmt(label, n, w, l, stake, pnl):
    wr = w / (w + l) * 100 if (w + l) else 0
    roi = pnl / stake * 100 if stake else 0
    return f"{label:<24}{n:>6}{w:>6}{l:>6}{wr:>7.1f}%{stake:>13,.0f}{pnl:>+13,.0f}{roi:>+9.2f}%"

trades = []
with open(PATH) as f:
    for line in f:
        try:
            t = json.loads(line)
        except Exception:
            continue
        if t.get("dry_run"):
            continue
        if not t.get("filled"):
            continue
        if t.get("result") not in ("win", "loss"):
            continue
        trades.append(t)

print(f"Total resolved Bottie trades: {len(trades)}")
print(f"Date range: {trades[0]['timestamp'][:10]} → {trades[-1]['timestamp'][:10]}")
print()

# Apply whitelist
in_wl = [t for t in trades if league_of(t) in WHITELIST]
print(f"In whitelist:                     {len(in_wl)}")

# Apply blacklist (subset)
sub = [t for t in in_wl if league_of(t) not in BLACKLIST]
print(f"After excluding EPL/LAL/MLB:      {len(sub)}")
print()

def aggregate(rows):
    n = len(rows)
    w = sum(1 for t in rows if t["result"] == "win")
    l = sum(1 for t in rows if t["result"] == "loss")
    stake = sum(float(t.get("size_usdc") or 0) for t in rows)
    pnl = sum(float(t.get("pnl") or 0) for t in rows)
    return n, w, l, stake, pnl

print("=" * 90)
print("BOTTIE REALIZED ROI — actual fills, real PnL")
print("=" * 90)
print(f"{'Group':<24}{'N':>6}{'W':>6}{'L':>6}{'WR':>8}{'Stake':>13}{'PnL':>13}{'ROI':>10}")
print("-" * 90)

print(fmt("ALL resolved", *aggregate(trades)))
print(fmt("In whitelist", *aggregate(in_wl)))
print(fmt("Subset (-EPL/LAL/MLB)", *aggregate(sub)))
print()

# Per leg-type within subset
print("--- Per leg-type (subset) ---")
by_type = defaultdict(list)
for t in sub:
    by_type[leg_type(t)].append(t)
for lt in sorted(by_type, key=lambda k: -aggregate(by_type[k])[4]):
    print(fmt(lt, *aggregate(by_type[lt])))
print()

# Per league within subset
print("--- Per league (subset, sorted by PnL) ---")
by_lg = defaultdict(list)
for t in sub:
    by_lg[league_of(t)].append(t)
rows = sorted(by_lg.items(), key=lambda kv: -aggregate(kv[1])[4])
for lg, ts in rows:
    print(fmt(lg, *aggregate(ts)))
print()

# Compare blacklisted leagues — did they actually lose for Bottie too?
print("--- Excluded leagues (sanity check) ---")
for lg in ("epl", "lal", "mlb"):
    rows = [t for t in trades if league_of(t) == lg]
    if rows:
        print(fmt(lg, *aggregate(rows)))
print()

# Council subset: WIN_NO solo + WIN_YES_with_draw
# For WIN_YES we need to know if Bottie also took a DRAW on same game
by_game = defaultdict(list)
for t in sub:
    by_game[t.get("event_slug","")].append(t)

council_trades = []
for slug, gl in by_game.items():
    has_draw = any(leg_type(t).startswith("DRAW") for t in gl)
    for t in gl:
        lt = leg_type(t)
        if lt == "WIN_NO":
            council_trades.append(t)
        elif lt == "WIN_YES" and has_draw:
            council_trades.append(t)
        elif lt.startswith("DRAW") and any(leg_type(x) == "WIN_YES" for x in gl):
            council_trades.append(t)

print("=" * 90)
print("COUNCIL SUBSET — WIN_NO solo + WIN_YES-with-DRAW (subset only)")
print("=" * 90)
print(f"{'Group':<24}{'N':>6}{'W':>6}{'L':>6}{'WR':>8}{'Stake':>13}{'PnL':>13}{'ROI':>10}")
print("-" * 90)
print(fmt("Council subset", *aggregate(council_trades)))

# Split council subset by leg-type
by_t2 = defaultdict(list)
for t in council_trades:
    by_t2[leg_type(t)].append(t)
for lt in sorted(by_t2, key=lambda k: -aggregate(by_t2[k])[4]):
    print(fmt(f"  {lt}", *aggregate(by_t2[lt])))
