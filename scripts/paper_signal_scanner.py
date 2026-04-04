#!/usr/bin/env python3
"""
Paper Signal Scanner — Bottie-Test
Vergelijkt Polymarket prijzen met bookmaker odds.
Schrijft dry_run TradeLog entries naar /opt/bottie-test/data/trades.jsonl.

Signaalcriteria:
  - Edge > MIN_EDGE_PCT (PM prijs < bookmaker fair prob)
  - PM prijs in [MIN_PRICE, MAX_PRICE]
  - Minimaal MIN_BOOKMAKERS bookmakers
  - Voetbal: Win NO op favoriet, Draw YES
  - NBA: Moneyline edge (h2h)

Draait via cron elke 4 uur op de VPS.
"""

import json
import re
import sys
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "0cb690f97add451f3282da4e481f0730")
ODDS_BASE      = "https://api.the-odds-api.com/v4/sports"
PM_EVENTS_URL  = "https://gamma-api.polymarket.com/events"
TRADES_FILE    = "/opt/bottie-test/data/trades.jsonl"
SCHEDULE_FILE  = "/opt/bottie/data/schedule_cache.json"

MIN_EDGE_PCT   = 3.0   # minimale edge in procentpunten
MIN_PRICE      = 0.20  # ondergrens PM prijs
MAX_PRICE      = 0.50  # bovengrens PM prijs (goedkope kant)
MIN_BOOKMAKERS = 4     # minimaal aantal bookmakers voor consensus
FLAT_SIZE_USDC = 5.0   # papier trade grootte

SOCCER_LEAGUES = {
    # Top 6 (origineel)
    "soccer_epl":                    "premier-league",
    "soccer_germany_bundesliga":     "bundesliga",
    "soccer_uefa_champs_league":     "champions-league",
    "soccer_spain_la_liga":          "la-liga",
    "soccer_italy_serie_a":          "serie-a",
    "soccer_france_ligue_one":       "ligue-1",
    # Cannae-actieve leagues
    "soccer_efl_champ":              "efl-champ",
    "soccer_england_league1":        "england-league1",
    "soccer_netherlands_eredivisie": "eredivisie",
    "soccer_portugal_primeira_liga": "primeira-liga",
    "soccer_argentina_primera_division": "argentina-primera",
    "soccer_brazil_campeonato":      "brazil-serie-a",
    "soccer_turkey_super_league":    "turkey-super-league",
}

NBA_SPORT_KEY = "basketball_nba"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Team name normalization (gedeeld met mispricing_scan.py) ──────────────────
TEAM_ALIASES = {
    "manchester united": ["man utd", "man united", "manchester utd"],
    "manchester city": ["man city", "manchester city fc"],
    "tottenham hotspur": ["tottenham", "spurs"],
    "wolverhampton wanderers": ["wolves", "wolverhampton"],
    "newcastle united": ["newcastle"],
    "nottingham forest": ["nottm forest", "nottingham"],
    "west ham united": ["west ham"],
    "brighton and hove albion": ["brighton"],
    "afc bournemouth": ["bournemouth"],
    "borussia dortmund": ["dortmund", "bvb"],
    "bayern munich": ["bayern", "fc bayern", "bayern munchen", "fc bayern munchen", "fc bayern munich"],
    "bayer leverkusen": ["leverkusen", "bayer 04"],
    "rb leipzig": ["leipzig"],
    "eintracht frankfurt": ["frankfurt", "e. frankfurt"],
    "atletico madrid": ["atletico", "atl. madrid", "atl madrid", "atletico de madrid"],
    "real madrid": ["real madrid cf"],
    "fc barcelona": ["barcelona", "barca"],
    "ac milan": ["milan"],
    "inter milan": ["inter", "internazionale"],
    "paris saint-germain": ["psg", "paris sg", "paris saint germain"],
    "olympique de marseille": ["marseille", "om"],
    "olympique lyonnais": ["lyon", "ol"],
}


def normalize(name: str) -> str:
    n = name.lower().strip()
    for suffix in [" fc", " cf", " sc", " bc", " ssc", " ac"]:
        if n.endswith(suffix):
            n = n[:-len(suffix)].strip()
    for prefix in ["fc ", "cf ", "sc ", "ac ", "as ", "ss "]:
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
    return n.replace(".", "").replace("'", "")


def teams_match(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if na == nb or na in nb or nb in na:
        return True
    for canonical, aliases in TEAM_ALIASES.items():
        all_norm = [normalize(canonical)] + [normalize(x) for x in aliases]
        if any(na == x or na in x or x in na for x in all_norm) and \
           any(nb == x or nb in x or x in nb for x in all_norm):
            return True
    return False


# ── Bookmaker odds fetch ──────────────────────────────────────────────────────
def fetch_bookmaker_odds(sport_key: str) -> list:
    """Haal odds op voor één sport. Retourneer lijst van wedstrijden met fair probs."""
    url = f"{ODDS_BASE}/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    remaining = resp.headers.get("x-requests-remaining", "?")
    if resp.status_code != 200:
        log.warning(f"Odds API {sport_key}: HTTP {resp.status_code}")
        return []

    data = resp.json()
    log.info(f"{sport_key}: {len(data)} games (API credits remaining: {remaining})")

    games = []
    for game in data:
        home = game["home_team"]
        away = game["away_team"]
        commence = game.get("commence_time", "")
        bookmakers = game.get("bookmakers", [])

        if len(bookmakers) < MIN_BOOKMAKERS:
            continue

        # Bereken consensus fair probs (gemiddelde over bookmakers, vig verwijderd)
        outcome_prices: dict = {}
        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for outcome in market["outcomes"]:
                    name = outcome["name"]
                    price = outcome["price"]
                    outcome_prices.setdefault(name, []).append(price)

        if not outcome_prices:
            continue

        raw_probs = {name: 1.0 / (sum(p) / len(p)) for name, p in outcome_prices.items()}
        total = sum(raw_probs.values())
        fair = {name: p / total for name, p in raw_probs.items()}

        bm_home = bm_away = bm_draw = None
        for name, prob in fair.items():
            if name.lower() == "draw":
                bm_draw = prob
            elif teams_match(home, name):
                bm_home = prob
            elif teams_match(away, name):
                bm_away = prob

        games.append({
            "sport": sport_key,
            "home": home,
            "away": away,
            "commence": commence,
            "bm_home": bm_home,
            "bm_away": bm_away,
            "bm_draw": bm_draw,
            "num_bm": len(bookmakers),
        })

    return games


# ── PM soccer market fetch ────────────────────────────────────────────────────
def fetch_pm_soccer(pm_tag: str) -> dict:
    """Haal PM voetbalmarkten op. Retourneer dict: team_key → {home_win, away_win, draw, ...}"""
    all_events = []
    offset = 0
    while True:
        r = requests.get(PM_EVENTS_URL, params={
            "tag_slug": pm_tag, "limit": 100, "active": "true", "closed": "false", "offset": offset,
        }, timeout=15)
        data = r.json()
        if not data:
            break
        all_events.extend(data)
        if len(data) < 100:
            break
        offset += 100

    markets = {}
    for event in all_events:
        title = event.get("title", "")
        if "vs" not in title.lower():
            continue
        if any(x in title.lower() for x in ["more market", "halftime", "exact score",
                                              "correct score", "both teams", "total goals",
                                              "goal scorer", "first goal", "last goal",
                                              "card", "corner"]):
            continue

        entry = {"title": title, "pm_tag": pm_tag, "home_win": None, "away_win": None,
                 "draw": None, "home_name": None, "away_name": None,
                 "condition_ids": {}, "token_ids": {}}

        for market in event.get("markets", []):
            q = market.get("question", "")
            outcomes = market.get("outcomes", "")
            prices = market.get("outcomePrices", "")
            condition_id = market.get("conditionId", "")
            tokens = market.get("clobTokenIds", "")

            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: continue
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: continue
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except: tokens = []

            if not prices or not outcomes:
                continue

            price_map = {}
            token_map = {}
            for i, name in enumerate(outcomes):
                if i < len(prices):
                    try:
                        price_map[name] = float(prices[i])
                        if i < len(tokens):
                            token_map[name] = tokens[i]
                    except: pass

            yes_price = price_map.get("Yes")
            no_price = price_map.get("No")
            yes_token = token_map.get("Yes", "")
            no_token = token_map.get("No", "")

            if "win" in q.lower() and yes_price is not None:
                win_match = re.search(r"Will (.+?) win", q, re.IGNORECASE)
                if win_match:
                    team_name = win_match.group(1).strip()
                    if entry["home_win"] is None:
                        entry["home_win"] = yes_price
                        entry["home_name"] = team_name
                        entry["condition_ids"]["home_win_yes"] = condition_id
                        entry["condition_ids"]["home_win_no"] = condition_id
                        entry["token_ids"]["home_win_yes"] = yes_token
                        entry["token_ids"]["home_win_no"] = no_token
                    else:
                        entry["away_win"] = yes_price
                        entry["away_name"] = team_name
                        entry["condition_ids"]["away_win_yes"] = condition_id
                        entry["condition_ids"]["away_win_no"] = condition_id
                        entry["token_ids"]["away_win_yes"] = yes_token
                        entry["token_ids"]["away_win_no"] = no_token

            if "draw" in q.lower() and yes_price is not None:
                entry["draw"] = yes_price
                entry["condition_ids"]["draw_yes"] = condition_id
                entry["token_ids"]["draw_yes"] = yes_token

        if entry["home_win"] is not None or entry["draw"] is not None:
            markets[f"{pm_tag}|{title}"] = entry

    return markets


# ── PM NBA market fetch ───────────────────────────────────────────────────────
def fetch_pm_nba() -> dict:
    """
    Haal PM NBA moneyline markten op via schedule_cache slugs.
    NBA game markets op PM hebben team-namen als outcomes (geen Yes/No).
    Retourneer dict keyed by slug: {team1, team2, pm_team1, pm_team2, ...}
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=3)   # wedstrijden die net begonnen zijn
    window_end   = now + timedelta(hours=28)  # komende ~dag

    # Laad schedule cache voor NBA slugs
    slugs = []
    try:
        with open(SCHEDULE_FILE) as f:
            games = json.load(f)
        for g in games:
            slug = g.get("event_slug", "")
            if not slug.startswith("nba-"):
                continue
            start_str = g.get("start_time", "")
            if not start_str:
                continue
            try:
                t = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if window_start <= t <= window_end:
                    slugs.append(slug)
            except:
                continue
    except FileNotFoundError:
        log.warning(f"schedule_cache.json niet gevonden: {SCHEDULE_FILE}")
        return {}

    log.info(f"NBA: {len(slugs)} wedstrijden in schedule cache voor komende 28u")

    markets = {}
    for slug in slugs:
        try:
            r = requests.get(PM_EVENTS_URL, params={"slug": slug}, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data:
                continue
            event = data[0] if isinstance(data, list) else data
            title = event.get("title", "")

            for market in event.get("markets", []):
                outcomes = market.get("outcomes", "")
                prices   = market.get("outcomePrices", "")
                cid      = market.get("conditionId", "")
                tokens   = market.get("clobTokenIds", "")

                if isinstance(outcomes, str):
                    try: outcomes = json.loads(outcomes)
                    except: continue
                if isinstance(prices, str):
                    try: prices = json.loads(prices)
                    except: continue
                if isinstance(tokens, str):
                    try: tokens = json.loads(tokens)
                    except: tokens = []

                if not outcomes or len(outcomes) != 2:
                    continue
                # NBA moneyline: 2 team-namen, GEEN Yes/No
                if "Yes" in outcomes or "No" in outcomes:
                    continue

                price_map = {}
                token_map = {}
                for i, name in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            price_map[name] = float(prices[i])
                            if i < len(tokens):
                                token_map[name] = tokens[i]
                        except: pass

                if len(price_map) != 2:
                    continue

                team1, team2 = outcomes[0], outcomes[1]
                markets[slug] = {
                    "slug": slug,
                    "title": title,
                    "team1": team1,
                    "team2": team2,
                    "pm_team1": price_map[team1],
                    "pm_team2": price_map[team2],
                    "condition_id": cid,
                    "token_team1": token_map.get(team1, ""),
                    "token_team2": token_map.get(team2, ""),
                }
                break  # moneyline gevonden, stop inner loop
        except Exception as e:
            log.debug(f"NBA slug {slug}: {e}")
            continue

    log.info(f"NBA: {len(markets)} moneyline markten gevonden op PM")
    return markets


# ── Signal generation ────────────────────────────────────────────────────────
def make_trade_entry(
    token_id: str,
    condition_id: str,
    market_title: str,
    sport: str,
    side: str,       # "buy"
    outcome: str,    # "Yes" | "No"
    price: float,
    edge_pct: float,
    bm_prob: float,
    num_bm: int,
    event_slug: Optional[str] = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "timestamp": now,
        "token_id": token_id,
        "condition_id": condition_id,
        "market_title": market_title,
        "sport": sport,
        "side": side,
        "outcome": outcome,
        "price": round(price, 4),
        "size_usdc": FLAT_SIZE_USDC,
        "size_shares": round(FLAT_SIZE_USDC / price, 4) if price > 0 else 0,
        "signal_source": "paper_odds",
        "copy_wallet": None,
        "consensus_count": num_bm,
        "edge_pct": round(edge_pct, 2),
        "confidence": 1.0,
        "signal_delay_ms": 0,
        "event_slug": event_slug,
        "order_id": None,
        "filled": True,
        "dry_run": True,
        "result": None,
        "pnl": None,
        "resolved_at": None,
        "sell_price": None,
        "actual_pnl": None,
        "exit_type": None,
        "strategy_version": "paper_v1",
        "_bm_prob": round(bm_prob, 4),
    }


def already_logged(condition_id: str, outcome: str) -> bool:
    """Check of dit signaal al open staat in trades.jsonl (voorkom dubbelen)."""
    key = f"{condition_id}:{outcome}"
    try:
        with open(TRADES_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                    if t.get("condition_id") == condition_id \
                       and t.get("outcome") == outcome \
                       and t.get("result") is None:
                        return True
                except: pass
    except FileNotFoundError:
        pass
    return False


def append_trade(entry: dict):
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Soccer scan ──────────────────────────────────────────────────────────────
def scan_soccer() -> list:
    signals = []

    for sport_key, pm_tag in SOCCER_LEAGUES.items():
        bm_games = fetch_bookmaker_odds(sport_key)
        pm_markets = fetch_pm_soccer(pm_tag)

        for bm in bm_games:
            best_pm = None
            best_score = 0

            for pm_key, pm in pm_markets.items():
                if pm["pm_tag"] != pm_tag:
                    continue
                home_match = (pm["home_name"] and teams_match(bm["home"], pm["home_name"])) or \
                             (pm["away_name"] and teams_match(bm["home"], pm["away_name"]))
                away_match = (pm["away_name"] and teams_match(bm["away"], pm["away_name"])) or \
                             (pm["home_name"] and teams_match(bm["away"], pm["home_name"]))
                score = int(bool(home_match)) + int(bool(away_match))
                if score > best_score:
                    best_score = score
                    best_pm = pm

            if not best_pm or best_score < 2:
                continue

            # Bepaal PM home/away oriëntatie
            if best_pm["home_name"] and teams_match(bm["home"], best_pm["home_name"]):
                pm_home_win = best_pm["home_win"]
                pm_home_no_token = best_pm["token_ids"].get("home_win_no", "")
                pm_home_cid = best_pm["condition_ids"].get("home_win_no", "")
                pm_away_win = best_pm["away_win"]
                pm_away_no_token = best_pm["token_ids"].get("away_win_no", "")
                pm_away_cid = best_pm["condition_ids"].get("away_win_no", "")
            else:
                pm_home_win = best_pm["away_win"]
                pm_home_no_token = best_pm["token_ids"].get("away_win_no", "")
                pm_home_cid = best_pm["condition_ids"].get("away_win_no", "")
                pm_away_win = best_pm["home_win"]
                pm_away_no_token = best_pm["token_ids"].get("home_win_no", "")
                pm_away_cid = best_pm["condition_ids"].get("home_win_no", "")

            pm_draw = best_pm["draw"]
            pm_draw_token = best_pm["token_ids"].get("draw_yes", "")
            pm_draw_cid = best_pm["condition_ids"].get("draw_yes", "")

            title = best_pm["title"]
            event_slug = f"{pm_tag}-{normalize(bm['home'])}-vs-{normalize(bm['away'])}"

            # Signal 1: Favoriet Win NO (PM goedkoper dan BM fair)
            # BM favoriet = team met hoogste fair prob
            if bm["bm_home"] and pm_home_win is not None:
                # Win NO price op PM = 1 - Win YES price
                pm_home_no = 1.0 - pm_home_win
                bm_home_no = 1.0 - bm["bm_home"]  # fair prob van "home wint NIET"
                edge = (bm_home_no - pm_home_no) * 100
                if edge >= MIN_EDGE_PCT and MIN_PRICE <= pm_home_no <= MAX_PRICE:
                    if not already_logged(pm_home_cid, "No"):
                        entry = make_trade_entry(
                            token_id=pm_home_no_token,
                            condition_id=pm_home_cid,
                            market_title=f"Will {bm['home']} win? ({title})",
                            sport=f"soccer_{pm_tag.replace('-', '_')}",
                            side="buy", outcome="No",
                            price=pm_home_no, edge_pct=edge,
                            bm_prob=bm_home_no, num_bm=bm["num_bm"],
                            event_slug=event_slug,
                        )
                        signals.append(entry)
                        log.info(f"SIGNAL soccer Win NO: {bm['home']} | PM={pm_home_no:.3f} BM={bm_home_no:.3f} edge={edge:.1f}pp")

            if bm["bm_away"] and pm_away_win is not None:
                pm_away_no = 1.0 - pm_away_win
                bm_away_no = 1.0 - bm["bm_away"]
                edge = (bm_away_no - pm_away_no) * 100
                if edge >= MIN_EDGE_PCT and MIN_PRICE <= pm_away_no <= MAX_PRICE:
                    if not already_logged(pm_away_cid, "No"):
                        entry = make_trade_entry(
                            token_id=pm_away_no_token,
                            condition_id=pm_away_cid,
                            market_title=f"Will {bm['away']} win? ({title})",
                            sport=f"soccer_{pm_tag.replace('-', '_')}",
                            side="buy", outcome="No",
                            price=pm_away_no, edge_pct=edge,
                            bm_prob=bm_away_no, num_bm=bm["num_bm"],
                            event_slug=event_slug,
                        )
                        signals.append(entry)
                        log.info(f"SIGNAL soccer Win NO: {bm['away']} | PM={pm_away_no:.3f} BM={bm_away_no:.3f} edge={edge:.1f}pp")

            # Signal 2: Draw YES (PM goedkoper dan BM fair draw prob)
            if bm["bm_draw"] and pm_draw is not None:
                edge = (bm["bm_draw"] - pm_draw) * 100
                if edge >= MIN_EDGE_PCT and MIN_PRICE <= pm_draw <= MAX_PRICE:
                    if not already_logged(pm_draw_cid, "Yes"):
                        entry = make_trade_entry(
                            token_id=pm_draw_token,
                            condition_id=pm_draw_cid,
                            market_title=f"Draw: {title}",
                            sport=f"soccer_{pm_tag.replace('-', '_')}",
                            side="buy", outcome="Yes",
                            price=pm_draw, edge_pct=edge,
                            bm_prob=bm["bm_draw"], num_bm=bm["num_bm"],
                            event_slug=event_slug,
                        )
                        signals.append(entry)
                        log.info(f"SIGNAL soccer Draw YES: {title} | PM={pm_draw:.3f} BM={bm['bm_draw']:.3f} edge={edge:.1f}pp")

    return signals


# ── NBA scan ─────────────────────────────────────────────────────────────────
def scan_nba() -> list:
    signals = []

    bm_games = fetch_bookmaker_odds(NBA_SPORT_KEY)
    pm_markets = fetch_pm_nba()

    for bm in bm_games:
        # Zoek matching PM markt (beide teams moeten matchen)
        best_pm = None
        for pm in pm_markets.values():
            home_ok = teams_match(bm["home"], pm["team1"]) or teams_match(bm["home"], pm["team2"])
            away_ok = teams_match(bm["away"], pm["team1"]) or teams_match(bm["away"], pm["team2"])
            if home_ok and away_ok:
                best_pm = pm
                break

        if not best_pm:
            continue

        cid        = best_pm["condition_id"]
        event_slug = best_pm["slug"]
        title      = best_pm["title"]

        # Wijs home/away toe op basis van team-match
        if teams_match(bm["home"], best_pm["team1"]):
            pm_home_price = best_pm["pm_team1"]
            pm_home_token = best_pm["token_team1"]
            home_outcome  = best_pm["team1"]
            pm_away_price = best_pm["pm_team2"]
            pm_away_token = best_pm["token_team2"]
            away_outcome  = best_pm["team2"]
        else:
            pm_home_price = best_pm["pm_team2"]
            pm_home_token = best_pm["token_team2"]
            home_outcome  = best_pm["team2"]
            pm_away_price = best_pm["pm_team1"]
            pm_away_token = best_pm["token_team1"]
            away_outcome  = best_pm["team1"]

        # Signal: home team goedkoper op PM dan BM fair prob
        if bm["bm_home"] is not None:
            edge = (bm["bm_home"] - pm_home_price) * 100
            if edge >= MIN_EDGE_PCT and MIN_PRICE <= pm_home_price <= MAX_PRICE:
                if not already_logged(cid, home_outcome):
                    entry = make_trade_entry(
                        token_id=pm_home_token, condition_id=cid,
                        market_title=title, sport="basketball_nba",
                        side="buy", outcome=home_outcome,
                        price=pm_home_price, edge_pct=edge,
                        bm_prob=bm["bm_home"], num_bm=bm["num_bm"],
                        event_slug=event_slug,
                    )
                    signals.append(entry)
                    log.info(f"SIGNAL NBA: {home_outcome} | PM={pm_home_price:.3f} BM={bm['bm_home']:.3f} edge={edge:.1f}pp")

        # Signal: away team goedkoper op PM dan BM fair prob
        if bm["bm_away"] is not None:
            edge = (bm["bm_away"] - pm_away_price) * 100
            if edge >= MIN_EDGE_PCT and MIN_PRICE <= pm_away_price <= MAX_PRICE:
                if not already_logged(cid, away_outcome):
                    entry = make_trade_entry(
                        token_id=pm_away_token, condition_id=cid,
                        market_title=title, sport="basketball_nba",
                        side="buy", outcome=away_outcome,
                        price=pm_away_price, edge_pct=edge,
                        bm_prob=bm["bm_away"], num_bm=bm["num_bm"],
                        event_slug=event_slug,
                    )
                    signals.append(entry)
                    log.info(f"SIGNAL NBA: {away_outcome} | PM={pm_away_price:.3f} BM={bm['bm_away']:.3f} edge={edge:.1f}pp")

    return signals


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 70)
    log.info("PAPER SIGNAL SCANNER — start")
    log.info(f"Datum: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"Config: edge>={MIN_EDGE_PCT}pp | prijs [{MIN_PRICE}-{MAX_PRICE}] | min_bm={MIN_BOOKMAKERS}")
    log.info("=" * 70)

    all_signals = []

    log.info("[STAP 1] Voetbal scannen...")
    try:
        soccer_signals = scan_soccer()
        all_signals.extend(soccer_signals)
        log.info(f"Voetbal: {len(soccer_signals)} nieuwe signalen")
    except Exception as e:
        log.error(f"Voetbal scan mislukt: {e}")

    log.info("[STAP 2] NBA scannen...")
    try:
        nba_signals = scan_nba()
        all_signals.extend(nba_signals)
        log.info(f"NBA: {len(nba_signals)} nieuwe signalen")
    except Exception as e:
        log.error(f"NBA scan mislukt: {e}")

    log.info(f"\nTotaal nieuwe signalen: {len(all_signals)}")

    if all_signals:
        for entry in all_signals:
            append_trade(entry)
            log.info(f"  LOGGED: {entry['sport']} | {entry['market_title'][:60]} | "
                     f"{entry['outcome']} @ {entry['price']:.3f} | edge={entry['edge_pct']:.1f}pp")
    else:
        log.info("Geen nieuwe signalen gevonden.")

    log.info("=" * 70)
    log.info("PAPER SIGNAL SCANNER — klaar")


if __name__ == "__main__":
    main()
