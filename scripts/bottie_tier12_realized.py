#!/usr/bin/env python3
"""Realized ROI on Bottie's actual fills, filtered to Tier 1+2 whitelist
(point-estimate ROI > +5% on Cannae 42d data, n>=8).
"""
import json
from collections import defaultdict

PATH = "/opt/bottie/data/trades.jsonl"

TIER12 = {
    "bra", "nba", "sea", "efa", "arg", "fl1", "elc", "bun",
    "uel", "tur", "por", "mex", "ere", "es2", "itc", "spl",
}
BLACKLIST = {"epl", "lal", "ucl", "uef", "mls", "mlb", "fif", "fr2", "chi", "aus"}

def league_of(t):
    slug = (t.get("event_slug") or "").replace("-more-markets", "")
    return slug.split("-", 1)[0] if slug else ""

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

trades = []
with open(PATH) as f:
    for line in f:
        try: t = json.loads(line)
        except: continue
        if t.get("dry_run"): continue
        if not t.get("filled"): continue
        if t.get("result") not in ("win", "loss"): continue
        trades.append(t)

print(f"Total resolved Bottie trades: {len(trades)}")
print(f"Date range: {trades[0]['timestamp'][:10]} → {trades[-1]['timestamp'][:10]}")
print()

def aggregate(rows):
    n = len(rows)
    w = sum(1 for t in rows if t["result"] == "win")
    l = sum(1 for t in rows if t["result"] == "loss")
    stake = sum(float(t.get("size_usdc") or 0) for t in rows)
    pnl = sum(float(t.get("pnl") or 0) for t in rows)
    return n, w, l, stake, pnl

def fmt(label, n, w, l, stake, pnl):
    wr = w/(w+l)*100 if (w+l) else 0
    roi = pnl/stake*100 if stake else 0
    return f"{label:<28}{n:>6}{w:>6}{l:>6}{wr:>7.1f}%{stake:>13,.0f}{pnl:>+13,.0f}{roi:>+9.2f}%"

tier12 = [t for t in trades if league_of(t) in TIER12]
old_subset = [t for t in trades if league_of(t) not in BLACKLIST and league_of(t) not in {"nfl"}]

print("=" * 95)
print("BOTTIE REALIZED ROI — Tier 1+2 vs old subset")
print("=" * 95)
print(f"{'Group':<28}{'N':>6}{'W':>6}{'L':>6}{'WR':>8}{'Stake':>13}{'PnL':>13}{'ROI':>10}")
print("-" * 95)
print(fmt("ALL resolved", *aggregate(trades)))
print(fmt("Old subset (-EPL/LAL/MLB)", *aggregate(old_subset)))
print(fmt("Tier 1+2 (16 leagues)", *aggregate(tier12)))
print()

# Per league within Tier 1+2
print("--- Per league (Tier 1+2) ---")
by_lg = defaultdict(list)
for t in tier12: by_lg[league_of(t)].append(t)
for lg, ts in sorted(by_lg.items(), key=lambda kv: -aggregate(kv[1])[4]):
    print(fmt(lg, *aggregate(ts)))
print()

# Per leg-type within Tier 1+2
print("--- Per leg-type (Tier 1+2) ---")
by_t = defaultdict(list)
for t in tier12: by_t[leg_type(t)].append(t)
for k in sorted(by_t, key=lambda x: -aggregate(by_t[x])[4]):
    print(fmt(k, *aggregate(by_t[k])))
print()

# Tier 1 only (bra + nba)
tier1 = [t for t in trades if league_of(t) in {"bra", "nba"}]
print("--- Tier 1 only (bra + nba, strict) ---")
print(fmt("Tier 1", *aggregate(tier1)))
by_lg1 = defaultdict(list)
for t in tier1: by_lg1[league_of(t)].append(t)
for lg, ts in sorted(by_lg1.items(), key=lambda kv: -aggregate(kv[1])[4]):
    print(fmt(f"  {lg}", *aggregate(ts)))
print()

# What's in trades but NOT in tier 1+2 — what would we drop?
dropped = [t for t in trades if league_of(t) not in TIER12]
print("--- Trades dropped by Tier 1+2 filter ---")
print(fmt("Dropped total", *aggregate(dropped)))
by_drop = defaultdict(list)
for t in dropped: by_drop[league_of(t)].append(t)
for lg, ts in sorted(by_drop.items(), key=lambda kv: -aggregate(kv[1])[4]):
    print(fmt(f"  {lg}", *aggregate(ts)))
