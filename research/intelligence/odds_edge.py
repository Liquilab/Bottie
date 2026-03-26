"""Module 7: Odds Edge Detection — PM price vs bookmaker odds.

Hypothesis: Cannae compares PM implied probability with sharp bookmaker odds
(Pinnacle/Bet365). When PM is mispriced → buy.

The Odds API (free tier, 500 req/month):
- Fetches h2h, spreads, totals for active leagues
- Stores snapshots in data/odds_snapshots/YYYY-MM-DD.jsonl
- Matches Cannae trades against bookmaker odds at entry time
"""

import json
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("intelligence.odds_edge")

ODDS_API = "https://api.the-odds-api.com/v4"
SNAPSHOTS_DIR = Path("data/odds_snapshots")
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# PM slug prefix → Odds API sport key
LEAGUE_MAP = {
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "epl": "soccer_epl",
    "lal": "soccer_spain_la_liga",
    "es2": "soccer_spain_segunda_division",
    "bun": "soccer_germany_bundesliga",
    "fl1": "soccer_france_ligue_one",
    "itc": "soccer_italy_serie_a",
    "arg": "soccer_argentina_primera_division",
    "bra": "soccer_brazil_campeonato",
    "mls": "soccer_usa_mls",
    "ere": "soccer_netherlands_eredivisie",
    "por": "soccer_portugal_primeira_liga",
    "mex": "soccer_mexico_ligamx",
    "uel": "soccer_uefa_europa_league",
    "uef": "soccer_uefa_nations_league",
}

# Common team name aliases (PM short → Odds API full)
# Extend as needed based on matching failures
TEAM_ALIASES = {
    # NBA
    "lakers": "los angeles lakers",
    "celtics": "boston celtics",
    "76ers": "philadelphia 76ers",
    "sixers": "philadelphia 76ers",
    "knicks": "new york knicks",
    "nets": "brooklyn nets",
    "warriors": "golden state warriors",
    "bucks": "milwaukee bucks",
    "heat": "miami heat",
    "nuggets": "denver nuggets",
    "suns": "phoenix suns",
    "mavs": "dallas mavericks",
    "mavericks": "dallas mavericks",
    "thunder": "oklahoma city thunder",
    "cavs": "cleveland cavaliers",
    "cavaliers": "cleveland cavaliers",
    "bulls": "chicago bulls",
    "raptors": "toronto raptors",
    "hawks": "atlanta hawks",
    "pacers": "indiana pacers",
    "magic": "orlando magic",
    "pistons": "detroit pistons",
    "rockets": "houston rockets",
    "spurs": "san antonio spurs",
    "wolves": "minnesota timberwolves",
    "timberwolves": "minnesota timberwolves",
    "pelicans": "new orleans pelicans",
    "kings": "sacramento kings",
    "blazers": "portland trail blazers",
    "grizzlies": "memphis grizzlies",
    "jazz": "utah jazz",
    "clippers": "los angeles clippers",
    "hornets": "charlotte hornets",
    "wizards": "washington wizards",
    # NHL
    "rangers": "new york rangers",
    "bruins": "boston bruins",
    "maple leafs": "toronto maple leafs",
    "canadiens": "montreal canadiens",
    "penguins": "pittsburgh penguins",
    "blackhawks": "chicago blackhawks",
    "oilers": "edmonton oilers",
    "flames": "calgary flames",
    "canucks": "vancouver canucks",
    "avalanche": "colorado avalanche",
    "lightning": "tampa bay lightning",
    "panthers": "florida panthers",
    "capitals": "washington capitals",
    "stars": "dallas stars",
    "wild": "minnesota wild",
    "red wings": "detroit red wings",
    "senators": "ottawa senators",
    "islanders": "new york islanders",
    "devils": "new jersey devils",
    "hurricanes": "carolina hurricanes",
    "blue jackets": "columbus blue jackets",
    "predators": "nashville predators",
    "ducks": "anaheim ducks",
    "sharks": "san jose sharks",
    "coyotes": "utah hockey club",
    "kraken": "seattle kraken",
    "golden knights": "vegas golden knights",
    "jets": "winnipeg jets",
    "sabres": "buffalo sabres",
    "flyers": "philadelphia flyers",
}


def collect_odds(active_leagues: list[str] = None, api_key: str = None) -> dict:
    """Fetch odds from The Odds API for active leagues and store snapshot.

    Args:
        active_leagues: PM league prefixes to fetch (e.g. ["epl", "bun"]).
                       If None, fetches top 5 by Cannae activity.
        api_key: Odds API key. Falls back to ODDS_API_KEY env var.

    Returns: dict with sport_key → list of games with odds
    """
    api_key = api_key or os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        log.error("No ODDS_API_KEY set")
        return {}

    if active_leagues is None:
        active_leagues = ["epl", "bun", "lal", "itc", "nba"]

    # Map PM prefixes to Odds API sport keys
    sport_keys = []
    for league in active_leagues:
        if league in LEAGUE_MAP:
            sport_keys.append(LEAGUE_MAP[league])

    if not sport_keys:
        log.warning("No mappable leagues")
        return {}

    client = httpx.Client(timeout=30)
    all_odds = {}
    requests_used = 0

    for sport_key in sport_keys:
        try:
            url = f"{ODDS_API}/sports/{sport_key}/odds/"
            params = {
                "apiKey": api_key,
                "regions": "eu,uk",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "decimal",
            }
            resp = client.get(url, params=params)
            requests_used += 1

            if resp.status_code == 401:
                log.error("Odds API key invalid")
                break
            if resp.status_code == 429:
                log.warning("Odds API rate limit reached")
                break
            resp.raise_for_status()

            games = resp.json()
            all_odds[sport_key] = games
            log.info(f"  {sport_key}: {len(games)} games fetched")

            # Check remaining requests from headers
            remaining = resp.headers.get("x-requests-remaining", "?")
            log.info(f"  API requests remaining: {remaining}")

        except Exception as e:
            log.error(f"  {sport_key} failed: {e}")

    client.close()

    # Save snapshot
    if all_odds:
        _save_snapshot(all_odds)

    log.info(f"Odds collection done: {requests_used} API calls, {sum(len(v) for v in all_odds.values())} games")
    return all_odds


def _save_snapshot(all_odds: dict):
    """Save odds snapshot to daily JSONL file."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_file = SNAPSHOTS_DIR / f"{today}.jsonl"

    ts = datetime.now(timezone.utc).isoformat()
    with open(snapshot_file, "a") as f:
        for sport_key, games in all_odds.items():
            for game in games:
                record = {
                    "ts": ts,
                    "sport_key": sport_key,
                    "game_id": game.get("id", ""),
                    "home_team": game.get("home_team", ""),
                    "away_team": game.get("away_team", ""),
                    "commence_time": game.get("commence_time", ""),
                    "bookmakers": _extract_best_odds(game.get("bookmakers", [])),
                }
                f.write(json.dumps(record) + "\n")

    log.info(f"Snapshot saved to {snapshot_file}")


def _extract_best_odds(bookmakers: list) -> dict:
    """Extract sharpest odds (Pinnacle preferred, then Bet365, then best available)."""
    priority = ["pinnacle", "betfair_ex_eu", "bet365", "1xbet", "betsson"]
    best = {}

    for market_key in ["h2h", "spreads", "totals"]:
        for pref in priority:
            for bm in bookmakers:
                if bm.get("key", "").lower() == pref:
                    for market in bm.get("markets", []):
                        if market.get("key") == market_key:
                            best[market_key] = {
                                "bookmaker": bm["key"],
                                "outcomes": market.get("outcomes", []),
                            }
                            break
                if market_key in best:
                    break
            if market_key in best:
                break

        # Fallback: first bookmaker with this market
        if market_key not in best:
            for bm in bookmakers:
                for market in bm.get("markets", []):
                    if market.get("key") == market_key:
                        best[market_key] = {
                            "bookmaker": bm["key"],
                            "outcomes": market.get("outcomes", []),
                        }
                        break
                if market_key in best:
                    break

    return best


def analyze_odds_edge(dataset: dict) -> dict:
    """Match Cannae's trades against bookmaker odds to find his edge source."""
    resolved = dataset["resolved"]

    # Load all historical snapshots
    snapshots = _load_all_snapshots()
    if not snapshots:
        return {
            "status": "no_odds_data",
            "note": "Run collect_odds() first to build odds history",
        }

    # Match trades to odds
    matches = _match_trades_to_odds(resolved, snapshots)
    if not matches:
        return {
            "status": "no_matches",
            "snapshots_loaded": len(snapshots),
            "note": "No trades could be matched to odds snapshots (need more data)",
        }

    return {
        "matched_trades": len(matches),
        "total_resolved": len(resolved),
        "match_rate": round(len(matches) / len(resolved), 4) if resolved else 0,
        "edge_analysis": _compute_edge(matches),
        "by_league": _edge_by_league(matches),
        "edge_vs_sizing": _edge_vs_sizing(matches),
        "edge_vs_outcome": _edge_vs_outcome(matches),
    }


def _load_all_snapshots() -> list:
    """Load all odds snapshots from disk."""
    records = []
    for f in sorted(SNAPSHOTS_DIR.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    log.info(f"Loaded {len(records)} odds snapshot records")
    return records


def _match_trades_to_odds(resolved: list, snapshots: list) -> list:
    """Match Cannae trades to bookmaker odds using team name fuzzy matching."""
    # Index snapshots by sport_key + date for fast lookup
    snap_index = defaultdict(list)
    for s in snapshots:
        date = s.get("ts", "")[:10]
        snap_index[(s.get("sport_key", ""), date)].append(s)

    matches = []
    for bet in resolved:
        league = bet["league"]
        sport_key = LEAGUE_MAP.get(league)
        if not sport_key:
            continue

        # Find snapshots around the trade date
        trade_date = datetime.fromtimestamp(bet["first_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        candidates = snap_index.get((sport_key, trade_date), [])

        # Also check day before (trade might be placed day before game)
        from datetime import timedelta
        prev_date = (datetime.fromtimestamp(bet["first_ts"], tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        candidates += snap_index.get((sport_key, prev_date), [])

        if not candidates:
            continue

        # Try to match by team names in the event slug/outcome
        slug = bet.get("event_slug", "").lower()
        outcome = bet.get("outcome", "").lower()
        title = bet.get("title", "").lower()

        best_match = None
        best_score = 0

        for snap in candidates:
            home = snap.get("home_team", "").lower()
            away = snap.get("away_team", "").lower()

            score = _match_score(slug, outcome, title, home, away)
            if score > best_score:
                best_score = score
                best_match = snap

        if best_match and best_score >= 2:
            # Extract relevant bookmaker odds
            h2h = best_match.get("bookmakers", {}).get("h2h", {})
            pm_prob = bet["avg_price"]

            # Find matching outcome in bookmaker odds
            book_prob = _find_book_prob(h2h, outcome, title, best_match)
            if book_prob is not None:
                edge = book_prob - pm_prob
                matches.append({
                    **bet,
                    "book_prob": round(book_prob, 4),
                    "pm_prob": round(pm_prob, 4),
                    "edge": round(edge, 4),
                    "bookmaker": h2h.get("bookmaker", "unknown"),
                    "matched_game": f"{best_match.get('home_team')} vs {best_match.get('away_team')}",
                })

    log.info(f"Matched {len(matches)} of {len(resolved)} trades to odds")
    return matches


def _match_score(slug: str, outcome: str, title: str, home: str, away: str) -> int:
    """Score how well a trade matches an odds game. Higher = better."""
    score = 0
    combined = f"{slug} {outcome} {title}"

    # Check team names
    home_parts = home.split()
    away_parts = away.split()

    for part in home_parts:
        if len(part) >= 3 and part in combined:
            score += 1
    for part in away_parts:
        if len(part) >= 3 and part in combined:
            score += 1

    # Check aliases
    for alias, full in TEAM_ALIASES.items():
        if alias in combined:
            if full == home or full == away:
                score += 2

    return score


def _find_book_prob(h2h: dict, outcome: str, title: str, snap: dict) -> float | None:
    """Find bookmaker implied probability for the matching outcome."""
    outcomes = h2h.get("outcomes", [])
    if not outcomes:
        return None

    outcome_lower = outcome.lower()
    title_lower = title.lower()

    for o in outcomes:
        name = o.get("name", "").lower()
        price = o.get("price", 0)
        if price <= 1:
            continue

        # Direct name match
        if name in outcome_lower or outcome_lower in name:
            return 1.0 / price

        # Check aliases
        for alias, full in TEAM_ALIASES.items():
            if alias in outcome_lower and full == name:
                return 1.0 / price

        # Draw match
        if "draw" in outcome_lower and name == "draw":
            return 1.0 / price

        # Over/Under match
        if "over" in title_lower and "over" in name.lower():
            return 1.0 / price
        if "under" in title_lower and "under" in name.lower():
            return 1.0 / price

    return None


def _compute_edge(matches: list) -> dict:
    """Overall edge statistics."""
    edges = [m["edge"] for m in matches]
    positive = [e for e in edges if e > 0]
    negative = [e for e in edges if e < 0]

    return {
        "avg_edge": round(statistics.mean(edges), 4),
        "median_edge": round(statistics.median(edges), 4),
        "positive_edge_pct": round(len(positive) / len(edges), 4) if edges else 0,
        "avg_positive_edge": round(statistics.mean(positive), 4) if positive else 0,
        "avg_negative_edge": round(statistics.mean(negative), 4) if negative else 0,
    }


def _edge_by_league(matches: list) -> dict:
    """Edge broken down by league."""
    leagues = defaultdict(list)
    for m in matches:
        leagues[m["league"]].append(m)

    result = {}
    for league, bets in sorted(leagues.items(), key=lambda x: -len(x[1])):
        if len(bets) < 3:
            continue
        edges = [b["edge"] for b in bets]
        wins = sum(1 for b in bets if b["result"] == "WIN")
        result[league] = {
            "bets": len(bets),
            "avg_edge": round(statistics.mean(edges), 4),
            "wr": round(wins / len(bets), 4),
            "positive_edge_pct": round(sum(1 for e in edges if e > 0) / len(edges), 4),
        }
    return result


def _edge_vs_sizing(matches: list) -> dict:
    """Does edge predict bet size?"""
    if len(matches) < 10:
        return {"insufficient_data": True}

    # Split by median edge
    sorted_by_edge = sorted(matches, key=lambda m: m["edge"])
    mid = len(sorted_by_edge) // 2
    low_edge = sorted_by_edge[:mid]
    high_edge = sorted_by_edge[mid:]

    return {
        "high_edge_avg_cost": round(statistics.mean(m["cost"] for m in high_edge), 2),
        "low_edge_avg_cost": round(statistics.mean(m["cost"] for m in low_edge), 2),
        "bigger_bets_on_bigger_edge": (
            statistics.mean(m["cost"] for m in high_edge) >
            statistics.mean(m["cost"] for m in low_edge) * 1.1
        ),
    }


def _edge_vs_outcome(matches: list) -> dict:
    """Does edge predict winning?"""
    wins = [m for m in matches if m["result"] == "WIN"]
    losses = [m for m in matches if m["result"] == "LOSS"]

    if not wins or not losses:
        return {"insufficient_data": True}

    return {
        "avg_edge_winners": round(statistics.mean(m["edge"] for m in wins), 4),
        "avg_edge_losers": round(statistics.mean(m["edge"] for m in losses), 4),
        "edge_predicts_outcome": (
            statistics.mean(m["edge"] for m in wins) >
            statistics.mean(m["edge"] for m in losses) + 0.02
        ),
    }
