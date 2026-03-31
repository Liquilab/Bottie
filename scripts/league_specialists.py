#!/usr/bin/env python3
"""League Specialists Discovery — Find consistent football bettors per league.

1. Fetch football events per league via Gamma API (tag_slug=soccer)
2. Per event → conditionId → /holders API → collect wallet addresses
3. Frequency table: which wallets appear in 3+ events per league
4. Top candidates → hauptbet analysis via lib/analyse.py
"""

import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from lib.analyse import (
    classify_sport, hauptbet_analysis, fetch_and_merge,
    get_lb_profit, fetch, API,
)

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "wallet-scout")

EXCLUDE = {
    "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b",  # Cannae
    "0x07921379f7b31ef93da634b688b2fe36897db778",  # ewelmealt
    "0x9f23f6d5d18f9fc5aef42efec8f63a7db3db6d15",  # Our bot
}

FOOTBALL_PREFIXES = [
    "epl", "bun", "lal", "fl1", "es2", "bl2", "ere", "por", "arg", "tur",
    "spl", "elc", "ucl", "uel", "uef", "mls", "mex", "fr2", "itc", "sea",
    "rou1", "efa", "acn", "cde", "cdr", "fif", "aus", "bra", "efl",
    "col1", "dfb", "nor", "den", "ind", "jap", "kor", "chi", "per1",
    "bol1", "cze1", "egy1", "mar1", "ukr1", "rus", "ssc",
]


def get_league(slug: str) -> str | None:
    if not slug:
        return None
    prefix = slug.split("-")[0]
    return prefix if prefix in FOOTBALL_PREFIXES else None


def fetch_football_events(max_events=500) -> list:
    """Fetch football events from Gamma API using tag_slug=soccer."""
    all_events = []
    offset = 0
    while len(all_events) < max_events:
        batch = fetch(
            f"https://gamma-api.polymarket.com/events?limit=50&tag_slug=soccer"
            f"&closed=true&order=endDate&ascending=false&offset={offset}"
        )
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
        time.sleep(0.3)
    return all_events


def events_by_league(events: list) -> dict:
    """Group events by league prefix from slug."""
    leagues = defaultdict(list)
    for e in events:
        slug = e.get("slug", "")
        league = get_league(slug)
        if league:
            leagues[league].append(e)
    return dict(leagues)


def get_holders(condition_id: str) -> list:
    try:
        data = fetch(f"{API}/holders?market={condition_id}")
        holders = []
        for group in data:
            for h in group.get("holders", []):
                wallet = h.get("proxyWallet", "").lower()
                amount = float(h.get("amount", 0) or 0)
                name = h.get("name", h.get("pseudonym", ""))
                if wallet and amount > 0:
                    holders.append({"wallet": wallet, "name": name, "shares": amount})
        return holders
    except Exception:
        return []


def discover_bettors(league_events: dict, max_events: int = 15) -> dict:
    """Per league, find wallets appearing in 3+ events."""
    league_wallets = {}

    for league, events in sorted(league_events.items()):
        wallet_stats = defaultdict(lambda: {"count": 0, "shares": 0, "names": set(), "events": set()})
        scan = events[:max_events]
        print(f"  {league}: {len(scan)} events...", end="", flush=True)

        for event in scan:
            for market in event.get("markets", []):
                cid = market.get("conditionId", "")
                if not cid:
                    continue
                for h in get_holders(cid):
                    w = h["wallet"]
                    if w in EXCLUDE:
                        continue
                    ws = wallet_stats[w]
                    ws["count"] += 1
                    ws["shares"] += h["shares"]
                    ws["names"].add(h["name"])
                    ws["events"].add(event.get("slug", ""))
                time.sleep(0.15)

        frequent = {w: s for w, s in wallet_stats.items() if len(s["events"]) >= 3}
        for s in frequent.values():
            s["names"] = list(s["names"])
            s["events"] = list(s["events"])

        league_wallets[league] = frequent
        print(f" {len(frequent)} candidates ({len(wallet_stats)} unique)")

    return league_wallets


def analyse_candidates(league_wallets: dict, top_n: int = 5) -> dict:
    """Per league, hauptbet analyse on top N frequent bettors."""
    results = {}
    cache = {}

    for league, wallets in sorted(league_wallets.items()):
        if not wallets:
            continue

        ranked = sorted(wallets.items(), key=lambda x: (-len(x[1]["events"]), -x[1]["shares"]))
        candidates = ranked[:top_n]
        print(f"\n  {league}: analysing {len(candidates)}...", flush=True)
        league_results = []

        for wallet, stats in candidates:
            name = stats["names"][0] if stats["names"] else wallet[:15]
            print(f"    {name[:20]}...", end="", flush=True)

            try:
                if wallet not in cache:
                    all_conds, lb_profit, sanity_gap, _ = fetch_and_merge(wallet)
                    cache[wallet] = (all_conds, lb_profit)

                conds, lb = cache[wallet]

                # League-specific hauptbet
                league_conds = {k: v for k, v in conds.items() if get_league(v["event_slug"]) == league}
                league_hb = hauptbet_analysis(league_conds, "football") if league_conds else {"games": 0, "wr": 0, "roi": 0, "pnl": 0}

                # All football hauptbet
                fb_conds = {k: v for k, v in conds.items() if classify_sport(v["title"], v.get("event_slug", "")) == "football"}
                all_hb = hauptbet_analysis(fb_conds, "football") if fb_conds else {"games": 0, "wr": 0, "roi": 0}

                result = {
                    "wallet": wallet,
                    "name": name,
                    "appearances": len(stats["events"]),
                    "league_hauptbet": league_hb,
                    "all_football_hauptbet": all_hb,
                    "lb_api_total_pnl": round(lb, 2) if lb else None,
                }
                league_results.append(result)
                lg = league_hb
                print(f" {lg['games']}g WR={lg['wr']}% ROI={lg['roi']}%")

            except Exception as e:
                print(f" ERROR: {e}")

        league_results.sort(key=lambda x: (
            -(x["league_hauptbet"]["wr"] if x["league_hauptbet"]["games"] >= 5 else 0),
            -(x["league_hauptbet"]["roi"] if x["league_hauptbet"]["games"] >= 5 else -999),
        ))
        results[league] = league_results

    return results


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 70)
    print("LEAGUE SPECIALISTS DISCOVERY")
    print("=" * 70)

    print("\nStep 1: Fetching football events from Polymarket...")
    events = fetch_football_events(max_events=300)
    print(f"  Total events: {len(events)}")

    leagues = events_by_league(events)
    for league, evts in sorted(leagues.items(), key=lambda x: -len(x[1])):
        print(f"  {league:8s}: {len(evts)} events")

    print(f"\nStep 2: Scanning holders per market...")
    league_wallets = discover_bettors(leagues, max_events=10)

    print(f"\nStep 3: Hauptbet analysis on candidates...")
    results = analyse_candidates(league_wallets, top_n=5)

    print("\n" + "=" * 70)
    print("TOP 3 PER LEAGUE (min 5 games)")
    print("=" * 70)

    for league, candidates in sorted(results.items()):
        qualified = [c for c in candidates if c["league_hauptbet"]["games"] >= 5]
        if not qualified:
            continue
        print(f"\n--- {league.upper()} ---")
        for i, c in enumerate(qualified[:3]):
            hb = c["league_hauptbet"]
            lb = c.get("lb_api_total_pnl")
            lb_str = f"${lb:,.0f}" if lb is not None else "?"
            print(f"  {i+1}. {c['name'][:25]:25s} | {hb['games']:3d}g | WR={hb['wr']:5.1f}% | ROI={hb['roi']:+6.1f}% | total={lb_str}")
            if hb.get("per_line"):
                for line, ls in hb["per_line"].items():
                    if ls["games"] >= 2:
                        print(f"     {line}: {ls['games']}g WR={ls['wr']}% ROI={ls['roi']}%")

    output = {"date": today, "timestamp": datetime.now(timezone.utc).isoformat(), "results": results}
    outpath = os.path.join(OUT_DIR, f"specialists-{today}.json")
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved to {outpath}")


if __name__ == "__main__":
    main()
