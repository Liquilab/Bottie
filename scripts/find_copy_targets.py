#!/usr/bin/env python3
"""Find copy targets — scan holders across ALL sports, find consistent winners.

1. Fetch events per sport tag from Gamma API
2. Per event → /holders → collect wallet addresses
3. Frequency filter: wallets in 5+ events
4. Hauptbet analysis via lib/analyse.py on top candidates
5. Rank by WR × ROI
"""

import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from lib.analyse import (
    fetch_and_merge, hauptbet_analysis, classify_sport,
    get_lb_profit, fetch, API,
)

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "wallet-scout")

EXCLUDE = {
    "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b",  # Cannae
    "0x07921379f7b31ef93da634b688b2fe36897db778",  # ewelmealt
    "0x9f23f6d5d18f9fc5aef42efec8f63a7db3db6d15",  # Our bot (main)
    "0x8a3a19aec04eeb6e3c183ee5750d06fe5c08066a",  # Our bot (test)
    "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",  # GamblingIsAllYouNeed (already tracking)
}

SPORT_TAGS = [
    "esports",   # CS2, LoL, Valorant, Dota2
    "soccer",    # all football leagues
    "mlb",       # baseball
    "nba",       # basketball
    "nhl",       # hockey
    "nfl",       # american football
    "cbb",       # college basketball
    "tennis",    # ATP/WTA
    "103097",    # CBA (Chinese basketball)
    "euroleague",
]


def get_events(tag, max_events=30):
    """Fetch active+recent events for a sport tag."""
    param = f"tag_id={tag}" if tag[0].isdigit() else f"tag_slug={tag}"
    try:
        # Active events (no end_date_max — some sports have far-future endDates)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = fetch(f"https://gamma-api.polymarket.com/events?active=true&closed=false&{param}&end_date_min={now}&limit={max_events}")
        return data
    except Exception as e:
        print(f"    Error fetching {tag}: {e}", flush=True)
        return []


def get_holders(condition_id):
    """Get top holders for a market."""
    try:
        data = fetch(f"{API}/holders?market={condition_id}")
        holders = []
        for group in data:
            for h in group.get("holders", []):
                wallet = h.get("proxyWallet", "").lower()
                amount = float(h.get("amount", 0) or 0)
                name = h.get("name", h.get("pseudonym", ""))
                if wallet and amount > 0 and wallet not in EXCLUDE:
                    holders.append({"wallet": wallet, "name": name, "shares": amount})
        return holders
    except:
        return []


def scan_sport(tag, max_events=20):
    """Scan holders across events for one sport tag. Returns {wallet: {count, names, events}}."""
    events = get_events(tag, max_events)
    # Only daily matches (slug contains date pattern)
    daily = [e for e in events if any(f"2026-0" in e.get("slug", "") or f"2026-1" in e.get("slug", "") for _ in [1])]
    if not daily:
        daily = events[:max_events]

    wallet_stats = defaultdict(lambda: {"count": 0, "names": set(), "events": set()})
    markets_scanned = 0

    for event in daily[:max_events]:
        markets = event.get("markets", [])
        # Only scan the first 2 markets per event (ML + maybe spread)
        for market in markets[:2]:
            cid = market.get("conditionId", "")
            if not cid:
                continue
            for h in get_holders(cid):
                ws = wallet_stats[h["wallet"]]
                ws["count"] += 1
                ws["names"].add(h["name"])
                ws["events"].add(event.get("slug", ""))
            markets_scanned += 1
            time.sleep(0.15)

    # Filter: must appear in 5+ different events
    frequent = {w: s for w, s in wallet_stats.items() if len(s["events"]) >= 5}
    for s in frequent.values():
        s["names"] = list(s["names"])
        s["events"] = list(s["events"])

    return frequent, markets_scanned, len(daily)


def analyse_candidate(wallet, cache):
    """Run hauptbet analysis on a wallet. Uses cache to avoid re-fetching.

    Uses fetch_and_merge() — no pagination limits, sanity check enforced.
    Raises ValueError if data is biased (merge vs lb-api gap too large).
    """
    if wallet not in cache:
        all_conds, lb_profit, sanity_gap, _ = fetch_and_merge(wallet)
        cache[wallet] = (all_conds, lb_profit)
    return cache[wallet]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 70, flush=True)
    print("COPY TARGET DISCOVERY — All Sports", flush=True)
    print("=" * 70, flush=True)

    # Step 1: Scan holders per sport
    all_candidates = defaultdict(lambda: {"count": 0, "names": set(), "events": set(), "sports": set()})

    for tag in SPORT_TAGS:
        print(f"\n  Scanning {tag}...", end="", flush=True)
        frequent, markets, events = scan_sport(tag, max_events=15)
        print(f" {events} events, {markets} markets, {len(frequent)} candidates", flush=True)

        for wallet, stats in frequent.items():
            c = all_candidates[wallet]
            c["count"] += stats["count"]
            c["names"].update(stats["names"])
            c["events"].update(stats["events"])
            c["sports"].add(tag)

    # Convert sets
    for c in all_candidates.values():
        c["names"] = list(c["names"])
        c["events"] = list(c["events"])
        c["sports"] = list(c["sports"])

    # Filter: 5+ events across any sport
    qualified = {w: c for w, c in all_candidates.items() if len(c["events"]) >= 5}
    print(f"\n  Total candidates (5+ events): {len(qualified)}", flush=True)

    # Step 2: Hauptbet analysis on top 30 (by frequency)
    ranked = sorted(qualified.items(), key=lambda x: -len(x[1]["events"]))[:30]

    print(f"\n{'=' * 70}", flush=True)
    print(f"HAUPTBET ANALYSIS — Top {len(ranked)} most active", flush=True)
    print(f"{'=' * 70}", flush=True)

    cache = {}
    results = []

    for wallet, stats in ranked:
        name = stats["names"][0] if stats["names"] else wallet[:15]
        sports_str = ",".join(sorted(stats["sports"]))
        print(f"\n  {name[:25]} ({len(stats['events'])} events, {sports_str})...", flush=True)

        try:
            conds, lb = analyse_candidate(wallet, cache)

            # Overall hauptbet
            all_sports = set()
            for v in conds.values():
                all_sports.add(classify_sport(v["title"], v.get("event_slug", "")))

            sport_results = {}
            for sport in all_sports:
                sport_conds = {k: v for k, v in conds.items() if classify_sport(v["title"], v.get("event_slug", "")) == sport}
                if len(sport_conds) < 5:
                    continue
                hb = hauptbet_analysis(sport_conds, sport)
                if hb["games"] >= 10:
                    sport_results[sport] = hb
                    flag = " <<<" if hb["wr"] >= 55 and hb["roi"] > 5 else ""
                    print(f"    {sport:12s}: {hb['games']:4d}g | WR={hb['wr']:5.1f}% | ROI={hb['roi']:+6.1f}%{flag}", flush=True)

            results.append({
                "wallet": wallet,
                "name": name,
                "appearances": len(stats["events"]),
                "sports": stats["sports"],
                "lb_api_total_pnl": round(lb, 2) if lb else None,
                "hauptbet_per_sport": sport_results,
            })

        except ValueError as e:
            print(f"    ⚠️ BIAS REJECTED: {e}", flush=True)
        except Exception as e:
            print(f"    ERROR: {e}", flush=True)

    # Step 3: Summary — wallets with edge (WR>=55%, ROI>5% on 10+ games)
    print(f"\n{'=' * 70}", flush=True)
    print("COPY TARGETS — WR >= 55% AND ROI > 5% (min 10 games)", flush=True)
    print(f"{'=' * 70}", flush=True)

    targets = []
    for r in results:
        for sport, hb in r.get("hauptbet_per_sport", {}).items():
            if hb["wr"] >= 55 and hb["roi"] > 5 and hb["games"] >= 10:
                targets.append({
                    "name": r["name"],
                    "wallet": r["wallet"],
                    "sport": sport,
                    "games": hb["games"],
                    "wr": hb["wr"],
                    "roi": hb["roi"],
                    "pnl": hb.get("pnl", 0),
                    "lb_total": r.get("lb_api_total_pnl"),
                })

    targets.sort(key=lambda x: (-x["wr"], -x["roi"]))

    print(f"\n{'Name':<25} {'Sport':<12} {'Games':>6} {'WR':>7} {'ROI':>8} {'PnL':>12} {'Total':>12}", flush=True)
    print("-" * 85, flush=True)
    for t in targets:
        lb = f"${t['lb_total']:,.0f}" if t["lb_total"] else "?"
        print(f"{t['name'][:24]:<25} {t['sport']:<12} {t['games']:>6} {t['wr']:>6.1f}% {t['roi']:>+7.1f}% ${t['pnl']:>+10,.0f} {lb:>12}", flush=True)

    # Save
    output = {
        "date": today,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sport_tags_scanned": SPORT_TAGS,
        "total_candidates": len(qualified),
        "analysed": len(results),
        "targets": targets,
        "full_results": results,
    }
    outpath = os.path.join(OUT_DIR, f"copy-targets-{today}.json")
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved to {outpath}", flush=True)


if __name__ == "__main__":
    main()
