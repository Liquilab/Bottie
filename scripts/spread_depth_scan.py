#!/usr/bin/env python3
"""Spread Depth Scanner — assess liquidity for merge-spread strategy.

Scans Polymarket sport win markets to measure:
- Best ask depth per side (YES/NO)
- Combined ask spread (YES ask + NO ask vs $1.00)
- Depth available within 1c of best ask

Usage:
    python3 scripts/spread_depth_scan.py
    python3 scripts/spread_depth_scan.py --sports premier-league,nba,nhl
"""
import json
import sys
import time
import urllib.request

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
RATE_LIMIT = 0.08  # faster scanning


def api_get(url, retries=1):
    for attempt in range(retries + 1):
        try:
            time.sleep(RATE_LIMIT)
            req = urllib.request.Request(url, headers={
                "User-Agent": "SpreadScan/1",
                "Accept": "application/json",
            })
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read())
        except Exception as e:
            if attempt == retries:
                return None
            time.sleep(0.5)


def fetch_events(tag_slug, limit=50):
    url = f"{GAMMA_API}/events?active=true&closed=false&tag_slug={tag_slug}&limit={limit}"
    data = api_get(url)
    if not data or not isinstance(data, list):
        return []
    return data


def fetch_book(token_id):
    url = f"{CLOB_API}/book?token_id={token_id}"
    return api_get(url)


def parse_asks(book_side):
    """Return (best_ask_price, depth_at_best_USDC, depth_within_1c_USDC)."""
    if not book_side:
        return None, 0, 0

    asks = sorted(book_side, key=lambda x: float(x["price"]))
    if not asks:
        return None, 0, 0

    best_price = float(asks[0]["price"])
    best_depth = float(asks[0]["size"]) * best_price  # USDC value

    # Total depth within 1c of best ask (inclusive)
    depth_1c = 0
    for ask in asks:
        p = float(ask["price"])
        if p <= best_price + 0.01 + 0.0001:
            depth_1c += float(ask["size"]) * p
        else:
            break

    return best_price, best_depth, depth_1c


def is_match_event(title):
    """Return True if this looks like a game/match event (has vs/v in title)."""
    t = title.lower()
    return " vs" in t or " vs." in t or " v " in t


def scan_markets(sports):
    results = []
    seen = set()
    total_books_fetched = 0

    for sport in sports:
        sys.stdout.write(f"\n--- {sport} ---\n")
        sys.stdout.flush()
        events = fetch_events(sport)
        match_events = [e for e in events if is_match_event(e.get("title", ""))]
        sys.stdout.write(f"  {len(events)} events, {len(match_events)} matches\n")
        sys.stdout.flush()

        for event in match_events:
            title = event.get("title", "Unknown")
            markets = event.get("markets", [])

            for market in markets:
                cond_id = market.get("conditionId", "")
                if cond_id in seen:
                    continue
                seen.add(cond_id)

                tokens_raw = market.get("clobTokenIds")
                if not tokens_raw:
                    continue
                if isinstance(tokens_raw, str):
                    try:
                        tokens = json.loads(tokens_raw)
                    except:
                        continue
                else:
                    tokens = tokens_raw

                if len(tokens) != 2:
                    continue

                outcomes_raw = market.get("outcomes")
                if isinstance(outcomes_raw, str):
                    try:
                        outcomes = json.loads(outcomes_raw)
                    except:
                        outcomes = ["Yes", "No"]
                else:
                    outcomes = outcomes_raw or ["Yes", "No"]

                market_q = market.get("question", title)

                # For match events: only scan "win" markets and main moneyline
                # Skip spreads, totals, player props etc to keep scan fast
                q_lower = market_q.lower()
                is_win_market = ("win" in q_lower or
                                 "beat" in q_lower or
                                 "winner" in q_lower or
                                 # Main moneyline: question matches event title pattern
                                 "vs" in q_lower or
                                 "v " in q_lower)
                # Skip obvious non-win markets
                is_skip = ("over" in q_lower or "under" in q_lower or
                           "spread" in q_lower or "total" in q_lower or
                           "points" in q_lower or "goals" in q_lower or
                           "score" in q_lower or "assist" in q_lower or
                           "rebound" in q_lower or "three" in q_lower or
                           "passing" in q_lower or "rushing" in q_lower or
                           "corner" in q_lower or "card" in q_lower or
                           "half" in q_lower or "quarter" in q_lower or
                           "inning" in q_lower or "period" in q_lower or
                           "strikeout" in q_lower or "hit" in q_lower or
                           "home run" in q_lower or "shutout" in q_lower or
                           "margin" in q_lower or "exact" in q_lower or
                           "double" in q_lower or "triple" in q_lower or
                           "both teams" in q_lower)
                if is_skip and not is_win_market:
                    continue

                # Fetch orderbooks
                book_yes = fetch_book(tokens[0])
                book_no = fetch_book(tokens[1])
                total_books_fetched += 2

                if not book_yes or not book_no:
                    continue

                yes_price, yes_depth, yes_depth_1c = parse_asks(book_yes.get("asks", []))
                no_price, no_depth, no_depth_1c = parse_asks(book_no.get("asks", []))

                if yes_price is None or no_price is None:
                    continue

                combined = yes_price + no_price
                spread_over = combined - 1.0

                results.append({
                    "sport": sport,
                    "event": title,
                    "market": market_q,
                    "yes_ask": yes_price,
                    "yes_depth": yes_depth,
                    "yes_depth_1c": yes_depth_1c,
                    "no_ask": no_price,
                    "no_depth": no_depth,
                    "no_depth_1c": no_depth_1c,
                    "combined_ask": combined,
                    "spread_over": spread_over,
                })

            # Progress indicator
            sys.stdout.write(".")
            sys.stdout.flush()

    sys.stdout.write(f"\n\nDone. Fetched {total_books_fetched} orderbooks.\n")
    sys.stdout.flush()
    return results


def print_results(results):
    if not results:
        print("\nNo match markets found.")
        return

    results.sort(key=lambda r: r["spread_over"])

    print(f"\n{'='*130}")
    print(f"SPREAD DEPTH SCAN — {len(results)} binary match markets")
    print(f"{'='*130}")

    header = (f"{'Sport':<16} {'Market':<50} {'Y Ask':>6} {'Y $':>7} {'N Ask':>6} {'N $':>7} "
              f"{'Comb':>6} {'Over':>7} {'1c Y$':>7} {'1c N$':>7}")
    print(header)
    print("-" * 130)

    for r in results:
        name = r["market"][:48]
        sport_short = r["sport"][:14]
        print(f"{sport_short:<16} {name:<50} {r['yes_ask']:>6.3f} {r['yes_depth']:>7.0f} "
              f"{r['no_ask']:>6.3f} {r['no_depth']:>7.0f} "
              f"{r['combined_ask']:>6.3f} {r['spread_over']:>+7.4f} "
              f"{r['yes_depth_1c']:>7.0f} {r['no_depth_1c']:>7.0f}")

    # === AGGREGATE STATS ===
    print(f"\n{'='*80}")
    print("AGGREGATE STATS")
    print(f"{'='*80}")

    n = len(results)
    avg_combined = sum(r["combined_ask"] for r in results) / n
    avg_yes_depth = sum(r["yes_depth"] for r in results) / n
    avg_no_depth = sum(r["no_depth"] for r in results) / n
    avg_yes_1c = sum(r["yes_depth_1c"] for r in results) / n
    avg_no_1c = sum(r["no_depth_1c"] for r in results) / n

    tight_3c = sum(1 for r in results if r["spread_over"] < 0.03)
    tight_5c = sum(1 for r in results if r["spread_over"] < 0.05)
    negative = sum(1 for r in results if r["spread_over"] < 0)
    deep_50 = sum(1 for r in results if min(r["yes_depth"], r["no_depth"]) >= 50)
    deep_100 = sum(1 for r in results if min(r["yes_depth"], r["no_depth"]) >= 100)

    spreads = sorted(r["spread_over"] for r in results)
    min_s, med_s, max_s = spreads[0], spreads[n // 2], spreads[-1]

    print(f"  Total match markets:             {n}")
    print(f"  Average combined ask:            ${avg_combined:.4f}")
    print(f"  Min / Median / Max spread:       {min_s:+.4f} / {med_s:+.4f} / {max_s:+.4f}")
    print(f"  Markets with spread < 0 (arb):   {negative} ({100*negative/n:.0f}%)")
    print(f"  Markets with spread < 3c:        {tight_3c} ({100*tight_3c/n:.0f}%)")
    print(f"  Markets with spread < 5c:        {tight_5c} ({100*tight_5c/n:.0f}%)")
    print()
    print(f"  Avg YES depth at best ask:       ${avg_yes_depth:.0f}")
    print(f"  Avg NO depth at best ask:        ${avg_no_depth:.0f}")
    print(f"  Avg YES depth within 1c:         ${avg_yes_1c:.0f}")
    print(f"  Avg NO depth within 1c:          ${avg_no_1c:.0f}")
    print(f"  Markets with >$50 depth/side:    {deep_50} ({100*deep_50/n:.0f}%)")
    print(f"  Markets with >$100 depth/side:   {deep_100} ({100*deep_100/n:.0f}%)")

    # By sport breakdown
    print(f"\n{'='*80}")
    print("BY SPORT")
    print(f"{'='*80}")
    from collections import defaultdict
    by_sport = defaultdict(list)
    for r in results:
        by_sport[r["sport"]].append(r)
    for sport, rs in sorted(by_sport.items()):
        avg_sp = sum(r["spread_over"] for r in rs) / len(rs)
        avg_d = sum(min(r["yes_depth_1c"], r["no_depth_1c"]) for r in rs) / len(rs)
        tight = sum(1 for r in rs if r["spread_over"] < 0.03)
        print(f"  {sport:<20} {len(rs):>3} mkts  avg spread {avg_sp:+.4f}  avg min depth ${avg_d:>6.0f}  <3c: {tight}")

    # Top opportunities
    print(f"\n{'='*80}")
    print("TOP 10 TIGHTEST SPREADS")
    print(f"{'='*80}")
    for r in results[:10]:
        min_d = min(r["yes_depth_1c"], r["no_depth_1c"])
        print(f"  {r['spread_over']:+.4f}  min_depth=${min_d:>6.0f}  {r['sport']:<16} {r['market'][:55]}")

    # Arb opportunities
    arbs = [r for r in results if r["spread_over"] < 0]
    if arbs:
        print(f"\n{'='*80}")
        print(f"ARBITRAGE OPS (combined ask < $1.00): {len(arbs)}")
        print(f"{'='*80}")
        for r in arbs:
            min_d = min(r["yes_depth_1c"], r["no_depth_1c"])
            print(f"  {r['spread_over']:+.4f}  Y={r['yes_ask']:.3f} N={r['no_ask']:.3f}  "
                  f"depth=${min_d:.0f}  {r['market'][:55]}")


if __name__ == "__main__":
    # Tag slugs that actually have match (vs) events on Polymarket
    sports = [
        "nba",            # NBA game markets
        "nhl",            # NHL game markets
        "mlb",            # MLB game markets
        "premier-league", # EPL matches
        "mls",            # MLS matches
        "soccer",         # Other soccer
    ]

    if len(sys.argv) > 1 and sys.argv[1] == "--sports":
        sports = sys.argv[2].split(",")

    print(f"Scanning: {', '.join(sports)}")
    results = scan_markets(sports)
    print_results(results)
