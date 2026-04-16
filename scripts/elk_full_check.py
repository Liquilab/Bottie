#!/usr/bin/env python3
"""Full Elkmonkey survivorship bias check."""
import json, urllib.request

API = "https://data-api.polymarket.com"
WALLET = "0xead152b855effa6b5b5837f53b24c0756830c76a"

def fetch(u):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"}), timeout=15
    ).read())

# 1. Closed positions
offset = 0
closed = []
while True:
    data = fetch("%s/closed-positions?user=%s&limit=500&offset=%d" % (API, WALLET, offset))
    if not data: break
    closed.extend(data)
    offset += 500
    if len(data) < 500: break

print("=== CLOSED: %d ===" % len(closed))
wins = 0
losses = 0
closed_pnl = 0
for t in closed:
    pnl = float(t.get("realizedPnl", 0) or 0)
    tb = float(t.get("totalBought", 0) or 0)
    title = (t.get("title","") or "")[:50]
    slug = t.get("eventSlug","") or ""
    avg = float(t.get("avgPrice", 0) or 0)
    closed_pnl += pnl
    wl = "W" if pnl > 0 else "L"
    if pnl > 0: wins += 1
    else: losses += 1
    print("  %s $%+10.0f  bought=$%10.0f  avg=%.2f  %-35s %s" % (wl, pnl, tb, avg, slug[:35], title))

print("\nClosed: %dW / %dL, PnL: $%+.0f" % (wins, losses, closed_pnl))

# 2. Open positions
pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, WALLET))
print("\n=== OPEN: %d ===" % len(pos))

ghost_loss = []
ghost_win = []
real_open = []

for p in pos:
    iv = float(p.get("initialValue", 0) or 0)
    cv = float(p.get("currentValue", 0) or 0)
    cp = float(p.get("curPrice", 0.5) or 0.5)
    title = (p.get("title","") or "")[:50]
    slug = p.get("eventSlug","") or ""
    outcome = (p.get("outcome","") or "")[:10]
    if iv <= 0:
        continue
    if cv == 0:
        ghost_loss.append((iv, slug, outcome, title))
    elif cp >= 0.95:
        ghost_win.append((iv, cv, cv-iv, slug, outcome, title))
    elif cp <= 0.05:
        ghost_loss.append((iv, slug, outcome, title))
    else:
        real_open.append((iv, cv, cp, slug, outcome, title))

print("\nGHOST LOSSES (%d):" % len(ghost_loss))
gl_total = 0
for iv, slug, out, title in sorted(ghost_loss, key=lambda x: -x[0]):
    gl_total += iv
    print("  -$%8.0f  %-10s  %-35s %s" % (iv, out, slug[:35], title))
print("  TOTAL: -$%.0f" % gl_total)

print("\nGHOST WINS (%d):" % len(ghost_win))
gw_total = 0
for iv, cv, pnl, slug, out, title in sorted(ghost_win, key=lambda x: -x[2])[:10]:
    gw_total += pnl
    print("  +$%7.0f  %-10s  %-35s %s" % (pnl, out, slug[:35], title))
if len(ghost_win) > 10:
    rest = sum(x[2] for x in sorted(ghost_win, key=lambda x: -x[2])[10:])
    gw_total += rest
    print("  ... +%d more = +$%.0f" % (len(ghost_win)-10, rest))
print("  TOTAL: +$%.0f" % gw_total)

print("\nREAL OPEN (%d):" % len(real_open))
for iv, cv, cp, slug, out, title in sorted(real_open, key=lambda x: -x[0])[:20]:
    pnl = cv - iv
    print("  $%8.0f -> $%7.0f  cp=%.2f  %-10s  %-30s %s" % (iv, cv, cp, out, slug[:30], title))
if len(real_open) > 20:
    print("  ... +%d more" % (len(real_open)-20))

total_resolved = len(closed) + len(ghost_loss) + len(ghost_win)
total_wins = wins + len(ghost_win)
total_losses = losses + len(ghost_loss)
total_pnl = closed_pnl - gl_total + gw_total
wr = total_wins/total_resolved*100 if total_resolved else 0

print("\n=== GECORRIGEERD ===")
print("Resolved: %d (%dW / %dL)" % (total_resolved, total_wins, total_losses))
print("WR: %.1f%%" % wr)
print("Closed PnL: +$%.0f" % closed_pnl)
print("Ghost losses: -$%.0f" % gl_total)
print("Ghost wins: +$%.0f" % gw_total)
print("Net resolved PnL: $%+.0f" % total_pnl)
print("Still open: %d positions" % len(real_open))
