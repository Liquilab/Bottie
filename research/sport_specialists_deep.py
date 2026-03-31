#!/usr/bin/env python3
"""
Deep dive on the top specialist candidates — fetch ALL closed positions.
Focus on wallets that showed promise in the initial scan.
"""

import requests
import json
import time
import re
from collections import defaultdict

HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

# Top candidates from initial scan + some extras worth deeper analysis
WALLETS = [
    ("CERTuo", "0xf195721ad850377c96cd634457c70cd9e8308057"),          # NHL specialist
    ("0x4924_anon", "0x492442eab586f242b53bda933fd5de859c8a3782"),     # NBA
    ("Countryside", "0xbddf61af533ff524d27154e589d2d7a81510c684"),     # NBA
    ("weflyhigh", "0x03e8a544e97eeff5753bc1e90d46e5ef22af1697"),       # NHL
    ("ewelmealt", "0x07921379f7b31ef93da634b688b2fe36897db778"),       # Football
    ("HorizonSplendidView", "0x02227b8f5a9636e895607edd3185ed6ee5598ff7"),  # Football
    ("Blessed-Sunshine", "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09"),     # NBA
    ("JPMorgan101", "0xb6d6e99d3bfe055874a04279f659f009fd57be17"),     # NBA/NHL
    ("bcda", "0xb45a797faa52b0fd8adc56d30382022b7b12192c"),           # MLB/NHL
    ("gatorr", "0x93abbc022ce98d6f45d4444b594791cc4b7a9723"),         # NHL
    ("Anointed-Connect", "0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6"),# NBA
    ("432614799197", "0xdc876e6873772d38716fda7f2452a78d426d7ab6"),    # Football
    ("beachboy4", "0xc2e7800b5af46e6093872b177b7a5e7f0563be51"),      # NBA
    ("RN1", "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"),             # NHL/Football
]

NHL_TEAMS = ["Bruins", "Penguins", "Oilers", "Ducks", "Wild", "Jets", "Panthers", "Devils",
             "Hurricanes", "Lightning", "Senators", "Blues", "Avalanche", "Stars", "Blackhawks",
             "Red Wings", "Sabres", "Flames", "Canucks", "Predators", "Capitals", "Islanders",
             "Flyers", "Kraken", "Sharks", "Coyotes", "Blue Jackets", "Canadiens", "Maple Leafs",
             "Rangers", "Kings", "Golden Knights", "Utah Hockey", "NHL"]

NBA_TEAMS = ["76ers", "Lakers", "Celtics", "Knicks", "Bulls", "Heat", "Hawks",
             "Jazz", "Suns", "Nets", "Pacers", "Bucks", "Spurs", "Rockets", "Nuggets",
             "Warriors", "Clippers", "Grizzlies", "Cavaliers", "Hornets", "Pistons",
             "Wizards", "Raptors", "Pelicans", "Blazers", "Timberwolves", "Mavericks",
             "Thunder", "Magic", "NBA"]

MLB_TEAMS = ["Yankees", "Dodgers", "Mets", "Braves", "Astros", "Phillies", "Padres", "Cubs",
             "Guardians", "Orioles", "Twins", "Tigers", "Mariners", "Royals", "Red Sox", "Rays",
             "Cardinals", "Brewers", "Diamondbacks", "Reds", "Pirates", "Rockies", "Athletics",
             "Marlins", "Nationals", "Angels", "White Sox", "MLB"]

NFL_TEAMS = ["Bills", "Dolphins", "Patriots", "Steelers", "Ravens", "Bengals", "Browns",
             "Titans", "Colts", "Jaguars", "Texans", "Chiefs", "Chargers", "Raiders", "Broncos",
             "Cowboys", "Giants", "Eagles", "Commanders", "Packers", "Vikings", "Bears", "Lions",
             "Falcons", "Saints", "Buccaneers", "Rams", "49ers", "Seahawks", "NFL", "Super Bowl"]

FOOTBALL_KEYWORDS = ["FC", " CF ", "United", " City ", "Arsenal", "Chelsea", "Liverpool", "Barcelona",
                     "Madrid", "Bayern", "Juventus", "Inter Milan", "AC Milan", "PSG", "Dortmund",
                     "Atletico", "Tottenham", "Manchester", "Leicester", "Everton", "Wolves",
                     "Crystal Palace", "Aston Villa", "Fulham", "Brentford", "Brighton",
                     "Bournemouth", "Nottingham", "Sheffield", "Burnley", "Luton",
                     "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
                     "Champions League", "Europa League", "MLS", "Copa", "World Cup",
                     "Eredivisie", "will draw", "DRAW", "Feyenoord", "Ajax", "PSV",
                     "Celtic", "Porto", "Benfica", "Sporting CP",
                     "Napoli", "Roma", "Lazio", "Atalanta", "Fiorentina"]

TENNIS_KEYWORDS = ["ATP", "WTA", "tennis", "Grand Slam", "Wimbledon", "French Open",
                   "Australian Open", "Djokovic", "Alcaraz", "Sinner",
                   "Medvedev", "Rublev", "Tsitsipas", "Zverev", "Ruud",
                   "Swiatek", "Sabalenka", "Gauff", "Rybakina"]


def classify_sport(title):
    if not title:
        return "Other"

    # Unambiguous NFL teams first
    nfl_unique = ["Bills", "Dolphins", "Patriots", "Steelers", "Ravens", "Bengals", "Browns",
                  "Titans", "Colts", "Jaguars", "Texans", "Chiefs", "Chargers", "Raiders", "Broncos",
                  "Cowboys", "Eagles", "Commanders", "Packers", "Vikings", "Bears", "Lions",
                  "Falcons", "Saints", "Buccaneers", "49ers", "Seahawks", "Super Bowl"]
    for team in nfl_unique:
        if team in title:
            return "NFL"
    if "NFL" in title:
        return "NFL"

    for team in NHL_TEAMS:
        if team in title:
            return "NHL"

    for team in NBA_TEAMS:
        if team in title:
            return "NBA"

    for team in MLB_TEAMS:
        if team in title:
            return "MLB"

    for kw in FOOTBALL_KEYWORDS:
        if kw in title:
            return "Football"

    for kw in TENNIS_KEYWORDS:
        if kw.lower() in title.lower():
            return "Tennis"

    return "Other"


def fetch_all_positions(address, endpoint="positions", max_records=5000):
    """Fetch ALL positions or closed-positions with full pagination."""
    all_data = []
    offset = 0
    limit = 500

    sort_params = ""
    if endpoint == "closed-positions":
        sort_params = "&sortBy=TIMESTAMP&sortOrder=DESC"

    while len(all_data) < max_records:
        url = f"https://data-api.polymarket.com/{endpoint}?user={address}&limit={limit}&offset={offset}&sizeThreshold=0{sort_params}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                print(f"    Rate limited, waiting 10s...")
                time.sleep(10)
                resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                print(f"    HTTP {resp.status_code} at offset {offset}")
                break
            data = resp.json()
            if not data:
                break
            all_data.extend(data)
            if len(data) < limit:
                break
            offset += limit
            time.sleep(0.4)
        except Exception as e:
            print(f"    Error: {e}")
            break
    return all_data


def analyse_wallet(username, address):
    print(f"\n{'='*70}")
    print(f"DEEP DIVE: {username} ({address})")

    open_pos = fetch_all_positions(address, "positions", max_records=5000)
    time.sleep(0.5)
    closed_pos = fetch_all_positions(address, "closed-positions", max_records=3000)

    print(f"  Fetched: {len(open_pos)} open, {len(closed_pos)} closed")

    # Build sport stats
    sport_stats = defaultdict(lambda: {
        "wins": 0, "losses": 0, "pending": 0,
        "pnl": 0.0, "invested": 0.0,
        "events": defaultdict(list),
        "positions": [],
    })

    total_sport_pos = 0

    # Process closed positions
    for pos in closed_pos:
        title = pos.get("title", "") or pos.get("marketTitle", "") or pos.get("groupTitle", "") or ""
        sport = classify_sport(title)
        if sport == "Other":
            continue

        realized_pnl = float(pos.get("realizedPnl", 0) or 0)
        initial_value = float(pos.get("initialValue", 0) or pos.get("cashPaid", 0) or 0)
        # If initialValue is 0 or missing, try to compute from size*avgPrice
        if initial_value == 0:
            size = float(pos.get("size", 0) or 0)
            avg = float(pos.get("avgPrice", 0) or 0)
            initial_value = size * avg

        sd = sport_stats[sport]
        sd["invested"] += abs(initial_value) if initial_value > 0 else abs(realized_pnl)
        sd["pnl"] += realized_pnl
        total_sport_pos += 1

        if realized_pnl > 0.01:
            sd["wins"] += 1
        elif realized_pnl < -0.01:
            sd["losses"] += 1
        # else: break-even, skip

        event = pos.get("eventSlug", "") or pos.get("groupSlug", "") or title[:40]
        outcome = pos.get("outcome", "") or pos.get("title", "")
        sd["events"][event].append({
            "size": abs(initial_value),
            "outcome": outcome,
            "pnl": realized_pnl,
            "source": "closed"
        })

    # Process open positions
    for pos in open_pos:
        title = pos.get("title", "") or pos.get("marketTitle", "") or pos.get("groupTitle", "") or ""
        sport = classify_sport(title)
        if sport == "Other":
            continue

        cur_price = float(pos.get("curPrice", 0.5) or 0.5)
        size = float(pos.get("size", 0) or 0)
        avg_price = float(pos.get("avgPrice", 0.5) or 0.5)
        initial_value = size * avg_price
        current_value = size * cur_price
        pnl = current_value - initial_value

        sd = sport_stats[sport]
        sd["invested"] += abs(initial_value)
        sd["pnl"] += pnl
        total_sport_pos += 1

        if cur_price >= 0.95:
            sd["wins"] += 1
        elif cur_price <= 0.05:
            sd["losses"] += 1
        else:
            sd["pending"] += 1

        event = pos.get("eventSlug", "") or pos.get("groupSlug", "") or title[:40]
        outcome = pos.get("outcome", "") or pos.get("title", "")
        sd["events"][event].append({
            "size": abs(initial_value),
            "outcome": outcome,
            "pnl": pnl,
            "source": "open",
            "cur_price": cur_price,
        })

    # Report per sport
    results = []
    for sport, sd in sorted(sport_stats.items()):
        resolved = sd["wins"] + sd["losses"]
        total = resolved + sd["pending"]
        if total < 5:
            continue

        wr = sd["wins"] / resolved if resolved > 0 else 0
        roi = sd["pnl"] / sd["invested"] if sd["invested"] > 0 else 0
        concentration = total / total_sport_pos if total_sport_pos > 0 else 0

        # Directionality check
        both_sides = 0
        total_events = len(sd["events"])
        for ev, legs in sd["events"].items():
            outcomes = set(l["outcome"] for l in legs)
            if len(outcomes) > 1:
                both_sides += 1
        dir_pct = both_sides / total_events if total_events > 0 else 0

        # Hauptbet analysis
        hauptbet_wins = 0
        hauptbet_total = 0
        for ev, legs in sd["events"].items():
            if len(legs) >= 2:
                largest = max(legs, key=lambda l: l["size"])
                hauptbet_total += 1
                if largest["pnl"] > 0:
                    hauptbet_wins += 1
        hauptbet_wr = hauptbet_wins / hauptbet_total if hauptbet_total > 0 else None

        r = {
            "username": username,
            "address": address,
            "sport": sport,
            "total_positions": total,
            "resolved": resolved,
            "wins": sd["wins"],
            "losses": sd["losses"],
            "pending": sd["pending"],
            "wr": wr,
            "roi": roi,
            "pnl": sd["pnl"],
            "invested": sd["invested"],
            "concentration": concentration,
            "directional_pct": dir_pct,
            "is_mm": dir_pct > 0.3,
            "num_events": total_events,
            "hauptbet_wr": hauptbet_wr,
            "hauptbet_n": hauptbet_total,
        }
        results.append(r)

        print(f"  {sport:<12} | pos={total:>5} res={resolved:>5} W={sd['wins']:>4} L={sd['losses']:>4} "
              f"WR={wr:>5.1%} ROI={roi:>7.1%} PnL=${sd['pnl']:>12,.0f} "
              f"conc={concentration:>4.0%} dir={dir_pct:>4.0%} "
              f"hWR={'N/A' if hauptbet_wr is None else f'{hauptbet_wr:.0%}':>4} ({hauptbet_total}ev)")

    return results


def main():
    all_results = []

    for username, address in WALLETS:
        try:
            results = analyse_wallet(username, address)
            all_results.extend(results)
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback; traceback.print_exc()
        time.sleep(1.5)

    # Final ranking
    print("\n\n" + "="*160)
    print("FINAL RANKING — ALL SPORT×WALLET COMBINATIONS (resolved >= 10, non-MM)")
    print("="*160)
    print(f"{'#':<4} {'Username':<25} {'Sport':<12} {'Pos':>5} {'Res':>5} {'W':>4} {'L':>4} "
          f"{'WR':>6} {'ROI':>8} {'PnL':>13} {'Invested':>13} {'Conc':>5} {'Dir%':>5} "
          f"{'hWR':>5} {'hN':>4}")
    print("-"*160)

    ranked = sorted(all_results, key=lambda r: r["roi"], reverse=True)
    i = 0
    for r in ranked:
        if r["resolved"] < 10:
            continue
        if r["is_mm"]:
            continue
        i += 1
        hwr = f"{r['hauptbet_wr']:.0%}" if r['hauptbet_wr'] is not None else "N/A"
        print(f"{i:<4} {r['username']:<25} {r['sport']:<12} {r['total_positions']:>5} {r['resolved']:>5} "
              f"{r['wins']:>4} {r['losses']:>4} {r['wr']:>5.1%} {r['roi']:>7.1%} "
              f"${r['pnl']:>12,.0f} ${r['invested']:>12,.0f} {r['concentration']:>4.0%} "
              f"{r['directional_pct']:>4.0%} {hwr:>5} {r['hauptbet_n']:>4}")

    # Also show MM flagged for completeness
    print("\n\n--- Market Makers (dir% > 30%, shown for reference) ---")
    for r in ranked:
        if r["resolved"] < 10 or not r["is_mm"]:
            continue
        hwr = f"{r['hauptbet_wr']:.0%}" if r['hauptbet_wr'] is not None else "N/A"
        print(f"  {r['username']:<25} {r['sport']:<12} res={r['resolved']:>5} WR={r['wr']:>5.1%} "
              f"ROI={r['roi']:>7.1%} dir={r['directional_pct']:.0%}")

    # TOP 3 SPECIALISTS
    top = [r for r in ranked if r["resolved"] >= 20 and not r["is_mm"] and r["roi"] > 0]
    print("\n\n" + "="*100)
    print("TOP 3 SPORT SPECIALISTS")
    print("="*100)
    for i, r in enumerate(top[:3], 1):
        hwr = f"{r['hauptbet_wr']:.0%}" if r['hauptbet_wr'] is not None else "N/A"
        print(f"\n  #{i}: {r['username']} — {r['sport']}")
        print(f"      Address:       {r['address']}")
        print(f"      Positions:     {r['total_positions']} ({r['resolved']} resolved, {r['pending']} pending)")
        print(f"      Win/Loss:      {r['wins']}W / {r['losses']}L = {r['wr']:.1%} WR")
        print(f"      ROI:           {r['roi']:.1%}")
        print(f"      PnL:           ${r['pnl']:,.0f}")
        print(f"      Invested:      ${r['invested']:,.0f}")
        print(f"      Concentration: {r['concentration']:.0%} of sport positions")
        print(f"      Events:        {r['num_events']} unique events")
        print(f"      Hauptbet WR:   {hwr} (n={r['hauptbet_n']})")
        print(f"      Directional:   {r['directional_pct']:.0%} (< 30% = directional trader)")

    # Save
    with open("/Users/koen/Projects/ Bottie/research/sport_specialists_deep_results.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n\nSaved {len(all_results)} results to research/sport_specialists_deep_results.json")


if __name__ == "__main__":
    main()
