#!/usr/bin/env python3
"""
Scan top Polymarket sports traders and analyse per-sport performance.
"""

import requests
import json
import time
import re
from collections import defaultdict

HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

WALLETS = [
    ("HorizonSplendidView", "0x02227b8f5a9636e895607edd3185ed6ee5598ff7"),
    ("beachboy4", "0xc2e7800b5af46e6093872b177b7a5e7f0563be51"),
    ("reachingthesky", "0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2"),
    ("0x4924_anon", "0x492442eab586f242b53bda933fd5de859c8a3782"),
    ("Countryside", "0xbddf61af533ff524d27154e589d2d7a81510c684"),
    ("majorexploiter", "0x019782cab5d844f02bafb71f512758be78579f3c"),
    ("0x2a2C_anon", "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1"),
    ("gatorr", "0x93abbc022ce98d6f45d4444b594791cc4b7a9723"),
    ("432614799197", "0xdc876e6873772d38716fda7f2452a78d426d7ab6"),
    ("CERTuo", "0xf195721ad850377c96cd634457c70cd9e8308057"),
    ("sovereign2013", "0xee613b3fc183ee44f9da9c05f53e2da107e3debf"),
    ("weflyhigh", "0x03e8a544e97eeff5753bc1e90d46e5ef22af1697"),
    ("RN1", "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"),
    ("Blessed-Sunshine", "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09"),
    ("Anointed-Connect", "0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6"),
    ("bcda", "0xb45a797faa52b0fd8adc56d30382022b7b12192c"),
    ("swisstony", "0x204f72f35326db932158cba6adff0b9a1da95e14"),
    ("GamblingIsAllYouNeed", "0x507e52ef684ca2dd91f90a9d26d149dd3288beae"),
    ("ewelmealt", "0x07921379f7b31ef93da634b688b2fe36897db778"),
    ("JPMorgan101", "0xb6d6e99d3bfe055874a04279f659f009fd57be17"),
]

# Sport classification
NHL_TEAMS = ["Bruins", "Penguins", "Oilers", "Ducks", "Wild", "Jets", "Panthers", "Devils",
             "Hurricanes", "Lightning", "Senators", "Blues", "Avalanche", "Stars", "Blackhawks",
             "Red Wings", "Sabres", "Flames", "Canucks", "Predators", "Capitals", "Islanders",
             "Flyers", "Kraken", "Sharks", "Coyotes", "Blue Jackets", "Canadiens", "Maple Leafs",
             "Rangers", "Kings", "Golden Knights", "Utah Hockey"]

NBA_TEAMS = ["76ers", "Lakers", "Celtics", "Knicks", "Bulls", "Heat", "Hawks", "Kings",
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

FOOTBALL_KEYWORDS = ["FC", "CF", "United", "City", "Arsenal", "Chelsea", "Liverpool", "Barcelona",
                     "Madrid", "Bayern", "Juventus", "Inter Milan", "AC Milan", "PSG", "Dortmund",
                     "Atletico", "Tottenham", "Manchester", "Leicester", "Everton", "Wolves",
                     "Crystal Palace", "Aston Villa", "Fulham", "Brentford", "Brighton",
                     "Bournemouth", "Nottingham", "Sheffield", "Burnley", "Luton",
                     "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
                     "Champions League", "Europa League", "MLS", "Copa", "World Cup",
                     "Eredivisie", "draw", "DRAW", "Feyenoord", "Ajax", "PSV",
                     "Celtic", "Rangers", "Porto", "Benfica", "Sporting",
                     "Napoli", "Roma", "Lazio", "Atalanta", "Fiorentina"]

TENNIS_KEYWORDS = ["ATP", "WTA", "tennis", "Grand Slam", "Wimbledon", "French Open",
                   "Australian Open", "US Open", "Djokovic", "Alcaraz", "Sinner",
                   "Medvedev", "Rublev", "Tsitsipas", "Zverev", "Ruud",
                   "Swiatek", "Sabalenka", "Gauff", "Rybakina"]


def classify_sport(title):
    """Classify a market title into a sport category."""
    if not title:
        return "Other"

    # NFL check first (some team names overlap)
    # Context: "Jets" could be NHL or NFL
    for team in NFL_TEAMS:
        if team in title:
            # Disambiguate Jets
            if team == "Jets" and any(nhl in title for nhl in ["Winnipeg", "NHL"]):
                continue
            if team == "Cardinals" and any(mlb in title for mlb in ["St. Louis", "MLB"]):
                continue
            if team == "Kings" and any(nba in title for nba in ["Sacramento", "NBA"]):
                continue
            if team in ["Bills", "Dolphins", "Patriots", "Steelers", "Ravens", "Bengals",
                        "Browns", "Titans", "Colts", "Jaguars", "Texans", "Chiefs",
                        "Chargers", "Raiders", "Broncos", "Cowboys", "Giants", "Eagles",
                        "Commanders", "Packers", "Vikings", "Bears", "Lions", "Falcons",
                        "Saints", "Buccaneers", "Rams", "49ers", "Seahawks", "NFL", "Super Bowl"]:
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

    # Check for generic sport patterns
    if re.search(r'over|under|spread|moneyline|total|o/u', title, re.IGNORECASE):
        # Could be any sport, classify as Other-Sport
        return "Other-Sport"

    return "Other"


def fetch_positions(address, max_pages=10):
    """Fetch open positions for a wallet."""
    all_positions = []
    offset = 0
    limit = 500
    for _ in range(max_pages):
        url = f"https://data-api.polymarket.com/positions?user={address}&limit={limit}&offset={offset}&sizeThreshold=0"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                print(f"  Rate limited, waiting 5s...")
                time.sleep(5)
                resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                print(f"  Positions HTTP {resp.status_code} for {address[:10]}... offset={offset}")
                break
            data = resp.json()
            if not data:
                break
            all_positions.extend(data)
            if len(data) < limit:
                break
            offset += limit
            time.sleep(0.3)
        except Exception as e:
            print(f"  Error fetching positions: {e}")
            break
    return all_positions


def fetch_closed_positions(address, max_pages=6):
    """Fetch closed/resolved positions for a wallet."""
    all_positions = []
    offset = 0
    limit = 500
    for _ in range(max_pages):
        url = f"https://data-api.polymarket.com/closed-positions?user={address}&limit={limit}&sortBy=TIMESTAMP&sortOrder=DESC&offset={offset}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                print(f"  Rate limited on closed, waiting 5s...")
                time.sleep(5)
                resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                print(f"  Closed HTTP {resp.status_code} for {address[:10]}... offset={offset}")
                break
            data = resp.json()
            if not data:
                break
            all_positions.extend(data)
            if len(data) < limit:
                break
            offset += limit
            time.sleep(0.3)
        except Exception as e:
            print(f"  Error fetching closed: {e}")
            break
    return all_positions


def analyse_wallet(username, address):
    """Analyse a single wallet's sport-specific performance."""
    print(f"\n{'='*60}")
    print(f"Analysing {username} ({address[:10]}...)")

    open_pos = fetch_positions(address)
    time.sleep(0.5)
    closed_pos = fetch_closed_positions(address)

    print(f"  Open positions: {len(open_pos)}, Closed positions: {len(closed_pos)}")

    # Classify each position by sport
    sport_data = defaultdict(lambda: {
        "positions": 0,
        "wins": 0,
        "losses": 0,
        "pending": 0,
        "total_invested": 0.0,
        "total_pnl": 0.0,
        "events": defaultdict(list),  # eventSlug -> list of position sizes
        "both_sides_events": 0,
    })

    total_positions = 0

    # Merge closed + open positions by conditionId to avoid win bias.
    # closed-positions API overrepresents winners (survivorship bias).
    # Use conditionId as key: closed data wins for resolved, open data for pending.
    seen_conditions = set()

    # Process closed positions ONLY for ROI/WR (these are truly resolved)
    for pos in closed_pos:
        title = pos.get("title", "") or pos.get("marketTitle", "") or pos.get("groupTitle", "") or ""
        sport = classify_sport(title)
        cid = pos.get("conditionId", "")

        realized_pnl = float(pos.get("realizedPnl", 0) or 0)
        initial_value = float(pos.get("totalBought", 0) or pos.get("initialValue", 0) or pos.get("cashPaid", 0) or 0)

        sd = sport_data[sport]
        sd["positions"] += 1
        sd["total_invested"] += abs(initial_value)
        sd["total_pnl"] += realized_pnl
        total_positions += 1

        if realized_pnl > 0:
            sd["wins"] += 1
        else:
            sd["losses"] += 1

        if cid:
            seen_conditions.add(cid)

        event = pos.get("eventSlug", "") or pos.get("groupSlug", "") or title[:30]
        outcome = pos.get("outcome", "") or pos.get("title", "")
        sd["events"][event].append({"size": abs(initial_value), "outcome": outcome})

    # Process open positions — count as pending only (NO unrealized P&L in ROI)
    for pos in open_pos:
        title = pos.get("title", "") or pos.get("marketTitle", "") or pos.get("groupTitle", "") or ""
        sport = classify_sport(title)
        cid = pos.get("conditionId", "")

        # Skip if already counted in closed positions
        if cid and cid in seen_conditions:
            continue

        size = float(pos.get("size", 0) or 0)
        avg_price = float(pos.get("avgPrice", 0.5) or 0.5)
        initial_value = size * avg_price

        sd = sport_data[sport]
        sd["positions"] += 1
        sd["pending"] += 1
        total_positions += 1

        event = pos.get("eventSlug", "") or pos.get("groupSlug", "") or title[:30]
        outcome = pos.get("outcome", "") or pos.get("title", "")
        sd["events"][event].append({"size": abs(initial_value), "outcome": outcome})

    # Calculate metrics per sport
    results = []
    for sport, sd in sport_data.items():
        if sport in ["Other", "Other-Sport"]:
            continue
        if sd["positions"] < 5:
            continue

        resolved = sd["wins"] + sd["losses"]
        wr = sd["wins"] / resolved if resolved > 0 else 0
        roi = sd["total_pnl"] / sd["total_invested"] if sd["total_invested"] > 0 else 0
        concentration = sd["positions"] / total_positions if total_positions > 0 else 0

        # Check directionality (market maker detection)
        both_sides = 0
        total_events = len(sd["events"])
        for event, legs in sd["events"].items():
            outcomes = set()
            for leg in legs:
                outcomes.add(leg.get("outcome", ""))
            if len(outcomes) > 1:
                both_sides += 1
        directional_pct = both_sides / total_events if total_events > 0 else 0
        is_mm = directional_pct > 0.3

        # Hauptbet analysis: for multi-leg events, find the largest leg
        hauptbet_wins = 0
        hauptbet_total = 0
        for event, legs in sd["events"].items():
            if len(legs) >= 2:
                largest = max(legs, key=lambda l: l["size"])
                # We can't easily determine if hauptbet won from this data alone
                # but we track it
                hauptbet_total += 1

        results.append({
            "username": username,
            "address": address,
            "sport": sport,
            "positions": sd["positions"],
            "resolved": resolved,
            "wins": sd["wins"],
            "losses": sd["losses"],
            "pending": sd["pending"],
            "wr": wr,
            "roi": roi,
            "pnl": sd["total_pnl"],
            "invested": sd["total_invested"],
            "concentration": concentration,
            "directional_pct": directional_pct,
            "is_mm": is_mm,
            "multi_leg_events": hauptbet_total,
        })

    return results


def main():
    all_results = []

    for username, address in WALLETS:
        try:
            results = analyse_wallet(username, address)
            all_results.extend(results)
        except Exception as e:
            print(f"  FAILED: {e}")
        time.sleep(1)

    # Sort by ROI descending
    all_results.sort(key=lambda r: r["roi"], reverse=True)

    # Print results table
    print("\n\n" + "="*140)
    print("SPORT SPECIALIST ANALYSIS — POLYMARKET TOP SPORTS TRADERS")
    print("="*140)
    print(f"{'Username':<25} {'Sport':<12} {'Pos':>5} {'Resolved':>8} {'WR':>6} {'ROI':>8} {'PnL':>12} {'Invested':>12} {'Conc%':>6} {'Dir%':>5} {'MM?':>4}")
    print("-"*140)

    for r in all_results:
        if r["resolved"] < 5:
            continue
        mm_flag = "YES" if r["is_mm"] else ""
        print(f"{r['username']:<25} {r['sport']:<12} {r['positions']:>5} {r['resolved']:>8} "
              f"{r['wr']:>5.1%} {r['roi']:>7.1%} {r['pnl']:>11.0f} {r['invested']:>11.0f} "
              f"{r['concentration']:>5.0%} {r['directional_pct']:>5.0%} {mm_flag:>4}")

    # Top specialists (ROI > 0, resolved >= 30, not market maker)
    print("\n\n" + "="*100)
    print("TOP SPORT SPECIALISTS (ROI > 0%, 30+ resolved, not market maker)")
    print("="*100)

    specialists = [r for r in all_results if r["roi"] > 0 and r["resolved"] >= 30 and not r["is_mm"]]
    specialists.sort(key=lambda r: r["roi"], reverse=True)

    for i, r in enumerate(specialists[:20], 1):
        print(f"\n#{i}: {r['username']} — {r['sport']}")
        print(f"   Address: {r['address']}")
        print(f"   Positions: {r['positions']}, Resolved: {r['resolved']}, WR: {r['wr']:.1%}, ROI: {r['roi']:.1%}")
        print(f"   PnL: ${r['pnl']:,.0f}, Invested: ${r['invested']:,.0f}")
        print(f"   Concentration: {r['concentration']:.0%}, Multi-leg events: {r['multi_leg_events']}")

    # Save raw results (relative to script dir)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    results_path = os.path.join(script_dir, "sport_specialists_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)

    # Generate readable report for /wallet-scout skill
    report_path = os.path.join(script_dir, "wallet_scout_report.md")
    with open(report_path, "w") as f:
        f.write(f"# Wallet Scout Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("ROI = realized only (closed positions). No unrealized/open P&L.\n\n")
        f.write(f"| Sport | Wallet | ROI | WR | Resolved | Pending | PnL | Invested |\n")
        f.write(f"|-------|--------|-----|-----|----------|---------|-----|----------|\n")
        sorted_results = sorted(all_results, key=lambda r: -r["roi"])
        for r in sorted_results:
            if r["resolved"] < 5:
                continue
            mm = " (MM)" if r.get("is_mm") else ""
            f.write(f"| {r['sport']} | {r['username']}{mm} | {r['roi']:+.1%} | {r['wr']:.0%} | {r['resolved']} | {r.get('pending',0)} | ${r['pnl']:,.0f} | ${r['invested']:,.0f} |\n")
        f.write(f"\nTotal: {len(all_results)} wallet×sport combinations\n")

    print(f"\n\nTotal wallet×sport combinations analysed: {len(all_results)}")
    print(f"Results saved to {results_path}")
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    import os
    from datetime import datetime
    main()
