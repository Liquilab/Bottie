#!/usr/bin/env python3
"""Head-to-head comparison: Elkmonkey vs kch123 on NBA and NHL."""
import json, urllib.request

API = "https://data-api.polymarket.com"
WALLETS = {
    "Elkmonkey": "0xead152b855effa6b5b5837f53b24c0756830c76a",
    "kch123": "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
}

FUTURES_KW = ["champion", "finals", "mvp", "stanley cup", "hart memorial", "mls cup", "world series", "ballon"]

def fetch(u):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"}), timeout=15
    ).read())

def get_all_resolved(wallet):
    # Closed positions
    offset = 0
    closed = []
    while True:
        data = fetch("%s/closed-positions?user=%s&limit=500&offset=%d" % (API, wallet, offset))
        if not data: break
        closed.extend(data)
        offset += 500
        if len(data) < 500: break

    # Open positions (ghost wins/losses)
    pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, wallet))

    resolved = []
    for t in closed:
        pnl = float(t.get("realizedPnl", 0) or 0)
        tb = float(t.get("totalBought", 0) or 0)
        avg = float(t.get("avgPrice", 0) or 0)
        slug = t.get("eventSlug", "") or ""
        title = t.get("title", "") or ""
        outcome = t.get("outcome", "") or ""
        resolved.append({
            "pnl": pnl, "inv": tb, "avg": avg, "slug": slug,
            "title": title, "outcome": outcome,
            "result": "win" if pnl > 0 else "loss", "source": "closed"
        })

    for p in pos:
        iv = float(p.get("initialValue", 0) or 0)
        cv = float(p.get("currentValue", 0) or 0)
        cp = float(p.get("curPrice", 0.5) or 0.5)
        slug = p.get("eventSlug", "") or ""
        title = p.get("title", "") or ""
        outcome = (p.get("outcome", "") or "")
        if iv <= 0: continue
        if cv == 0 or cp <= 0.05:
            resolved.append({
                "pnl": -iv, "inv": iv, "avg": 0, "slug": slug,
                "title": title, "outcome": outcome,
                "result": "loss", "source": "ghost_loss"
            })
        elif cp >= 0.95:
            resolved.append({
                "pnl": cv - iv, "inv": iv, "avg": 0, "slug": slug,
                "title": title, "outcome": outcome,
                "result": "win", "source": "ghost_win"
            })

    return resolved

def classify(title):
    tl = title.lower()
    if "spread" in tl: return "spread"
    if "o/u" in tl or "over" in tl or "under" in tl: return "ou"
    if "draw" in tl: return "draw"
    if "btts" in tl: return "btts"
    return "win"

def is_futures(title):
    tl = title.lower()
    return any(kw in tl for kw in FUTURES_KW)

def print_league(name, wallet_name, trades):
    if not trades:
        print("  Geen trades")
        return

    w = [t for t in trades if t["result"] == "win"]
    l = [t for t in trades if t["result"] == "loss"]
    total_pnl = sum(t["pnl"] for t in trades)
    total_inv = sum(t["inv"] for t in trades)
    wr = len(w) / len(trades) * 100 if trades else 0
    roi = total_pnl / total_inv * 100 if total_inv else 0

    print("  %s %s: %dW / %dL = %.1f%% WR | PnL: $%+.0f | Inv: $%.0f | ROI: %.1f%%" % (
        wallet_name, name, len(w), len(l), wr, total_pnl, total_inv, roi))
    print()

    # Per market type
    for mt in ["win", "spread", "ou"]:
        sub = [t for t in trades if classify(t["title"]) == mt]
        if not sub: continue
        sw = sum(1 for t in sub if t["result"] == "win")
        sl = sum(1 for t in sub if t["result"] == "loss")
        sp = sum(t["pnl"] for t in sub)
        si = sum(t["inv"] for t in sub)
        sr = sp / si * 100 if si else 0
        print("    %-8s %2dW/%2dL  WR:%5.1f%%  PnL:$%+10.0f  Inv:$%10.0f  ROI:%6.1f%%" % (
            mt, sw, sl, sw/(sw+sl)*100 if sw+sl else 0, sp, si, sr))

    # Sizing breakdown
    print()
    for label, lo, hi in [("<$1K", 0, 1000), ("$1K-$10K", 1000, 10000), ("$10K-$50K", 10000, 50000), ("$50K-$100K", 50000, 100000), ("$100K+", 100000, 999999999)]:
        sub = [t for t in trades if lo <= t["inv"] < hi]
        if not sub: continue
        sw = sum(1 for t in sub if t["result"] == "win")
        sl = sum(1 for t in sub if t["result"] == "loss")
        sp = sum(t["pnl"] for t in sub)
        print("    %-10s %2dW/%2dL  WR:%5.1f%%  PnL:$%+10.0f" % (
            label, sw, sl, sw/(sw+sl)*100 if sw+sl else 0, sp))

    # Every single trade
    print()
    print("  Alle trades:")
    for t in sorted(trades, key=lambda x: -x["inv"]):
        mt = classify(t["title"])
        res = "W" if t["result"] == "win" else "L"
        src = ""
        if t["source"] == "ghost_loss": src = " [ghost]"
        if t["source"] == "ghost_win": src = " [ghost]"
        print("    %s $%+9.0f  inv=$%8.0f  avg=%.2f  %-6s %-12s %s%s" % (
            res, t["pnl"], t["inv"], t["avg"], mt, t["outcome"][:12], t["title"][:45], src))


for wallet_name, wallet_addr in WALLETS.items():
    print()
    print("=" * 80)
    print("  %s (%s...)" % (wallet_name, wallet_addr[:10]))
    print("=" * 80)

    resolved = get_all_resolved(wallet_addr)

    for league_name, league_prefix in [("NBA", "nba"), ("NHL", "nhl")]:
        # Game bets only (no futures)
        game_bets = [r for r in resolved if r["slug"].startswith(league_prefix + "-") and not is_futures(r["title"])]
        futures = [r for r in resolved if r["slug"].startswith(league_prefix) and is_futures(r["title"])]
        # Also catch futures without slug prefix (e.g. "2026-nba-champion")
        futures += [r for r in resolved if league_name.lower() in r["title"].lower() and is_futures(r["title"]) and r not in futures]

        print()
        print("-" * 60)
        print("  %s — %s GAME BETS" % (wallet_name, league_name))
        print("-" * 60)
        print_league(league_name, wallet_name, game_bets)

        if futures:
            fw = sum(1 for t in futures if t["result"] == "win")
            fl = sum(1 for t in futures if t["result"] == "loss")
            fp = sum(t["pnl"] for t in futures)
            fi = sum(t["inv"] for t in futures)
            print("  %s Futures: %dW/%dL  PnL:$%+.0f  Inv:$%.0f" % (league_name, fw, fl, fp, fi))
            print()

# Head to head summary
print()
print("=" * 80)
print("  HEAD-TO-HEAD SAMENVATTING")
print("=" * 80)
