#!/usr/bin/env python3
"""
Mispricing Scan: Polymarket vs Bookmakers
Vergelijkt PM prijzen met bookmaker odds voor voetbalwedstrijden.
Focus op Cannae-strategie legs: Win NO en Draw YES.
"""

import requests
import json
import re
import sys
from datetime import datetime, timezone

ODDS_API_KEY = "7dc78ea013dc0b9e5a5a7f660e2c8824"
ODDS_BASE = "https://api.the-odds-api.com/v4/sports"
PM_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Mapping: Odds API sport key -> PM tag_slug
LEAGUE_MAP = {
    "soccer_epl": "premier-league",
    "soccer_germany_bundesliga": "bundesliga",
    "soccer_uefa_champions_league": "champions-league",
    "soccer_spain_la_liga": "la-liga",
    "soccer_italy_serie_a": "serie-a",
    "soccer_france_ligue_one": "ligue-1",
}

# Team name normalization
TEAM_ALIASES = {
    "manchester united": ["man utd", "man united", "manchester utd"],
    "manchester city": ["man city", "manchester city fc"],
    "tottenham hotspur": ["tottenham", "spurs", "tottenham hotspur fc"],
    "wolverhampton wanderers": ["wolves", "wolverhampton", "wolverhampton wanderers fc"],
    "newcastle united": ["newcastle", "newcastle united fc"],
    "nottingham forest": ["nottm forest", "nottingham", "nottingham forest fc"],
    "west ham united": ["west ham", "west ham united fc"],
    "brighton and hove albion": ["brighton", "brighton and hove albion fc"],
    "leicester city": ["leicester", "leicester city fc"],
    "crystal palace": ["crystal palace", "crystal palace fc"],
    "afc bournemouth": ["bournemouth", "afc bournemouth fc"],
    "ipswich town": ["ipswich", "ipswich town fc"],
    "arsenal": ["arsenal fc"],
    "liverpool": ["liverpool fc"],
    "chelsea": ["chelsea fc"],
    "everton": ["everton fc"],
    "fulham": ["fulham fc"],
    "aston villa": ["aston villa fc"],
    "brentford": ["brentford fc"],
    "southampton": ["southampton fc"],
    "borussia dortmund": ["dortmund", "bvb", "borussia dortmund fc"],
    "bayern munich": ["bayern", "fc bayern", "bayern munchen", "fc bayern munchen", "fc bayern munich"],
    "bayer leverkusen": ["leverkusen", "bayer 04", "bayer 04 leverkusen"],
    "rb leipzig": ["leipzig", "rasenballsport leipzig"],
    "eintracht frankfurt": ["frankfurt", "e. frankfurt", "eintracht frankfurt fc"],
    "borussia monchengladbach": ["gladbach", "monchengladbach", "borussia mgladbach"],
    "vfb stuttgart": ["stuttgart", "vfb stuttgart fc"],
    "sc freiburg": ["freiburg", "sc freiburg fc"],
    "vfl wolfsburg": ["wolfsburg", "vfl wolfsburg fc"],
    "1. fc union berlin": ["union berlin"],
    "fc augsburg": ["augsburg"],
    "1. fc heidenheim 1846": ["heidenheim"],
    "tsg hoffenheim": ["hoffenheim", "tsg 1899 hoffenheim"],
    "fc st. pauli": ["st. pauli", "st pauli", "fc st pauli"],
    "holstein kiel": ["kiel"],
    "sv werder bremen": ["werder bremen", "bremen"],
    "vfl bochum": ["bochum", "vfl bochum 1848"],
    "1. fsv mainz 05": ["mainz", "mainz 05"],
    "atletico madrid": ["atletico", "atl. madrid", "atl madrid", "atletico de madrid", "club atletico de madrid"],
    "real madrid": ["real madrid cf"],
    "fc barcelona": ["barcelona", "barca"],
    "real sociedad": ["real sociedad fc"],
    "athletic bilbao": ["ath bilbao", "athletic club", "athletic club bilbao"],
    "real betis": ["betis", "real betis balompie"],
    "villarreal cf": ["villarreal"],
    "rcd mallorca": ["mallorca"],
    "celta vigo": ["celta", "rc celta de vigo"],
    "rayo vallecano": ["rayo"],
    "deportivo alaves": ["alaves"],
    "cd leganes": ["leganes"],
    "rcd espanyol": ["espanyol"],
    "real valladolid": ["valladolid"],
    "ud las palmas": ["las palmas"],
    "getafe cf": ["getafe"],
    "ca osasuna": ["osasuna"],
    "girona fc": ["girona"],
    "sevilla fc": ["sevilla"],
    "valencia cf": ["valencia"],
    "ac milan": ["milan", "ac milan fc"],
    "inter milan": ["inter", "internazionale", "fc internazionale milano"],
    "juventus": ["juve", "juventus fc"],
    "as roma": ["roma", "as roma fc"],
    "ss lazio": ["lazio", "ss lazio fc"],
    "atalanta bc": ["atalanta", "atalanta bergamo"],
    "ssc napoli": ["napoli"],
    "acf fiorentina": ["fiorentina"],
    "torino fc": ["torino"],
    "us lecce": ["lecce"],
    "cagliari calcio": ["cagliari"],
    "hellas verona": ["verona", "hellas verona fc"],
    "udinese calcio": ["udinese"],
    "genoa cfc": ["genoa"],
    "empoli fc": ["empoli"],
    "parma calcio": ["parma", "parma calcio 1913"],
    "como 1907": ["como"],
    "venezia fc": ["venezia"],
    "ac monza": ["monza"],
    "paris saint-germain": ["paris saint germain", "psg", "paris sg", "paris saint-germain fc"],
    "olympique de marseille": ["marseille", "om"],
    "olympique lyonnais": ["lyon", "ol", "olympique lyon"],
    "as monaco": ["monaco", "as monaco fc"],
    "losc lille": ["lille"],
    "ogc nice": ["nice"],
    "rc lens": ["lens"],
    "stade rennais": ["rennes"],
    "rc strasbourg alsace": ["strasbourg"],
    "stade de reims": ["reims"],
    "fc nantes": ["nantes"],
    "montpellier hsc": ["montpellier"],
    "toulouse fc": ["toulouse"],
    "angers sco": ["angers"],
    "le havre ac": ["le havre"],
    "as saint-etienne": ["saint-etienne", "st etienne"],
    "aj auxerre": ["auxerre"],
    "club brugge": ["brugge", "club bruges"],
    "psv eindhoven": ["psv"],
    "benfica": ["sl benfica"],
    "sporting cp": ["sporting lisbon", "sporting"],
}


def normalize(name):
    """Normalize team name: lowercase, strip FC/CF suffixes, remove dots."""
    n = name.lower().strip()
    # Remove common suffixes
    for suffix in [" fc", " cf", " sc", " bc", " ssc", " ac"]:
        if n.endswith(suffix):
            n = n[:-len(suffix)].strip()
    # Remove leading prefixes
    for prefix in ["fc ", "cf ", "sc ", "ac ", "as ", "ss "]:
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
    n = n.replace(".", "").replace("'", "")
    return n


def teams_match(name_a, name_b):
    """Check if two team names refer to the same team."""
    na = normalize(name_a)
    nb = normalize(name_b)

    # Direct
    if na == nb or na in nb or nb in na:
        return True

    # Check aliases
    for canonical, aliases in TEAM_ALIASES.items():
        all_names = [canonical] + aliases
        all_norm = [normalize(n) for n in all_names]
        a_match = any(na == a or na in a or a in na for a in all_norm)
        b_match = any(nb == a or nb in a or a in nb for a in all_norm)
        if a_match and b_match:
            return True

    return False


def fetch_bookmaker_odds():
    """Fetch odds from The Odds API for all leagues."""
    all_games = {}
    for sport_key, pm_tag in LEAGUE_MAP.items():
        url = "%s/%s/odds/" % (ODDS_BASE, sport_key)
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal",
        }
        resp = requests.get(url, params=params, timeout=15)
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")

        if resp.status_code != 200:
            print("[WARN] Odds API %s: HTTP %d" % (sport_key, resp.status_code))
            continue

        data = resp.json()
        print("[INFO] %s: %d games (credits used=%s, remaining=%s)" % (sport_key, len(data), used, remaining))

        for game in data:
            home = game["home_team"]
            away = game["away_team"]
            commence = game.get("commence_time", "")

            # Consensus odds (avg across bookmakers, vig removed)
            outcome_odds = {}
            for bm in game.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market["key"] != "h2h":
                        continue
                    for outcome in market["outcomes"]:
                        name = outcome["name"]
                        price = outcome["price"]
                        if name not in outcome_odds:
                            outcome_odds[name] = []
                        outcome_odds[name].append(price)

            if not outcome_odds:
                continue

            # Average + remove vig
            raw_probs = {}
            for name, prices in outcome_odds.items():
                avg_odd = sum(prices) / len(prices)
                raw_probs[name] = 1.0 / avg_odd

            total = sum(raw_probs.values())
            fair_probs = {}
            for name, prob in raw_probs.items():
                fair_probs[name] = prob / total

            # Map to home/away/draw
            bm_home = bm_away = bm_draw = None
            for name, prob in fair_probs.items():
                if name.lower() == "draw":
                    bm_draw = prob
                elif teams_match(home, name):
                    bm_home = prob
                elif teams_match(away, name):
                    bm_away = prob

            key = "%s|%s|%s" % (pm_tag, home, away)
            all_games[key] = {
                "league": sport_key,
                "pm_tag": pm_tag,
                "home": home,
                "away": away,
                "commence": commence,
                "bm_home": bm_home,
                "bm_away": bm_away,
                "bm_draw": bm_draw,
                "num_bookmakers": len(game.get("bookmakers", [])),
            }

    return all_games


def fetch_pm_matches():
    """Fetch match-level markets from PM per league."""
    all_matches = {}

    for pm_tag in LEAGUE_MAP.values():
        offset = 0
        events = []
        while True:
            r = requests.get(PM_EVENTS_URL, params={
                "tag_slug": pm_tag,
                "limit": 100,
                "active": "true",
                "closed": "false",
                "offset": offset,
            }, timeout=15)
            data = r.json()
            if not data:
                break
            events.extend(data)
            if len(data) < 100:
                break
            offset += 100

        # Extract match events (contain "vs" in title)
        match_count = 0
        for event in events:
            title = event.get("title", "")
            if "vs" not in title.lower() and "v." not in title.lower():
                continue

            # Skip "More Markets", "Halftime", "Exact Score" etc - we want base markets
            if any(x in title.lower() for x in ["more market", "halftime", "exact score",
                                                  "correct score", "both teams", "total goals",
                                                  "goal scorer", "first goal", "last goal",
                                                  "card", "corner"]):
                continue

            match_data = {
                "title": title,
                "pm_tag": pm_tag,
                "home_win": None,
                "away_win": None,
                "draw": None,
                "home_name": None,
                "away_name": None,
            }

            for market in event.get("markets", []):
                q = market.get("question", "")
                outcomes = market.get("outcomes", "")
                prices = market.get("outcomePrices", "")

                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except (json.JSONDecodeError, TypeError):
                        continue
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except (json.JSONDecodeError, TypeError):
                        continue

                if not prices or not outcomes:
                    continue

                # Build price map
                price_map = {}
                for i, name in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            price_map[name] = float(prices[i])
                        except (ValueError, TypeError):
                            pass

                yes_price = price_map.get("Yes")

                # "Will X win?" pattern
                if "win" in q.lower() and yes_price is not None:
                    # Extract team name from question
                    # Pattern: "Will <team> win on <date>?"
                    win_match = re.search(r"Will (.+?) win", q, re.IGNORECASE)
                    if win_match:
                        team_name = win_match.group(1).strip()
                        if match_data["home_win"] is None:
                            match_data["home_win"] = yes_price
                            match_data["home_name"] = team_name
                        else:
                            match_data["away_win"] = yes_price
                            match_data["away_name"] = team_name

                # "draw" pattern
                if "draw" in q.lower() and yes_price is not None:
                    match_data["draw"] = yes_price

            if match_data["home_win"] is not None or match_data["draw"] is not None:
                key = "%s|%s" % (pm_tag, title)
                all_matches[key] = match_data
                match_count += 1

        print("[INFO] PM %s: %d match events with prices" % (pm_tag, match_count))

    return all_matches


def match_games(bm_games, pm_matches):
    """Match bookmaker games to PM markets."""
    matched = []
    unmatched_bm = []

    for bm_key, bm in bm_games.items():
        best_pm = None
        best_score = 0

        for pm_key, pm in pm_matches.items():
            if bm["pm_tag"] != pm["pm_tag"]:
                continue

            # Try matching teams
            home_match = False
            away_match = False

            if pm["home_name"] and teams_match(bm["home"], pm["home_name"]):
                home_match = True
            if pm["away_name"] and teams_match(bm["away"], pm["away_name"]):
                away_match = True

            # Also try reversed (PM might list teams differently)
            if not home_match and pm["away_name"] and teams_match(bm["home"], pm["away_name"]):
                home_match = True
            if not away_match and pm["home_name"] and teams_match(bm["away"], pm["home_name"]):
                away_match = True

            # Also check title
            if not home_match:
                if teams_match(bm["home"], pm["title"]):
                    home_match = True
            if not away_match:
                if teams_match(bm["away"], pm["title"]):
                    away_match = True

            score = int(home_match) + int(away_match)
            if score > best_score:
                best_score = score
                best_pm = pm

        if best_pm and best_score >= 2:
            # Determine correct mapping: which PM team = BM home?
            pm_home_win = None
            pm_away_win = None

            if best_pm["home_name"] and teams_match(bm["home"], best_pm["home_name"]):
                pm_home_win = best_pm["home_win"]
                pm_away_win = best_pm["away_win"]
            elif best_pm["away_name"] and teams_match(bm["home"], best_pm["away_name"]):
                pm_home_win = best_pm["away_win"]
                pm_away_win = best_pm["home_win"]
            else:
                pm_home_win = best_pm["home_win"]
                pm_away_win = best_pm["away_win"]

            matched.append({
                "league": bm["league"],
                "home": bm["home"],
                "away": bm["away"],
                "commence": bm["commence"],
                "bm_home": bm["bm_home"],
                "bm_away": bm["bm_away"],
                "bm_draw": bm["bm_draw"],
                "pm_home_win": pm_home_win,
                "pm_away_win": pm_away_win,
                "pm_draw": best_pm["draw"],
                "pm_title": best_pm["title"],
                "num_bm": bm["num_bookmakers"],
            })
        else:
            unmatched_bm.append("%s: %s vs %s" % (bm["league"], bm["home"], bm["away"]))

    return matched, unmatched_bm


def main():
    print("=" * 100)
    print("MISPRICING SCAN: Polymarket vs Bookmakers (Voetbal)")
    print("Datum: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 100)

    # Step 1: Fetch bookmaker odds
    print("\n[STAP 1] Bookmaker odds ophalen...")
    bm_games = fetch_bookmaker_odds()
    print("Totaal bookmaker wedstrijden: %d" % len(bm_games))

    # Step 2: Fetch PM match markets
    print("\n[STAP 2] Polymarket match markets ophalen...")
    pm_matches = fetch_pm_matches()
    print("Totaal PM match events: %d" % len(pm_matches))

    # Step 3: Match
    print("\n[STAP 3] Matching...")
    matched, unmatched = match_games(bm_games, pm_matches)
    print("Gematcht: %d | Niet gematcht: %d" % (len(matched), len(unmatched)))

    # Step 4: Output
    print("\n")
    print("=" * 100)
    print("RESULTATEN")
    print("=" * 100)

    if not matched:
        print("\nGeen wedstrijden gematcht. Check de team name matching.")
        if unmatched:
            print("\nNiet-gematcht (BM):")
            for u in unmatched:
                print("  %s" % u)
        return

    # Sort by league then commence time
    matched.sort(key=lambda x: (x["league"], x["commence"]))

    # Calculate diffs
    home_win_yes_diffs = []
    away_win_yes_diffs = []
    home_win_no_diffs = []
    away_win_no_diffs = []
    draw_yes_diffs = []

    print("\n%-40s | %5s %5s %5s | %5s %5s %5s | %6s %6s %6s" % (
        "Wedstrijd", "BM_H", "BM_A", "BM_D", "PM_H", "PM_A", "PM_D",
        "dH", "dA", "dDraw"
    ))
    print("-" * 100)

    current_league = None
    for r in matched:
        if r["league"] != current_league:
            current_league = r["league"]
            print("\n  --- %s ---" % current_league.upper())

        match_str = "%s v %s" % (r["home"][:18], r["away"][:18])

        bm_h = ("%.0f" % (r["bm_home"] * 100)) if r["bm_home"] else " - "
        bm_a = ("%.0f" % (r["bm_away"] * 100)) if r["bm_away"] else " - "
        bm_d = ("%.0f" % (r["bm_draw"] * 100)) if r["bm_draw"] else " - "
        pm_h = ("%.0f" % (r["pm_home_win"] * 100)) if r["pm_home_win"] else " - "
        pm_a = ("%.0f" % (r["pm_away_win"] * 100)) if r["pm_away_win"] else " - "
        pm_d = ("%.0f" % (r["pm_draw"] * 100)) if r["pm_draw"] else " - "

        dh = da = dd = ""
        if r["bm_home"] and r["pm_home_win"]:
            diff = r["pm_home_win"] - r["bm_home"]
            dh = "%+.1f" % (diff * 100)
            home_win_yes_diffs.append(diff)
            home_win_no_diffs.append(-diff)  # Win NO diff = -(Win YES diff)

        if r["bm_away"] and r["pm_away_win"]:
            diff = r["pm_away_win"] - r["bm_away"]
            da = "%+.1f" % (diff * 100)
            away_win_yes_diffs.append(diff)
            away_win_no_diffs.append(-diff)

        if r["bm_draw"] and r["pm_draw"]:
            diff = r["pm_draw"] - r["bm_draw"]
            dd = "%+.1f" % (diff * 100)
            draw_yes_diffs.append(diff)

        print("%-40s | %5s %5s %5s | %5s %5s %5s | %6s %6s %6s" % (
            match_str, bm_h, bm_a, bm_d, pm_h, pm_a, pm_d, dh, da, dd
        ))

    # Summary
    print("\n")
    print("=" * 100)
    print("SAMENVATTING MISPRICING (PM prijs - BM fair prob)")
    print("  Positief = PM duurder dan bookmakers")
    print("  Negatief = PM goedkoper dan bookmakers")
    print("=" * 100)

    def show_stats(label, diffs):
        if not diffs:
            print("%-25s: geen data" % label)
            return
        avg = sum(diffs) / len(diffs)
        med = sorted(diffs)[len(diffs) // 2]
        mn = min(diffs)
        mx = max(diffs)
        print("%-25s: gem=%+5.1f%% | med=%+5.1f%% | min=%+5.1f%% | max=%+5.1f%% | N=%d" % (
            label, avg * 100, med * 100, mn * 100, mx * 100, len(diffs)
        ))

    show_stats("Home Win YES", home_win_yes_diffs)
    show_stats("Away Win YES", away_win_yes_diffs)
    show_stats("Home Win NO", home_win_no_diffs)
    show_stats("Away Win NO", away_win_no_diffs)
    show_stats("Draw YES", draw_yes_diffs)

    # Cannae strategy focus
    print("\n")
    print("=" * 100)
    print("CANNAE STRATEGIE ANALYSE")
    print("  Cannae koopt: Win NO (team verliest of gelijk) en Draw YES")
    print("  Value = PM prijs LAGER dan bookmaker fair prob (negatief verschil)")
    print("=" * 100)

    all_win_no = home_win_no_diffs + away_win_no_diffs
    if all_win_no:
        avg_wn = sum(all_win_no) / len(all_win_no)
        print("\nWin NO (alle teams gecombineerd):")
        print("  Gemiddeld verschil:     %+.2f%% (N=%d)" % (avg_wn * 100, len(all_win_no)))
        pm_cheaper = len([d for d in all_win_no if d < 0])
        pm_cheaper_2 = len([d for d in all_win_no if d < -0.02])
        pm_cheaper_5 = len([d for d in all_win_no if d < -0.05])
        print("  PM goedkoper (any):     %d/%d (%.0f%%)" % (pm_cheaper, len(all_win_no), 100.0 * pm_cheaper / len(all_win_no)))
        print("  PM goedkoper >2pp:      %d/%d (%.0f%%)" % (pm_cheaper_2, len(all_win_no), 100.0 * pm_cheaper_2 / len(all_win_no)))
        print("  PM goedkoper >5pp:      %d/%d (%.0f%%)" % (pm_cheaper_5, len(all_win_no), 100.0 * pm_cheaper_5 / len(all_win_no)))

    if draw_yes_diffs:
        avg_d = sum(draw_yes_diffs) / len(draw_yes_diffs)
        print("\nDraw YES:")
        print("  Gemiddeld verschil:     %+.2f%% (N=%d)" % (avg_d * 100, len(draw_yes_diffs)))
        pm_cheaper = len([d for d in draw_yes_diffs if d < 0])
        pm_cheaper_2 = len([d for d in draw_yes_diffs if d < -0.02])
        pm_cheaper_5 = len([d for d in draw_yes_diffs if d < -0.05])
        print("  PM goedkoper (any):     %d/%d (%.0f%%)" % (pm_cheaper, len(draw_yes_diffs), 100.0 * pm_cheaper / len(draw_yes_diffs)))
        print("  PM goedkoper >2pp:      %d/%d (%.0f%%)" % (pm_cheaper_2, len(draw_yes_diffs), 100.0 * pm_cheaper_2 / len(draw_yes_diffs)))
        print("  PM goedkoper >5pp:      %d/%d (%.0f%%)" % (pm_cheaper_5, len(draw_yes_diffs), 100.0 * pm_cheaper_5 / len(draw_yes_diffs)))

    # Overall conclusion
    print("\n")
    print("=" * 100)
    print("CONCLUSIE")
    print("=" * 100)

    all_abs_diffs = [abs(d) for d in (home_win_yes_diffs + away_win_yes_diffs + draw_yes_diffs)]
    if all_abs_diffs:
        overall_avg = sum(all_abs_diffs) / len(all_abs_diffs)
        print("Gemiddelde absolute afwijking (alle markten): %.1f procentpunt" % (overall_avg * 100))

        if overall_avg > 0.05:
            print("=> SIGNIFICANTE mispricing (>5pp) tussen PM en bookmakers.")
        elif overall_avg > 0.02:
            print("=> MATIGE mispricing (2-5pp). Er is enige inefficientie.")
        else:
            print("=> MINIMALE mispricing (<2pp). PM is efficient geprijsd.")

        # Directional bias
        all_signed = home_win_yes_diffs + away_win_yes_diffs
        if all_signed:
            avg_signed = sum(all_signed) / len(all_signed)
            if avg_signed > 0.01:
                print("=> BIAS: PM prijst Win YES systematisch HOGER (= Win NO goedkoper op PM).")
                print("   Dit is GUNSTIG voor de Cannae strategie (koopt Win NO).")
            elif avg_signed < -0.01:
                print("=> BIAS: PM prijst Win YES systematisch LAGER (= Win NO duurder op PM).")
                print("   Dit is ONGUNSTIG voor de Cannae strategie.")
            else:
                print("=> Geen systematische bias in Win YES pricing.")

        if draw_yes_diffs:
            avg_draw_signed = sum(draw_yes_diffs) / len(draw_yes_diffs)
            if avg_draw_signed < -0.01:
                print("=> PM Draw YES is systematisch GOEDKOPER dan bookmakers.")
                print("   Dit is GUNSTIG voor de Cannae strategie (koopt Draw YES).")
            elif avg_draw_signed > 0.01:
                print("=> PM Draw YES is systematisch DUURDER dan bookmakers.")
                print("   Dit is ONGUNSTIG voor de Cannae strategie.")
            else:
                print("=> Geen systematische bias in Draw YES pricing.")

    # Unmatched
    if unmatched:
        print("\n--- Niet-gematcht (%d wedstrijden) ---" % len(unmatched))
        for u in sorted(unmatched):
            print("  %s" % u)


if __name__ == "__main__":
    main()
