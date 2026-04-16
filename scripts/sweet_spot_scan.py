#!/usr/bin/env python3
"""Re-scan dismissed wallets for league x size sweet spots."""
import json, urllib.request, time, datetime
from collections import defaultdict

API = "https://data-api.polymarket.com"

def fetch(u):
    time.sleep(0.4)
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent":"B/1","Accept":"application/json"}), timeout=30
    ).read())

WALLETS = [
    ("bossoskil1", "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a"),
    ("noMoohyun523", "0x63a51cbb37341837b873bc29d05f482bc2988e33"),
    ("0x163eff", "0x163eff4d251df4bfc95c49f4d90cd1bf224edc5b"),
    ("Dhdhsjsj", "0x5d58e38cd0a7e6f5fa67b7f9c2f70dd70df09a15"),
    ("VARca", "0x5c3a1a602848565bb16165fcd460b00c3d43020b"),
]

for wallet_name, wallet_addr in WALLETS:
    print("\n" + "=" * 80)
    print("  SWEET SPOT SCAN: %s (%s)" % (wallet_name, wallet_addr[:12]))
    print("=" * 80)

    # All closed positions
    offset = 0
    all_closed = []
    while True:
        try:
            data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, wallet_addr, offset))
        except:
            time.sleep(3)
            try:
                data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, wallet_addr, offset))
            except: break
        if not data: break
        all_closed.extend(data)
        if len(data) < 50: break
        offset += 50

    # Ghost losses
    try:
        pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, wallet_addr))
        ghosts = []
        for p in pos:
            iv = float(p.get("initialValue",0) or 0)
            cv = float(p.get("currentValue",0) or 0)
            if cv == 0 and iv > 0:
                ghosts.append({"pnl": -iv, "inv": iv, "slug": p.get("eventSlug","") or "", "outcome": p.get("outcome","") or "", "result": "loss"})
    except:
        ghosts = []

    # Mergeable check
    try:
        mergeable = fetch("%s/positions?user=%s&mergeable=true&limit=100&sizeThreshold=0.01" % (API, wallet_addr))
    except:
        mergeable = []

    # Combine
    all_resolved = []
    for t in all_closed:
        all_resolved.append({"pnl": float(t.get("realizedPnl",0) or 0), "inv": float(t.get("totalBought",0) or 0), "slug": t.get("eventSlug","") or "", "outcome": t.get("outcome","") or "", "result": "win" if float(t.get("realizedPnl",0) or 0) > 0 else "loss"})
    all_resolved.extend(ghosts)

    # Group by event
    by_event = defaultdict(list)
    for r in all_resolved:
        by_event[r["slug"]].append(r)

    # Per league: find directional events, check size buckets
    leagues = defaultdict(lambda: {"dir": [], "mm": []})
    for slug, legs in by_event.items():
        league = slug.split("-")[0] if slug else "?"
        outcomes = set(r["outcome"] for r in legs)
        total_inv = sum(r["inv"] for r in legs)
        total_pnl = sum(r["pnl"] for r in legs)
        entry = {"inv": total_inv, "pnl": total_pnl, "result": "win" if total_pnl > 0 else "loss"}
        if len(outcomes) >= 2:
            leagues[league]["mm"].append(entry)
        else:
            leagues[league]["dir"].append(entry)

    # Find sweet spots
    sweet_spots = []
    for league in sorted(leagues.keys()):
        dir_events = leagues[league]["dir"]
        mm_events = leagues[league]["mm"]
        if len(dir_events) < 3:
            continue

        for threshold in [5000, 10000, 25000, 50000, 100000]:
            sub = [e for e in dir_events if e["inv"] >= threshold]
            if len(sub) < 5:
                continue
            w = sum(1 for e in sub if e["result"] == "win")
            l = sum(1 for e in sub if e["result"] == "loss")
            pnl = sum(e["pnl"] for e in sub)
            inv = sum(e["inv"] for e in sub)
            wr = w/(w+l)*100 if w+l else 0
            roi = pnl/inv*100 if inv else 0
            n = len(sub)

            if wr >= 60 and pnl > 0 and n >= 10:
                sweet_spots.append((league, threshold, w, l, wr, pnl, roi, n, len(mm_events)))

    if sweet_spots:
        print("\n  SWEET SPOTS GEVONDEN:")
        for league, thresh, w, l, wr, pnl, roi, n, mm_n in sweet_spots:
            mm_tag = " [MM=%d]" % mm_n if mm_n > 0 else ""
            print("    %-8s >= $%6d: %3dW/%3dL  WR:%5.1f%%  PnL:$%+10.0f  ROI:%5.1f%%  N=%d%s" % (
                league, thresh, w, l, wr, pnl, roi, n, mm_tag))
    else:
        print("\n  GEEN SWEET SPOTS (WR>=60%%, PnL>0, N>=10)")

    # Also show per-league totals for context
    print("\n  Per league (directional, alle sizes):")
    for league in sorted(leagues.keys()):
        d = leagues[league]["dir"]
        m = leagues[league]["mm"]
        if len(d) < 3: continue
        w = sum(1 for e in d if e["result"] == "win")
        l = sum(1 for e in d if e["result"] == "loss")
        pnl = sum(e["pnl"] for e in d)
        inv = sum(e["inv"] for e in d)
        wr = w/(w+l)*100 if w+l else 0
        roi = pnl/inv*100 if inv else 0
        mm_tag = " [+%dMM]" % len(m) if m else ""
        print("    %-8s %3dW/%3dL  WR:%5.1f%%  PnL:$%+10.0f  ROI:%5.1f%%%s" % (
            league, w, l, wr, pnl, roi, mm_tag))
