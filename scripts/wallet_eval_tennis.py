#!/usr/bin/env python3
"""Full wallet-eval protocol for tennis wallet 0xe30e74..."""
import json, urllib.request, datetime, time
from collections import defaultdict

API = "https://data-api.polymarket.com"
WALLET = "0xe30e74595517de48f1fb19f4553dd3d9f1e96b87"

def fetch(u):
    time.sleep(0.5)
    req = urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

print("WALLET-EVAL PROTOCOL: 0xe30e74...")
print("=" * 80)

# === A. ALL closed positions (paginate!) ===
print("\n[A] Fetching ALL closed positions...")
offset = 0
all_closed = []
while True:
    try:
        data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, WALLET, offset))
    except Exception as e:
        print("  Error at offset %d: %s, retrying..." % (offset, e))
        time.sleep(5)
        try:
            data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, WALLET, offset))
        except:
            break
    if not data: break
    all_closed.extend(data)
    if len(data) < 50: break
    offset += 50
print("  Total closed: %d" % len(all_closed))

# === B. ALL open positions ===
print("\n[B] Fetching open positions...")
open_pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("  Open positions: %d" % len(open_pos))

# === C. Mergeable ===
print("\n[C] Fetching mergeable positions...")
mergeable = fetch("%s/positions?user=%s&mergeable=true&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("  Mergeable: %d" % len(mergeable))

# === D. Sells via trades ===
print("\n[D] Fetching sells...")
sells = fetch("%s/trades?user=%s&side=SELL&limit=10000" % (API, WALLET))
print("  Sells: %d" % len(sells))
sells_under99 = [s for s in sells if float(s.get("price",0) or 0) < 0.99]
sells_at99 = [s for s in sells if float(s.get("price",0) or 0) >= 0.99]
print("  Sells <99ct: %d | Sells >=99ct: %d" % (len(sells_under99), len(sells_at99)))

# === E. Activity: merges ===
print("\n[E] Fetching merge activities...")
merges = fetch("%s/activity?user=%s&type=MERGE&limit=500" % (API, WALLET))
print("  Merges: %d" % len(merges))

# === F. Activity: redeems ===
print("\n[F] Fetching redeem activities...")
redeems = fetch("%s/activity?user=%s&type=REDEEM&limit=500" % (API, WALLET))
print("  Redeems: %d" % len(redeems))

# === G. Activity: sells ===
print("\n[G] Fetching sell activities...")
sell_activity = fetch("%s/activity?user=%s&side=SELL&limit=500" % (API, WALLET))
print("  Sell activities: %d" % len(sell_activity))

# === LB API Profit (SSOT) ===
print("\n[LB] Fetching LB API profit...")
try:
    lb = fetch("https://lb-api.polymarket.com/profit?address=%s" % WALLET)
    lb_profit = float(lb[0].get("amount", 0)) if lb else 0
    lb_name = lb[0].get("pseudonym", "") if lb else ""
    print("  LB Profit: $%.0f (name: %s)" % (lb_profit, lb_name))
except Exception as e:
    lb_profit = 0
    print("  LB Error: %s" % e)

# ================================================================
# CHECKS
# ================================================================

print("\n" + "=" * 80)
print("CHECKS")
print("=" * 80)

# Check 1: Market Maker?
print("\n--- CHECK 1: MARKET MAKER? ---")
is_mm = False
if len(mergeable) > 0:
    print("  Mergeable positions: %d → MARKET MAKER INDICATOR" % len(mergeable))
    is_mm = True
else:
    print("  Mergeable positions: 0 → NIET market maker")

if len(merges) > 10:
    print("  Merge activities: %d → ACTIEVE MARKET MAKER" % len(merges))
    is_mm = True
else:
    print("  Merge activities: %d → Geen actieve MM" % len(merges))

if sells_under99:
    print("  Sells <99ct: %d → HEEFT STOP LOSSES OF MID-GAME EXITS" % len(sells_under99))
    for s in sorted(sells_under99, key=lambda x: -float(x.get("size",0) or 0) * float(x.get("price",0) or 0))[:5]:
        price = float(s.get("price",0) or 0)
        size = float(s.get("size",0) or 0)
        title = (s.get("title","") or "")[:45]
        print("    SELL %.0fsh @ %.2f = $%.0f  %s" % (size, price, size*price, title))
else:
    print("  Sells <99ct: 0 → Verkoopt alleen winners")

print("  VERDICT: %s" % ("JA — MARKET MAKER" if is_mm else "NEE — GEEN MARKET MAKER"))

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
    title = (p.get("title","") or "")[:50]
    outcome = (p.get("outcome","") or "")[:12]
    slug = p.get("eventSlug","") or ""
    if cv == 0 or cp <= 0.05:
        ghost_losses.append((iv, outcome, slug, title))
    elif cp >= 0.95:
        ghost_wins.append((iv, cv, cv-iv, outcome, slug, title))

gl_total = sum(x[0] for x in ghost_losses)
gw_total = sum(x[2] for x in ghost_wins)
print("  Ghost losses: %d (total: -$%.0f)" % (len(ghost_losses), gl_total))
for iv, out, slug, title in sorted(ghost_losses, key=lambda x: -x[0])[:10]:
    print("    -$%8.0f  %-12s %s" % (iv, out, title))
print("  Ghost wins: %d (total: +$%.0f)" % (len(ghost_wins), gw_total))

# Check 4: Per-league breakdown
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

for lg in sorted(by_league, key=lambda x: -by_league[x]["pnl"]):
    d = by_league[lg]
    n = d["w"]+d["l"]
    lwr = d["w"]/n*100 if n else 0
    lroi = d["pnl"]/d["inv"]*100 if d["inv"] else 0
    print("  %-10s %3dW/%3dL  WR:%5.1f%%  PnL:$%+10.0f  Inv:$%10.0f  ROI:%6.1f%%" % (
        lg, d["w"], d["l"], lwr, d["pnl"], d["inv"], lroi))

# Check 5: Per-week breakdown
print("\n--- CHECK 5: PER WEEK ---")
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
    except:
        continue
    year, week_num, _ = dt.isocalendar()
    week_key = "%d-W%02d" % (year, week_num)
    weekly[week_key]["inv"] += inv
    weekly[week_key]["pnl"] += pnl
    weekly[week_key]["w" if pnl > 0 else "l"] += 1

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

# Trend analysis
weeks = sorted(weekly.keys())
if len(weeks) >= 4:
    last4 = weeks[-4:]
    first_half = weeks[:len(weeks)//2]
    second_half = weeks[len(weeks)//2:]

    early_wr_data = [(weekly[w]["w"], weekly[w]["l"]) for w in first_half]
    late_wr_data = [(weekly[w]["w"], weekly[w]["l"]) for w in second_half]

    early_w = sum(x[0] for x in early_wr_data)
    early_l = sum(x[1] for x in early_wr_data)
    late_w = sum(x[0] for x in late_wr_data)
    late_l = sum(x[1] for x in late_wr_data)

    early_wr = early_w/(early_w+early_l)*100 if (early_w+early_l) else 0
    late_wr = late_w/(late_w+late_l)*100 if (late_w+late_l) else 0

    print()
    print("  Trend: eerste helft %.1f%% WR → tweede helft %.1f%% WR" % (early_wr, late_wr))
    if late_wr > early_wr + 5:
        print("  → STIJGEND")
    elif late_wr < early_wr - 5:
        print("  → DALEND")
    else:
        print("  → STABIEL")

# Check 6: LB API vs berekening
print("\n--- CHECK 6: LB API VERGELIJKING ---")
calc_pnl = total_pnl - gl_total + gw_total
print("  LB API (SSOT):     $%+.0f" % lb_profit)
print("  Onze berekening:   $%+.0f (closed) + $%+.0f (ghost wins) - $%.0f (ghost losses) = $%+.0f" % (
    total_pnl, gw_total, gl_total, calc_pnl))
diff_pct = abs(lb_profit - calc_pnl) / abs(lb_profit) * 100 if lb_profit != 0 else 0
print("  Verschil: $%.0f (%.0f%%)" % (abs(lb_profit - calc_pnl), diff_pct))
if diff_pct > 20:
    print("  ⚠️ VERSCHIL > 20%% — ER ONTBREEKT DATA")

# ================================================================
# GECORRIGEERD TOTAAL
# ================================================================
print("\n" + "=" * 80)
print("GECORRIGEERD TOTAAL")
print("=" * 80)
total_resolved = len(all_closed) + len(ghost_losses) + len(ghost_wins)
total_wins = len(wins) + len(ghost_wins)
total_losses_count = len(losses) + len(ghost_losses)
corrected_pnl = total_pnl - gl_total + gw_total
corrected_wr = total_wins/total_resolved*100 if total_resolved else 0
print("  Resolved: %d (%dW / %dL) = %.1f%% WR" % (total_resolved, total_wins, total_losses_count, corrected_wr))
print("  Corrected PnL: $%+.0f" % corrected_pnl)
print("  LB Profit SSOT: $%+.0f" % lb_profit)

# All closed detail
print("\n--- ALL CLOSED POSITIONS ---")
for t in sorted(all_closed, key=lambda x: -abs(float(x.get("realizedPnl",0) or 0))):
    pnl = float(t.get("realizedPnl",0) or 0)
    inv = float(t.get("totalBought",0) or 0)
    avg = float(t.get("avgPrice",0) or 0)
    title = (t.get("title","") or "")[:50]
    outcome = (t.get("outcome","") or "")[:12]
    slug = (t.get("eventSlug","") or "")[:25]
    wl = "W" if pnl > 0 else "L"
    print("  %s $%+9.0f  inv=$%8.0f  avg=%.2f  %-12s %-25s %s" % (
        wl, pnl, inv, avg, outcome, slug, title))
