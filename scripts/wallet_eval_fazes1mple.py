#!/usr/bin/env python3
"""Full wallet-eval protocol for fazes1mple 0x13414a77..."""
import json, urllib.request, datetime, time
from collections import defaultdict

API = "https://data-api.polymarket.com"
WALLET = "0x13414a77a4be48988851c73dfd824d0168e70853"

def fetch(u):
    time.sleep(0.5)
    req = urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

print("WALLET-EVAL: fazes1mple (%s)" % WALLET[:12])
print("=" * 80)

# A. ALL closed positions
print("\n[A] Closed positions...")
offset = 0
all_closed = []
while True:
    try:
        data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, WALLET, offset))
    except Exception as e:
        print("  Error at %d: %s, retrying..." % (offset, e))
        time.sleep(5)
        try:
            data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, WALLET, offset))
        except:
            break
    if not data: break
    all_closed.extend(data)
    if offset % 500 == 0 and offset > 0:
        print("  %d..." % len(all_closed))
    if len(data) < 50: break
    offset += 50
print("  Total: %d" % len(all_closed))

# B. Open positions
print("\n[B] Open positions...")
open_pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("  Total: %d" % len(open_pos))

# C. Mergeable
print("\n[C] Mergeable...")
mergeable = fetch("%s/positions?user=%s&mergeable=true&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("  Total: %d" % len(mergeable))

# D. Sells
print("\n[D] Sells...")
sells = fetch("%s/trades?user=%s&side=SELL&limit=10000" % (API, WALLET))
sells_under99 = [s for s in sells if float(s.get("price",0) or 0) < 0.99]
sells_at99 = [s for s in sells if float(s.get("price",0) or 0) >= 0.99]
print("  Total: %d (<%s99ct: %d, >=99ct: %d)" % (len(sells), "", len(sells_under99), len(sells_at99)))

# E. Merges
print("\n[E] Merges...")
merges = fetch("%s/activity?user=%s&type=MERGE&limit=500" % (API, WALLET))
print("  Total: %d" % len(merges))

# F. Redeems
print("\n[F] Redeems...")
redeems = fetch("%s/activity?user=%s&type=REDEEM&limit=500" % (API, WALLET))
print("  Total: %d" % len(redeems))

# G. Sell activity
print("\n[G] Sell activity...")
sell_act = fetch("%s/activity?user=%s&side=SELL&limit=500" % (API, WALLET))
print("  Total: %d" % len(sell_act))

# LB Profit
print("\n[LB] Profit...")
try:
    lb = fetch("https://lb-api.polymarket.com/profit?address=%s" % WALLET)
    lb_profit = float(lb[0].get("amount", 0)) if lb else 0
    lb_name = lb[0].get("pseudonym", "") if lb else ""
    print("  Profit: $%.0f (name: %s)" % (lb_profit, lb_name))
except Exception as e:
    lb_profit = 0
    print("  Error: %s" % e)

# === CHECKS ===
print("\n" + "=" * 80)
print("CHECKS")

# Check 1: Market Maker
print("\n--- CHECK 1: MARKET MAKER ---")
is_mm = len(mergeable) > 0 or len(merges) > 10
print("  Mergeable: %d | Merges: %d | Sells <99ct: %d" % (len(mergeable), len(merges), len(sells_under99)))
if sells_under99:
    for s in sorted(sells_under99, key=lambda x: -float(x.get("size",0) or 0) * float(x.get("price",0) or 0))[:5]:
        p = float(s.get("price",0) or 0)
        sz = float(s.get("size",0) or 0)
        print("    SELL %.0fsh @ %.2f = $%.0f  %s" % (sz, p, sz*p, (s.get("title","") or "")[:40]))
print("  VERDICT: %s" % ("JA" if is_mm else "NEE"))

# Check 2: Echte WR
print("\n--- CHECK 2: ECHTE WR ---")
wins = [t for t in all_closed if float(t.get("realizedPnl",0) or 0) > 0]
losses = [t for t in all_closed if float(t.get("realizedPnl",0) or 0) <= 0]
total_pnl = sum(float(t.get("realizedPnl",0) or 0) for t in all_closed)
total_inv = sum(float(t.get("totalBought",0) or 0) for t in all_closed)
wr = len(wins)/len(all_closed)*100 if all_closed else 0
roi = total_pnl/total_inv*100 if total_inv else 0
print("  %dW / %dL = %.1f%% WR" % (len(wins), len(losses), wr))
print("  PnL: $%+.0f | Inv: $%.0f | ROI: %.1f%%" % (total_pnl, total_inv, roi))

# Check 3: Ghost losses
print("\n--- CHECK 3: GHOST LOSSES ---")
ghost_losses = []
ghost_wins = []
for p in open_pos:
    iv = float(p.get("initialValue",0) or 0)
    cv = float(p.get("currentValue",0) or 0)
    cp = float(p.get("curPrice",0.5) or 0.5)
    if iv <= 0: continue
    if cv == 0 or cp <= 0.05:
        ghost_losses.append((iv, (p.get("outcome","") or "")[:12], (p.get("title","") or "")[:50], p.get("eventSlug","") or ""))
    elif cp >= 0.95:
        ghost_wins.append((iv, cv, cv-iv, (p.get("outcome","") or "")[:12], (p.get("title","") or "")[:50]))
gl_total = sum(x[0] for x in ghost_losses)
gw_total = sum(x[2] for x in ghost_wins)
print("  Ghost losses: %d (-$%.0f)" % (len(ghost_losses), gl_total))
for iv, out, title, slug in sorted(ghost_losses, key=lambda x: -x[0])[:10]:
    print("    -$%8.0f  %-12s %s" % (iv, out, title))
print("  Ghost wins: %d (+$%.0f)" % (len(ghost_wins), gw_total))

# Check 4: Per league
print("\n--- CHECK 4: PER LEAGUE ---")
by_league = defaultdict(lambda: {"w":0,"l":0,"pnl":0.0,"inv":0.0})
for t in all_closed:
    slug = t.get("eventSlug","") or ""
    league = slug.split("-")[0] if slug else "?"
    pnl = float(t.get("realizedPnl",0) or 0)
    inv = float(t.get("totalBought",0) or 0)
    by_league[league]["w" if pnl > 0 else "l"] += 1
    by_league[league]["pnl"] += pnl
    by_league[league]["inv"] += inv
# Add ghost losses to league stats
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
    print("  %-10s %3dW/%3dL  WR:%5.1f%%  PnL:$%+10.0f  Inv:$%10.0f  ROI:%6.1f%%" % (
        lg, d["w"], d["l"], lwr, d["pnl"], d["inv"], lroi))

# Check 5: Per week
print("\n--- CHECK 5: PER WEEK ---")
weekly = defaultdict(lambda: {"w":0,"l":0,"pnl":0.0,"inv":0.0})
all_for_weekly = list(all_closed)
# Add ghosts with estimated dates from slug
for iv, out, title, slug in ghost_losses:
    # Try to extract date from slug
    parts = slug.split("-")
    date_str = ""
    for i, p in enumerate(parts):
        if len(p) == 4 and p.isdigit() and i+2 < len(parts):
            date_str = "-".join(parts[i:i+3])
            break
    if date_str:
        try:
            dt = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            year, wn, _ = dt.isocalendar()
            wk = "%d-W%02d" % (year, wn)
            weekly[wk]["l"] += 1
            weekly[wk]["pnl"] -= iv
            weekly[wk]["inv"] += iv
        except:
            pass

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
    except:
        continue
    year, wn, _ = dt.isocalendar()
    wk = "%d-W%02d" % (year, wn)
    weekly[wk]["inv"] += inv
    weekly[wk]["pnl"] += pnl
    weekly[wk]["w" if pnl > 0 else "l"] += 1

print("  %-10s %4s %4s %6s %12s %12s %7s" % ("Week", "W", "L", "WR%", "PnL", "Invested", "ROI%"))
print("  " + "-" * 65)
for week in sorted(weekly.keys()):
    d = weekly[week]
    n = d["w"]+d["l"]
    if n == 0: continue
    wwr = d["w"]/n*100
    wroi = d["pnl"]/d["inv"]*100 if d["inv"] else 0
    print("  %-10s %4d %4d %5.1f%% $%+10.0f $%10.0f %6.1f%%" % (
        week, d["w"], d["l"], wwr, d["pnl"], d["inv"], wroi))

# Trend
weeks = sorted(weekly.keys())
if len(weeks) >= 4:
    half = len(weeks)//2
    ew = sum(weekly[w]["w"] for w in weeks[:half])
    el = sum(weekly[w]["l"] for w in weeks[:half])
    lw = sum(weekly[w]["w"] for w in weeks[half:])
    ll = sum(weekly[w]["l"] for w in weeks[half:])
    ewr = ew/(ew+el)*100 if ew+el else 0
    lwr = lw/(lw+ll)*100 if lw+ll else 0
    print("\n  Trend: eerste helft %.1f%% → tweede helft %.1f%% → %s" % (
        ewr, lwr, "STIJGEND" if lwr > ewr+5 else "DALEND" if lwr < ewr-5 else "STABIEL"))

# Check 6: LB comparison
print("\n--- CHECK 6: LB VERGELIJKING ---")
calc = total_pnl - gl_total + gw_total
print("  LB SSOT:   $%+.0f" % lb_profit)
print("  Berekend:  $%+.0f" % calc)
diff = abs(lb_profit - calc) / abs(lb_profit) * 100 if lb_profit else 0
print("  Verschil:  %.0f%%" % diff)
if diff > 20:
    print("  !! VERSCHIL > 20%%")

# Corrected total
print("\n" + "=" * 80)
print("GECORRIGEERD TOTAAL")
tr = len(all_closed) + len(ghost_losses) + len(ghost_wins)
tw = len(wins) + len(ghost_wins)
tl = len(losses) + len(ghost_losses)
print("  Resolved: %d (%dW / %dL) = %.1f%% WR" % (tr, tw, tl, tw/tr*100 if tr else 0))
print("  Corrected PnL: $%+.0f" % calc)
print("  LB Profit SSOT: $%+.0f" % lb_profit)

# Size vs WR
print("\n--- SIZE vs WR ---")
all_resolved = []
for t in all_closed:
    all_resolved.append({"pnl": float(t.get("realizedPnl",0) or 0), "inv": float(t.get("totalBought",0) or 0), "slug": t.get("eventSlug","") or "", "result": "win" if float(t.get("realizedPnl",0) or 0) > 0 else "loss"})
for iv, out, title, slug in ghost_losses:
    all_resolved.append({"pnl": -iv, "inv": iv, "slug": slug, "result": "loss"})

# Group by match
by_match = {}
for r in all_resolved:
    s = r["slug"]
    if not s: continue
    if s not in by_match:
        by_match[s] = {"inv":0,"pnl":0}
    by_match[s]["inv"] += r["inv"]
    by_match[s]["pnl"] += r["pnl"]

matches = [{"slug":s, "inv":d["inv"], "pnl":d["pnl"], "result":"win" if d["pnl"]>0 else "loss"} for s,d in by_match.items()]

for threshold in [0, 1000, 5000, 10000, 25000, 50000]:
    sub = [m for m in matches if m["inv"] >= threshold]
    if not sub: continue
    w = sum(1 for m in sub if m["result"]=="win")
    l = sum(1 for m in sub if m["result"]=="loss")
    pnl = sum(m["pnl"] for m in sub)
    inv = sum(m["inv"] for m in sub)
    print("  >= $%5d:  %3dW/%3dL  WR:%5.1f%%  PnL:$%+10.0f  ROI:%6.1f%%  N=%d" % (
        threshold, w, l, w/(w+l)*100 if w+l else 0, pnl, pnl/inv*100 if inv else 0, len(sub)))
