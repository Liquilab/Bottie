#!/usr/bin/env python3
"""Full wallet-eval protocol for esports wallet 0xa5ea13..."""
import json, urllib.request, datetime, time
from collections import defaultdict

API = "https://data-api.polymarket.com"
WALLET = "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a"

def fetch(u):
    time.sleep(0.4)
    req = urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

print("WALLET-EVAL: 0xa5ea13...")
print("=" * 80)

# A. ALL closed positions
print("\n[A] Closed positions...")
offset = 0
all_closed = []
while True:
    try:
        data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, WALLET, offset))
    except:
        time.sleep(3)
        try:
            data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, WALLET, offset))
        except: break
    if not data: break
    all_closed.extend(data)
    if offset % 500 == 0 and offset > 0:
        print("  %d..." % len(all_closed))
    if len(data) < 50: break
    offset += 50
print("  Total: %d" % len(all_closed))

# B-G: Other endpoints
print("\n[B-G] Other endpoints...")
open_pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("  Open: %d" % len(open_pos))
mergeable = fetch("%s/positions?user=%s&mergeable=true&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("  Mergeable: %d" % len(mergeable))
sells = fetch("%s/trades?user=%s&side=SELL&limit=10000" % (API, WALLET))
sells_under99 = [s for s in sells if float(s.get("price",0) or 0) < 0.99]
print("  Sells: %d (<%s99ct: %d)" % (len(sells), "", len(sells_under99)))
merges = fetch("%s/activity?user=%s&type=MERGE&limit=500" % (API, WALLET))
print("  Merges: %d" % len(merges))

# LB
try:
    lb = fetch("https://lb-api.polymarket.com/profit?address=%s" % WALLET)
    lb_profit = float(lb[0].get("amount", 0)) if lb else 0
    lb_name = lb[0].get("pseudonym", "") if lb else ""
    print("  LB Profit: $%.0f (name: %s)" % (lb_profit, lb_name))
except:
    lb_profit = 0
    print("  LB Error")

# === CHECKS ===
print("\n" + "=" * 80)

# Check 1
is_mm = len(mergeable) > 0 or len(merges) > 10
print("MARKET MAKER: %s (mergeable=%d, merges=%d, sells<99ct=%d)" % (
    "JA" if is_mm else "NEE", len(mergeable), len(merges), len(sells_under99)))

# Check 2
wins = [t for t in all_closed if float(t.get("realizedPnl",0) or 0) > 0]
losses = [t for t in all_closed if float(t.get("realizedPnl",0) or 0) <= 0]
total_pnl = sum(float(t.get("realizedPnl",0) or 0) for t in all_closed)
total_inv = sum(float(t.get("totalBought",0) or 0) for t in all_closed)
wr = len(wins)/len(all_closed)*100 if all_closed else 0
roi = total_pnl/total_inv*100 if total_inv else 0
print("\nCLOSED: %dW / %dL = %.1f%% WR | PnL: $%+.0f | Inv: $%.0f | ROI: %.1f%%" % (
    len(wins), len(losses), wr, total_pnl, total_inv, roi))

# Check 3
ghost_losses = [(float(p.get("initialValue",0) or 0), (p.get("outcome","") or "")[:12], (p.get("title","") or "")[:50], p.get("eventSlug","") or "")
    for p in open_pos if float(p.get("currentValue",0) or 0) == 0 and float(p.get("initialValue",0) or 0) > 0]
gl_total = sum(x[0] for x in ghost_losses)
print("\nGHOST LOSSES: %d (-$%.0f)" % (len(ghost_losses), gl_total))
for iv, out, title, slug in sorted(ghost_losses, key=lambda x: -x[0])[:10]:
    print("  -$%8.0f  %-12s %s" % (iv, out, title))

# Corrected
tr = len(all_closed) + len(ghost_losses)
tw = len(wins)
tl = len(losses) + len(ghost_losses)
calc = total_pnl - gl_total
cwr = tw/tr*100 if tr else 0
print("\nGECORRIGEERD: %dW / %dL = %.1f%% WR | PnL: $%+.0f | LB: $%+.0f" % (tw, tl, cwr, calc, lb_profit))

# Check 4: Per league
print("\n--- PER LEAGUE ---")
by_league = defaultdict(lambda: {"w":0,"l":0,"pnl":0.0,"inv":0.0})
for t in all_closed:
    slug = t.get("eventSlug","") or ""
    league = slug.split("-")[0] if slug else "?"
    pnl = float(t.get("realizedPnl",0) or 0)
    inv = float(t.get("totalBought",0) or 0)
    by_league[league]["w" if pnl > 0 else "l"] += 1
    by_league[league]["pnl"] += pnl
    by_league[league]["inv"] += inv
for iv, out, title, slug in ghost_losses:
    league = slug.split("-")[0] if slug else "?"
    by_league[league]["l"] += 1
    by_league[league]["pnl"] -= iv
    by_league[league]["inv"] += iv

for lg in sorted(by_league, key=lambda x: -by_league[x]["pnl"]):
    d = by_league[lg]
    n = d["w"]+d["l"]
    lwr = d["w"]/n*100 if n else 0
    lroi = d["pnl"]/d["inv"]*100 if d["inv"] else 0
    print("  %-10s %4dW/%4dL  WR:%5.1f%%  PnL:$%+10.0f  Inv:$%10.0f  ROI:%6.1f%%" % (
        lg, d["w"], d["l"], lwr, d["pnl"], d["inv"], lroi))

# Check 5: Per week
print("\n--- PER WEEK ---")
weekly = defaultdict(lambda: {"w":0,"l":0,"pnl":0.0,"inv":0.0})
for t in all_closed:
    pnl = float(t.get("realizedPnl",0) or 0)
    inv = float(t.get("totalBought",0) or 0)
    ts_raw = t.get("endDate") or t.get("timestamp")
    if not ts_raw: continue
    try:
        if isinstance(ts_raw, (int, float)):
            dt = datetime.datetime.fromtimestamp(int(ts_raw), tz=datetime.timezone.utc)
        else:
            dt = datetime.datetime.strptime(str(ts_raw)[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    except: continue
    year, wn, _ = dt.isocalendar()
    wk = "%d-W%02d" % (year, wn)
    weekly[wk]["inv"] += inv
    weekly[wk]["pnl"] += pnl
    weekly[wk]["w" if pnl > 0 else "l"] += 1

print("%-10s %5s %5s %6s %12s %12s %7s" % ("Week", "W", "L", "WR%", "PnL", "Invested", "ROI%"))
print("-" * 70)
for week in sorted(weekly.keys()):
    d = weekly[week]
    n = d["w"]+d["l"]
    if n == 0: continue
    wwr = d["w"]/n*100
    wroi = d["pnl"]/d["inv"]*100 if d["inv"] else 0
    print("%-10s %5d %5d %5.1f%% $%+10.0f $%10.0f %6.1f%%" % (
        week, d["w"], d["l"], wwr, d["pnl"], d["inv"], wroi))

weeks = sorted(weekly.keys())
if len(weeks) >= 4:
    half = len(weeks)//2
    ew = sum(weekly[w]["w"] for w in weeks[:half])
    el = sum(weekly[w]["l"] for w in weeks[:half])
    lw = sum(weekly[w]["w"] for w in weeks[half:])
    ll = sum(weekly[w]["l"] for w in weeks[half:])
    ewr = ew/(ew+el)*100 if ew+el else 0
    lwr = lw/(lw+ll)*100 if lw+ll else 0
    print("\nTrend: eerste helft %.1f%% → tweede helft %.1f%% → %s" % (
        ewr, lwr, "STIJGEND" if lwr > ewr+5 else "DALEND" if lwr < ewr-5 else "STABIEL"))

# Size vs WR (per match)
print("\n--- SIZE vs WR (per match) ---")
all_resolved = []
for t in all_closed:
    all_resolved.append({"pnl": float(t.get("realizedPnl",0) or 0), "inv": float(t.get("totalBought",0) or 0), "slug": t.get("eventSlug","") or "", "result": "win" if float(t.get("realizedPnl",0) or 0) > 0 else "loss"})
for iv, out, title, slug in ghost_losses:
    all_resolved.append({"pnl": -iv, "inv": iv, "slug": slug, "result": "loss"})

by_match = {}
for r in all_resolved:
    s = r["slug"]
    if not s: continue
    if s not in by_match: by_match[s] = {"inv":0,"pnl":0}
    by_match[s]["inv"] += r["inv"]
    by_match[s]["pnl"] += r["pnl"]
matches = [{"slug":s,"inv":d["inv"],"pnl":d["pnl"],"result":"win" if d["pnl"]>0 else "loss"} for s,d in by_match.items()]

for threshold in [0, 1000, 5000, 10000, 25000, 50000]:
    sub = [m for m in matches if m["inv"] >= threshold]
    if not sub: continue
    w = sum(1 for m in sub if m["result"]=="win")
    l = sum(1 for m in sub if m["result"]=="loss")
    pnl = sum(m["pnl"] for m in sub)
    inv = sum(m["inv"] for m in sub)
    wr = w/(w+l)*100 if w+l else 0
    roi = pnl/inv*100 if inv else 0
    print("  >= $%5d:  %3dW/%3dL  WR:%5.1f%%  PnL:$%+10.0f  ROI:%6.1f%%  N=%d" % (
        threshold, w, l, wr, pnl, roi, len(sub)))
