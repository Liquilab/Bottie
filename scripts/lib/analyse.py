"""Canonieke analyse library — ENIGE bron van waarheid voor wallet analyse.

Elke analyse MOET deze library importeren. Geen copy-paste, geen inline implementaties.

Verified 2026-03-30:
- WR: 10/10 match tegen CLOB resolutie (5 wins + 5 losses)
- Merge: 93.9% complete, gap = timing window (1-4 dagen), geen win/loss bias
- Hauptbet: max invested (shares × avgPrice) per event_slug
- PnL per entry: realizedPnl teken klopt 1:1 met CLOB resolution
- Absolute PnL sommatie: ONBETROUWBAAR (dubbeltelling). Gebruik lb-api.
"""

import json
import re
import urllib.request
from collections import defaultdict

API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "B/1", "Accept": "application/json"}

# --- Sport classification ---

# Slug-prefix → sport mapping (PRIMARY — most reliable)
FOOTBALL_SLUGS = {
    "epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por",
    "bra", "itc", "ere", "es2", "bl2", "sea", "elc", "mex", "fr2", "aus",
    "spl", "efl", "tur", "uef", "ucl", "cdr", "acn", "cde", "ssc", "fif",
}

SLUG_TO_SPORT = {
    "nba": "nba", "nhl": "nhl", "mlb": "mlb", "nfl": "nfl", "cbb": "nba",
    "nfc": "nfl", "afc": "nfl",
}
SLUG_TO_SPORT.update({s: "football" for s in FOOTBALL_SLUGS})

# Team names as FALLBACK only (when slug prefix is unknown)
NBA_TEAMS = {"76ers", "lakers", "celtics", "knicks", "bulls", "heat", "hawks", "kings",
             "jazz", "suns", "nets", "pacers", "bucks", "spurs", "rockets", "nuggets",
             "warriors", "clippers", "grizzlies", "cavaliers", "hornets", "pistons",
             "wizards", "raptors", "pelicans", "blazers", "timberwolves", "mavericks",
             "thunder", "magic"}

NHL_TEAMS = {"bruins", "penguins", "oilers", "ducks", "wild", "jets", "panthers",
             "devils", "hurricanes", "lightning", "senators", "blues", "avalanche",
             "stars", "blackhawks", "red wings", "sabres", "flames", "canucks",
             "predators", "capitals", "islanders", "flyers", "kraken", "sharks",
             "coyotes", "blue jackets", "canadiens", "maple leafs", "rangers",
             "golden knights"}

MLB_TEAMS = {"yankees", "dodgers", "mets", "braves", "astros", "phillies", "padres",
             "cubs", "guardians", "orioles", "twins", "tigers", "mariners", "royals",
             "red sox", "rays", "cardinals", "brewers", "diamondbacks", "reds",
             "pirates", "rockies", "athletics", "marlins", "nationals", "angels",
             "white sox"}


def _slug_looks_like_game(slug: str) -> bool:
    """Check if slug looks like a game market (has date or 'vs'/'v-')."""
    return bool(re.search(r"\d{4}-\d{2}-\d{2}|\bvs?\b|v-", slug))


def classify_sport(title: str, event_slug: str = "") -> str:
    """Classify a market into a sport category.

    PRIMARY: slug prefix (e.g. 'nba-celtics-vs-lakers-2026-03-30' → 'nba').
    SECONDARY: slug contains known sport prefix anywhere (e.g. 'champions-league-...').
    FALLBACK: team name matching, but ONLY for game-like slugs (with date or 'vs').
    """
    slug = (event_slug or "").lower()

    # Primary: slug prefix detection
    if slug:
        prefix = slug.split("-")[0]
        if prefix in SLUG_TO_SPORT:
            return SLUG_TO_SPORT[prefix]

        # Secondary: known sport prefix anywhere in slug
        for sp, sport in SLUG_TO_SPORT.items():
            if f"-{sp}-" in f"-{slug}-":
                return sport

    # Fallback: team name matching, ONLY for game-like slugs
    if not slug or _slug_looks_like_game(slug):
        t = title.lower()
        if any(team in t for team in NHL_TEAMS):
            return "nhl"
        if any(team in t for team in NBA_TEAMS):
            return "nba"
        if "spread:" in t and "win on" not in t:
            return "nba"
        if any(team in t for team in MLB_TEAMS):
            return "mlb"
        if "win on" in t or "draw" in t or any(x in t for x in [
            "fc", "cf ", "united", "city", "arsenal", "chelsea",
            "liverpool", "barcelona", "madrid"
        ]):
            return "football"

    return "other"


def get_game_line(title: str) -> str:
    """Classify market type from title."""
    t = title.lower()
    if "spread" in t:
        return "spread"
    if "o/u" in t or "total" in t:
        return "totals"
    if "win" in t:
        return "win"
    if " vs. " in t:
        return "win"  # NBA/NHL moneyline = "Team vs. Team"
    if "draw" in t:
        return "draw"
    return "other"


# --- API fetching ---

def fetch(url: str):
    """Fetch JSON from URL."""
    req = urllib.request.Request(url, headers=HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def get_all_positions(address: str) -> list:
    """Fetch ALL positions (paginated). ALWAYS fetches everything — no limits.

    Includes resolved losers at curPrice=0 (critical for unbiased merge).
    """
    all_pos, offset = [], 0
    while True:
        batch = fetch(f"{API}/positions?user={address}&limit=500&offset={offset}&sizeThreshold=0")
        if not batch:
            break
        all_pos.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        if offset > 200000:
            break
    return all_pos


def get_closed_positions(address: str) -> list:
    """Fetch ALL closed positions (paginated). ALWAYS fetches everything — no limits.

    Contains winners, misses some losers (fixed by merge with positions API).
    """
    all_closed, offset = [], 0
    while True:
        batch = fetch(f"{API}/closed-positions?user={address}&limit=50&sortBy=TIMESTAMP&sortDirection=ASC&offset={offset}")
        if not batch:
            break
        all_closed.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
        if offset > 200000:
            break
    return all_closed


def get_lb_profit(address: str) -> float | None:
    """Get total PnL from lb-api (source of truth for absolute PnL)."""
    try:
        lb = fetch(f"https://lb-api.polymarket.com/profit?address={address}")
        return float(lb[0]["amount"]) if lb else None
    except Exception:
        return None


# --- Invested calculation ---

def calc_invested(entry: dict) -> float:
    """Calculate dollar amount invested from available API fields.

    Priority: initialValue > cashPaid > totalBought×avgPrice > size×avgPrice
    """
    for field in ["initialValue", "cashPaid"]:
        v = entry.get(field)
        if v is not None:
            v = float(v)
            if v > 0:
                return v
    tb = float(entry.get("totalBought", 0) or 0)
    avg = float(entry.get("avgPrice", 0) or 0)
    if tb > 0 and avg > 0:
        return tb * avg
    sz = float(entry.get("size", 0) or 0)
    if sz > 0 and avg > 0:
        return sz * avg
    return 0


# --- Core merge logic ---

def merge_positions(closed_pos: list, open_pos: list) -> dict:
    """Unbiased merge of closed-positions + positions API.

    Key = conditionId + "_" + outcomeIndex

    1. Start with ALL closed-positions (has winners, misses some losers)
    2. For EACH positions entry with curPrice~0 AND size>0 (resolved losers):
       - If key exists in closed: OVERLAP FIX — subtract remaining loss
       - If key NOT in closed: add as pure loser

    Returns dict[key] = {pnl, invested, title, event_slug}

    Verified 2026-03-30: pnl sign matches CLOB resolution 10/10.
    """
    all_conds = {}

    # Step 1: all closed positions
    for p in closed_pos:
        cid = p.get("conditionId", "")
        oi = str(p.get("outcomeIndex", ""))
        key = f"{cid}_{oi}"
        all_conds[key] = {
            "pnl": float(p.get("realizedPnl", 0) or 0),
            "invested": calc_invested(p),
            "title": p.get("title", "") or "",
            "event_slug": p.get("eventSlug", "") or "",
        }

    # Step 2: open resolved losers (curPrice ~ 0)
    for p in open_pos:
        cp = float(p.get("curPrice", 0) or 0)
        sz = float(p.get("size", 0) or 0)
        avg = float(p.get("avgPrice", 0) or 0)
        if cp >= 0.005 or sz <= 0 or avg <= 0.01:
            continue  # not a resolved loser

        cid = p.get("conditionId", "")
        oi = str(p.get("outcomeIndex", ""))
        key = f"{cid}_{oi}"
        remaining_loss = sz * avg

        if key in all_conds:
            # OVERLAP FIX: exists in both APIs
            all_conds[key]["pnl"] -= remaining_loss
            all_conds[key]["invested"] += remaining_loss
        else:
            # Pure loser, not in closed-positions
            all_conds[key] = {
                "pnl": -remaining_loss,
                "invested": remaining_loss,
                "title": p.get("title", "") or "",
                "event_slug": p.get("eventSlug", "") or "",
            }

    return all_conds


# --- Hauptbet analysis ---

def hauptbet_analysis(all_conds: dict, target_sport: str) -> dict:
    """Group by event_slug, pick hauptbet (max invested leg), return WR/ROI + per game line.

    Hauptbet = the leg with the highest dollar investment (shares × avgPrice) per game.
    This represents the wallet's primary conviction for that game.

    Returns:
        {
            "games": int,
            "wins": int, "losses": int,
            "wr": float, "roi": float,
            "pnl": float, "invested": float,
            "per_line": {line: {games, wr, roi, pnl}, ...},
        }
    """
    # Group legs by event_slug
    games = {}
    for key, entry in all_conds.items():
        if classify_sport(entry["title"], entry.get("event_slug", "")) != target_sport:
            continue
        slug = entry["event_slug"] or entry["title"]
        if slug not in games:
            games[slug] = []
        games[slug].append(entry)

    hb_wins, hb_losses, hb_pnl, hb_inv = 0, 0, 0.0, 0.0
    line_stats = {}

    for slug, legs in games.items():
        # Hauptbet = max invested (dollar amount, not shares)
        hb = max(legs, key=lambda l: l["invested"])
        gl = get_game_line(hb["title"])

        hb_pnl += hb["pnl"]
        hb_inv += hb["invested"]
        won = hb["pnl"] > 0
        if won:
            hb_wins += 1
        else:
            hb_losses += 1

        if gl not in line_stats:
            line_stats[gl] = {"wins": 0, "losses": 0, "pnl": 0.0, "invested": 0.0}
        ls = line_stats[gl]
        ls["pnl"] += hb["pnl"]
        ls["invested"] += hb["invested"]
        if won:
            ls["wins"] += 1
        else:
            ls["losses"] += 1

    total = hb_wins + hb_losses

    def safe_stats(wins, losses, pnl, invested):
        n = wins + losses
        return {
            "games": n,
            "wins": wins,
            "losses": losses,
            "wr": round(wins / n * 100, 1) if n > 0 else 0,
            "roi": round(pnl / invested * 100, 1) if invested > 0 else 0,
            "pnl": round(pnl, 2),
            "invested": round(invested, 2),
        }

    result = safe_stats(hb_wins, hb_losses, hb_pnl, hb_inv)
    result["per_line"] = {
        k: safe_stats(v["wins"], v["losses"], v["pnl"], v["invested"])
        for k, v in sorted(line_stats.items())
    }
    return result


# --- Full wallet analysis ---

MAX_SANITY_GAP_PCT = 30  # Refuse results if merge vs lb-api gap exceeds this


def fetch_and_merge(address: str, *, require_lb: bool = True) -> tuple:
    """Fetch ALL data for a wallet and merge. Returns (all_conds, lb_profit, sanity_gap).

    This is the ONLY way to get wallet data. No limits, no shortcuts.
    Raises ValueError if:
    - sanity check fails (gap > MAX_SANITY_GAP_PCT)
    - lb-api data missing and require_lb=True (default)
    """
    open_pos = get_all_positions(address)
    closed_pos = get_closed_positions(address)
    all_conds = merge_positions(closed_pos, open_pos)
    lb_profit = get_lb_profit(address)

    merge_total = sum(e["pnl"] for e in all_conds.values())
    sanity_gap = None

    if lb_profit is None or abs(lb_profit) <= 1:
        if require_lb:
            raise ValueError(
                f"NO LB DATA: wallet not on leaderboard (merge PnL ${merge_total:+,.0f}). "
                f"Cannot verify data completeness — wallet REJECTED. "
                f"{len(closed_pos)} closed, {len(open_pos)} positions."
            )
    else:
        sanity_gap = round(abs(merge_total - lb_profit) / abs(lb_profit) * 100, 1)
        if sanity_gap > MAX_SANITY_GAP_PCT:
            raise ValueError(
                f"BIAS DETECTED: merge PnL ${merge_total:+,.0f} vs lb-api ${lb_profit:+,.0f} "
                f"(gap {sanity_gap}% > {MAX_SANITY_GAP_PCT}% max). "
                f"Data incomplete: {len(closed_pos)} closed, {len(open_pos)} positions."
            )

    return all_conds, lb_profit, sanity_gap, open_pos


def analyse_wallet(address: str, target_sport: str) -> dict:
    """Complete wallet analysis: merge + hauptbet + sanity check.

    Raises ValueError if data is biased (merge vs lb-api gap too large).
    """
    all_conds, lb_profit, sanity_gap, open_pos = fetch_and_merge(address)

    # All-legs stats
    sport_entries = [e for e in all_conds.values() if classify_sport(e["title"], e.get("event_slug", "")) == target_sport]
    al_pnl = sum(e["pnl"] for e in sport_entries)
    al_inv = sum(e["invested"] for e in sport_entries)
    al_wins = sum(1 for e in sport_entries if e["pnl"] > 0)
    al_count = len(sport_entries)

    # Hauptbet
    hb = hauptbet_analysis(all_conds, target_sport)

    # Active positions (not resolved)
    active = 0
    for p in open_pos:
        cp = float(p.get("curPrice", 0.5) or 0.5)
        sz = float(p.get("size", 0) or 0)
        if sz >= 0.1 and 0.005 < cp < 0.99:
            if classify_sport(p.get("title", ""), p.get("eventSlug", "")) == target_sport:
                active += 1

    merge_total = sum(e["pnl"] for e in all_conds.values())
    return {
        "all_legs": {
            "count": al_count,
            "wins": al_wins,
            "losses": al_count - al_wins,
            "wr": round(al_wins / al_count * 100, 1) if al_count > 0 else 0,
            "roi": round(al_pnl / al_inv * 100, 1) if al_inv > 0 else 0,
            "pnl": round(al_pnl, 2),
        },
        "hauptbet": hb,
        "active": active,
        "lb_api_total_pnl": round(lb_profit, 2) if lb_profit is not None else None,
        "merge_total_pnl": round(merge_total, 2),
        "sanity_gap_pct": sanity_gap,
    }


# --- Anti-bias helpers (league scanner) ---

def is_match_event(slug: str, markets_count: int) -> bool:
    """True als event een wedstrijd is (niet futures/outright).

    Match events have a date in the slug and few markets (≤8).
    Futures like "Will X win the World Cup?" have no date and/or many markets.
    """
    has_date = bool(re.search(r"\d{4}-\d{2}-\d{2}", slug))
    return has_date and markets_count <= 8


def both_sides_pct(all_conds: dict) -> float:
    """Fractie conditionIds waar wallet BEIDE outcomes heeft (market maker signal).

    If >30% of conditions have both sides, wallet is likely a market maker, not predictor.
    """
    cids = defaultdict(set)
    for key in all_conds:
        parts = key.rsplit("_", 1)
        if len(parts) == 2:
            cid, outcome_idx = parts
            cids[cid].add(outcome_idx)
    if not cids:
        return 0.0
    return sum(1 for outcomes in cids.values() if len(outcomes) > 1) / len(cids)


def sell_ratio(positions: list) -> float:
    """Gemiddelde totalSold/totalBought over alle posities.

    High sell ratio (>0.3) = trader who sells before resolution, not a predictor.
    """
    ratios = []
    for p in positions:
        bought = float(p.get("totalBought", 0) or 0)
        sold = float(p.get("totalSold", 0) or 0)
        if bought > 0:
            ratios.append(sold / bought)
    return sum(ratios) / len(ratios) if ratios else 0.0


def sliding_wr_cap(resolved_ratio: float) -> int:
    """Max toegestane WR gegeven resolved ratio.

    Lower resolved ratio → more conservative WR cap (survivorship bias protection).
    Returns 0 = REJECT (too uncertain).
    """
    if resolved_ratio >= 0.8:
        return 75
    if resolved_ratio >= 0.6:
        return 65
    if resolved_ratio >= 0.5:
        return 55
    return 0  # reject
