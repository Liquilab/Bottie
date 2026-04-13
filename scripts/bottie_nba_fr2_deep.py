#!/usr/bin/env python3
"""Deep dive into Bottie's nba (loser, n=41, -11.3%) and fr2 (winner, n=13, +80.9%) trades.
Show: per-trade detail, entry price distribution, leg-type mix, signal_delay,
copy_wallet, win/loss patterns."""
import json
from collections import defaultdict
from statistics import mean, median

PATH = "/opt/bottie/data/trades.jsonl"

def league_of(t):
    slug = (t.get("event_slug") or "").replace("-more-markets", "")
    return slug.split("-", 1)[0] if slug else ""

def leg_type(t):
    title = (t.get("market_title") or "").lower()
    outcome = (t.get("outcome") or "").lower()
    if "end in a draw" in title: return f"DRAW_{outcome.upper()}"
    if "win on" in title: return f"WIN_{outcome.upper()}"
    if "o/u" in title or "over/under" in title: return "O/U"
    if "spread" in title: return "SPREAD"
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

for league_name in ("nba", "fr2"):
    rows = [t for t in trades if league_of(t) == league_name]
    print()
    print("=" * 100)
    print(f"LEAGUE: {league_name}  ({len(rows)} trades)")
    print("=" * 100)

    # Aggregate
    w = sum(1 for t in rows if t["result"]=="win")
    l = sum(1 for t in rows if t["result"]=="loss")
    stake = sum(float(t.get("size_usdc") or 0) for t in rows)
    pnl = sum(float(t.get("pnl") or 0) for t in rows)
    print(f"WR: {w/(w+l)*100:.1f}%  Stake: ${stake:,.0f}  PnL: ${pnl:+,.0f}  ROI: {pnl/stake*100:+.2f}%")

    # Per leg type
    print(f"\n--- per leg-type ---")
    by_t = defaultdict(list)
    for t in rows: by_t[leg_type(t)].append(t)
    for k in sorted(by_t, key=lambda x: -sum(float(t.get('pnl') or 0) for t in by_t[x])):
        ts = by_t[k]
        ww = sum(1 for t in ts if t["result"]=="win")
        ll = sum(1 for t in ts if t["result"]=="loss")
        s = sum(float(t.get("size_usdc") or 0) for t in ts)
        p = sum(float(t.get("pnl") or 0) for t in ts)
        roi = p/s*100 if s else 0
        print(f"  {k:<14} n={len(ts):>3}  W={ww:>2}/L={ll:>2}  WR={ww/(ww+ll)*100:>5.1f}%  stake=${s:>7,.0f}  pnl=${p:>+7,.0f}  ROI={roi:>+7.1f}%")

    # Entry price distribution
    prices = [float(t.get("price") or 0) for t in rows if t.get("price")]
    if prices:
        print(f"\n--- entry price ---")
        print(f"  min={min(prices):.3f}  median={median(prices):.3f}  mean={mean(prices):.3f}  max={max(prices):.3f}")

    # Signal delay
    delays = [t.get("signal_delay_ms", 0) for t in rows if t.get("signal_delay_ms") is not None]
    if delays:
        print(f"\n--- signal delay (ms) ---")
        print(f"  min={min(delays)}  median={median(delays):.0f}  mean={mean(delays):.0f}  max={max(delays)}")

    # Copy wallet
    print(f"\n--- copy_wallet ---")
    by_w = defaultdict(int)
    for t in rows: by_w[t.get("copy_wallet","?")[:10]] += 1
    for w_, c in sorted(by_w.items(), key=lambda x: -x[1]):
        print(f"  {w_}... : {c}")

    # consensus_count
    cc = defaultdict(int)
    for t in rows: cc[t.get("consensus_count", 0)] += 1
    print(f"\n--- consensus_count ---")
    for c, n in sorted(cc.items()):
        print(f"  {c}: {n}")

    # confidence (sizing %)
    confs = [t.get("confidence", 0) for t in rows if t.get("confidence")]
    if confs:
        print(f"\n--- confidence (sizing %) ---")
        print(f"  min={min(confs):.3f}  median={median(confs):.3f}  max={max(confs):.3f}")

    # Per-trade listing
    print(f"\n--- ALL TRADES ---")
    rows_sorted = sorted(rows, key=lambda t: float(t.get("pnl") or 0))
    for t in rows_sorted:
        ts = (t.get("timestamp") or "")[:16]
        slug = (t.get("event_slug") or "")[:30]
        out = (t.get("outcome") or "")[:5]
        title = (t.get("market_title") or "")[:38]
        price = float(t.get("price") or 0)
        size = float(t.get("size_usdc") or 0)
        p = float(t.get("pnl") or 0)
        result = t.get("result", "?")
        print(f"  {ts}  {slug:<30}  {title:<38}  {out:<5}  ${size:>6,.1f} @ {price:.3f}  {result:<4}  ${p:>+7,.1f}")
