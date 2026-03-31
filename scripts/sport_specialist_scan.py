#!/usr/bin/env python3
"""Sport Specialist Scanner — Find the #1 wallet per sport by ROI.

Usage:
    python3 sport_specialist_scan.py --sport nba
    python3 sport_specialist_scan.py --sport football
    python3 sport_specialist_scan.py --sport nhl
    python3 sport_specialist_scan.py --sport mlb
    python3 sport_specialist_scan.py --sport nfl

Per sport:
1. Fetch 30+ events (active + recently closed) via Gamma API
2. Get ALL holders per market (not just top 2 markets)
3. Collect wallets appearing in 3+ events
4. Run FULL unbiased analysis (fetch_and_merge + sanity check) on top 30
5. Rank by hauptbet ROI → output the #1 specialist

SSOT: Uses lib/analyse.py for ALL analysis. No shortcuts, no limits.
"""

import json, os, sys, time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from lib.analyse import (
    fetch_and_merge, hauptbet_analysis, classify_sport,
    fetch, API,
)

EXCLUDE = {
    "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b",  # Cannae
    "0x07921379f7b31ef93da634b688b2fe36897db778",  # ewelmealt
    "0x9f23f6d5d18f9fc5aef42efec8f63a7db3db6d15",  # Our bot (main)
    "0x8a3a19aec04eeb6e3c183ee5750d06fe5c08066a",  # Our bot (test)
    "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",  # GamblingIsAllYouNeed
}

SPORT_TAGS = {
    "nba": ["nba"],
    "nhl": ["nhl"],
    "mlb": ["mlb"],
    "nfl": ["nfl"],
    "football": ["soccer"],
}

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "wallet-scout")


def get_events(tag, max_events=30, include_closed=True):
    """Fetch active + recently closed events for a sport tag."""
    events = []
    param = f"tag_id={tag}" if tag[0].isdigit() else f"tag_slug={tag}"

    # Active events
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        active = fetch(f"https://gamma-api.polymarket.com/events?active=true&closed=false&{param}&limit={max_events}")
        events.extend(active or [])
    except Exception as e:
        print(f"    Error fetching active {tag}: {e}", flush=True)

    # Recently closed (last 14 days) — critical for finding wallets with resolved bets
    if include_closed:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
            closed = fetch(f"https://gamma-api.polymarket.com/events?active=false&closed=true&{param}&end_date_min={cutoff}&limit={max_events}")
            events.extend(closed or [])
        except Exception as e:
            print(f"    Error fetching closed {tag}: {e}", flush=True)

    # Dedup by slug
    seen = set()
    deduped = []
    for e in events:
        slug = e.get("slug", "")
        if slug and slug not in seen:
            seen.add(slug)
            deduped.append(e)
    return deduped


def get_holders(condition_id):
    """Get ALL holders for a market."""
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


def scan_holders(sport):
    """Scan ALL holders across events for a sport. Returns {wallet: {count, names, events}}."""
    tags = SPORT_TAGS[sport]
    wallet_stats = defaultdict(lambda: {"count": 0, "names": set(), "events": set()})
    total_markets = 0

    for tag in tags:
        events = get_events(tag, max_events=30, include_closed=True)
        print(f"  {tag}: {len(events)} events", flush=True)

        for event in events:
            # Scan ALL markets per event (not just 2!)
            for market in event.get("markets", []):
                cid = market.get("conditionId", "")
                if not cid:
                    continue
                for h in get_holders(cid):
                    ws = wallet_stats[h["wallet"]]
                    ws["count"] += 1
                    ws["names"].add(h["name"])
                    ws["events"].add(event.get("slug", ""))
                total_markets += 1
                time.sleep(0.1)  # Rate limit

    # Filter: 3+ different events (lower threshold for broader coverage)
    frequent = {w: s for w, s in wallet_stats.items() if len(s["events"]) >= 3}
    for s in frequent.values():
        s["names"] = list(s["names"])
        s["events"] = list(s["events"])

    print(f"  Scanned {total_markets} markets, {len(wallet_stats)} unique wallets, {len(frequent)} with 3+ events", flush=True)
    return frequent


MAX_POSITIONS = 5000  # Skip wallets with too many positions (OOM risk)


def analyse_wallet_for_sport(wallet, name, sport):
    """Run full unbiased analysis for one wallet in one sport.

    Returns dict with results or None if biased/error.
    """
    try:
        # Quick position count check to avoid OOM on mega-wallets
        probe = fetch(f"{API}/positions?user={wallet}&limit=1&offset={MAX_POSITIONS}&sizeThreshold=0")
        if probe and len(probe) > 0:
            print(f"    ✗ {name}: SKIP — >{MAX_POSITIONS} positions (OOM risk)", flush=True)
            return None
        all_conds, lb_profit, sanity_gap, _ = fetch_and_merge(wallet)
        sport_conds = {k: v for k, v in all_conds.items()
                       if classify_sport(v["title"], v.get("event_slug", "")) == sport}
        if len(sport_conds) < 5:
            return None

        hb = hauptbet_analysis(sport_conds, sport)
        if hb["games"] < 10:
            return None

        return {
            "wallet": wallet,
            "name": name,
            "sport": sport,
            "games": hb["games"],
            "wins": hb["wins"],
            "losses": hb["losses"],
            "wr": hb["wr"],
            "roi": hb["roi"],
            "pnl": hb["pnl"],
            "invested": hb["invested"],
            "per_line": hb["per_line"],
            "lb_total_pnl": round(lb_profit, 2) if lb_profit else None,
            "sanity_gap": sanity_gap,
            "total_conditions": len(all_conds),
        }
    except ValueError as e:
        print(f"    ⚠️ {name}: BIAS REJECTED — {e}", flush=True)
        return None
    except Exception as e:
        print(f"    ✗ {name}: ERROR — {e}", flush=True)
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", required=True, choices=list(SPORT_TAGS.keys()))
    parser.add_argument("--top", type=int, default=30, help="Max wallets to analyse")
    parser.add_argument("--parallel", type=int, default=3, help="Parallel analysis workers")
    args = parser.parse_args()

    sport = args.sport
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"{'=' * 70}", flush=True)
    print(f"SPORT SPECIALIST SCAN — {sport.upper()}", flush=True)
    print(f"{'=' * 70}", flush=True)

    # Step 1: Scan holders
    print(f"\nStep 1: Scanning holders...", flush=True)
    candidates = scan_holders(sport)

    # Step 2: Quick pre-filter with lb-api profit (1 call per wallet, fast)
    ranked_by_freq = sorted(candidates.items(), key=lambda x: -len(x[1]["events"]))[:args.top * 2]
    print(f"\nStep 2: Pre-filter {len(ranked_by_freq)} candidates via lb-api profit...", flush=True)

    from lib.analyse import get_lb_profit
    pre_filtered = []
    for wallet, stats in ranked_by_freq:
        name = stats["names"][0] if stats["names"] else wallet[:15]
        try:
            lb = get_lb_profit(wallet)
            if lb is not None and lb > 0:
                if lb > 200_000:
                    print(f"    ✗ {name[:25]}: ${lb:+,.0f} (whale, skip — too many positions)", flush=True)
                    continue
                pre_filtered.append((wallet, stats, lb))
                print(f"    ✓ {name[:25]}: ${lb:+,.0f}", flush=True)
            else:
                print(f"    ✗ {name[:25]}: ${lb:+,.0f} (skip)" if lb else f"    ✗ {name[:25]}: no data (skip)", flush=True)
        except:
            print(f"    ✗ {name[:25]}: error (skip)", flush=True)

    # Sort by event count (sport-specific activity), not total lb profit
    # This avoids starting with mega-whales whose fetch_and_merge is too heavy
    pre_filtered.sort(key=lambda x: -len(x[1]["events"]))
    ranked = [(w, s) for w, s, _ in pre_filtered[:args.top]]
    print(f"\n  {len(ranked)} profitable wallets → full analysis", flush=True)

    # Step 3: Analyse in parallel (only profitable wallets)
    print(f"\nStep 3: Full unbiased analysis on {len(ranked)} wallets...", flush=True)
    results = []

    def analyse_one(item):
        wallet, stats = item
        name = stats["names"][0] if stats["names"] else wallet[:15]
        events_count = len(stats["events"])
        print(f"    → {name[:25]} ({events_count} events)...", flush=True)
        result = analyse_wallet_for_sport(wallet, name, sport)
        if result:
            result["events_found"] = events_count
            flag = " <<<" if result["wr"] >= 50 and result["roi"] > 5 else ""
            print(f"      {result['games']:4d}g | WR={result['wr']:5.1f}% | ROI={result['roi']:+6.1f}% | PnL=${result['pnl']:+,.0f} | gap={result['sanity_gap']}%{flag}", flush=True)
        else:
            print(f"      (insufficient data or bias rejected)", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(analyse_one, item): item for item in ranked}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    # Step 4: Rank by ROI
    results.sort(key=lambda x: -x["roi"])

    print(f"\n{'=' * 70}", flush=True)
    print(f"RESULTS — {sport.upper()} SPECIALISTS (ranked by ROI)", flush=True)
    print(f"{'=' * 70}", flush=True)
    print(f"\n{'Name':<25} {'Games':>6} {'WR':>6} {'ROI':>8} {'PnL':>12} {'Gap':>6} {'Events':>6}", flush=True)
    print("-" * 75, flush=True)

    for r in results:
        flag = " <<<" if r["wr"] >= 50 and r["roi"] > 5 else ""
        gap_str = f"{r['sanity_gap']:.0f}%" if r['sanity_gap'] is not None else "?"
        print(f"{r['name'][:24]:<25} {r['games']:>6} {r['wr']:>5.1f}% {r['roi']:>+7.1f}% ${r['pnl']:>+10,.0f} {gap_str:>6} {r['events_found']:>6}{flag}", flush=True)

    if results:
        best = results[0]
        print(f"\n★ BEST {sport.upper()} SPECIALIST: {best['name']} — {best['games']}g, WR={best['wr']}%, ROI={best['roi']:+.1f}%, PnL=${best['pnl']:+,.0f}", flush=True)
        print(f"  Wallet: {best['wallet']}", flush=True)
        print(f"  Sanity gap: {best['sanity_gap']}% (max 30%)", flush=True)
        print(f"  Per line: {json.dumps(best['per_line'], indent=2)}", flush=True)
    else:
        print(f"\nNo qualified specialists found for {sport}.", flush=True)

    # Save
    outpath = os.path.join(OUT_DIR, f"specialist-{sport}-{today}.json")
    with open(outpath, "w") as f:
        json.dump({
            "date": today,
            "sport": sport,
            "candidates_scanned": len(ranked),
            "results": results,
        }, f, indent=2, default=str)
    print(f"\nSaved to {outpath}", flush=True)


if __name__ == "__main__":
    main()
