"""
Shared Polymarket API library.

Consolidates fetch logic, sport detection, survivorship bias correction,
and Telegram alerts used across validation and wallet scout scripts.
"""

import asyncio
import json
import logging
import os
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import httpx

log = logging.getLogger("pm_api")

DATA_API = "https://data-api.polymarket.com"

SPORT_KEYWORDS = [
    "win on", "spread", "o/u", "over/under", "both teams",
    "nba", "nfl", "nhl", "mlb", "soccer", "football", "tennis",
    "ufc", "mma", "epl", "premier league", "champions league",
    "serie a", "bundesliga", "la liga", "mls", "ncaa", "college",
    "valorant", "esports", "league of legends",
    "fc", "united", "city fc", "vs.", "map 1 winner", "map 2 winner",
]

SPORT_SLUG_PREFIXES = [
    "nba-", "nfl-", "nhl-", "mlb-", "ufc-", "mma-",
    "epl-", "ucl-", "soccer-", "tennis-", "ncaa-",
    "bundesliga-", "serie-a-", "la-liga-", "mls-",
    "valorant-", "csgo-", "lol-",
]


def is_sport(position: dict) -> bool:
    title = (position.get("title") or "").lower()
    slug = (position.get("slug") or position.get("eventSlug") or "").lower()
    if any(slug.startswith(p) for p in SPORT_SLUG_PREFIXES):
        return True
    return any(kw in title for kw in SPORT_KEYWORDS)


def detect_market_type(title: str) -> str:
    """
    Detect market type from title (win/draw/ou/spread/btts/other).

    Logic matches Rust bot:
    - "win on" or "win the" (case-insensitive) → "win"
    - "draw" in title → "draw"
    - "o/u" or "over/under" in title → "ou"
    - "spread" in title → "spread"
    - "both teams to score" or "btts" in title → "btts"
    - otherwise → "other"
    """
    t = title.lower()
    if "win on" in t or "win the" in t:
        return "win"
    if "draw" in t:
        return "draw"
    if "o/u" in t or "over/under" in t:
        return "ou"
    if "spread" in t:
        return "spread"
    if "both teams" in t or "btts" in t:
        return "btts"
    return "other"


def detect_league(title: str, slug: str = "") -> str:
    """Detect league from title/slug. Returns short league key."""
    combined = f"{title} {slug}".lower()
    if "nba" in combined or "basketball" in combined:
        return "nba"
    if "nfl" in combined:
        return "nfl"
    if "nhl" in combined or "hockey" in combined:
        return "nhl"
    if "mlb" in combined or "baseball" in combined:
        return "mlb"
    if any(kw in combined for kw in ["epl", "premier league"]):
        return "epl"
    if any(kw in combined for kw in ["champions league", "ucl"]):
        return "ucl"
    if "bundesliga" in combined:
        return "bundesliga"
    if "serie a" in combined:
        return "serie-a"
    if "la liga" in combined:
        return "la-liga"
    if "mls" in combined:
        return "mls"
    if any(kw in combined for kw in ["soccer", "fc ", " fc"]):
        return "soccer-other"
    if any(kw in combined for kw in ["tennis", "atp", "wta"]):
        return "tennis"
    if any(kw in combined for kw in ["mma", "ufc"]):
        return "mma"
    if any(kw in combined for kw in ["esport", "valorant", "csgo", "lol", "league of legends"]):
        return "esports"
    if "ncaa" in combined or "college" in combined:
        return "ncaa"
    return "other"


def check_both_sides(positions: list[dict]) -> float:
    """Compute both-sides ratio (spread farmer detection)."""
    by_cid = defaultdict(set)
    for p in positions:
        size = p.get("size", 0)
        if isinstance(size, str):
            try:
                size = float(size)
            except (ValueError, TypeError):
                size = 0
        if size and size > 0:
            cid = p.get("conditionId", "")
            outcome = p.get("outcome", "")
            if cid:
                by_cid[cid].add(outcome)
    if not by_cid:
        return 0.0
    both_sides = sum(1 for outcomes in by_cid.values() if len(outcomes) > 1)
    return both_sides / len(by_cid)


async def fetch_leaderboard(
    client: httpx.AsyncClient,
    category: str,
    period: str,
    order_by: str,
    limit: int = 100,
) -> list[dict]:
    """Fetch leaderboard entries (paginated, 50 per batch)."""
    all_entries = []
    for offset in range(0, limit, 50):
        batch_limit = min(50, limit - offset)
        try:
            resp = await client.get(
                f"{DATA_API}/v1/leaderboard",
                params={
                    "category": category,
                    "timePeriod": period,
                    "orderBy": order_by,
                    "limit": batch_limit,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            all_entries.extend(data)
            if len(data) < batch_limit:
                break
        except Exception as e:
            log.warning(f"leaderboard {category}/{period}/{order_by} offset={offset}: {e}")
            break
        await asyncio.sleep(0.5)
    return all_entries


async def fetch_closed_positions(
    client: httpx.AsyncClient,
    address: str,
    max_pages: int = 20,
) -> list[dict]:
    """Fetch closed positions (paginated, 50 per batch). sortBy=TIMESTAMP."""
    all_closed = []
    for page in range(max_pages):
        try:
            resp = await client.get(
                f"{DATA_API}/closed-positions",
                params={
                    "user": address,
                    "limit": 50,
                    "offset": page * 50,
                    "sortBy": "TIMESTAMP",
                },
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            all_closed.extend(batch)
            if len(batch) < 50:
                break
        except Exception as e:
            log.warning(f"closed positions page {page} for {address[:10]}: {e}")
            break
        await asyncio.sleep(0.3)
    return all_closed


async def fetch_positions(
    client: httpx.AsyncClient,
    address: str,
    paginate: bool = True,
    max_pages: int = 40,  # Cap at 20K positions — whales can have millions
) -> list[dict]:
    """Fetch current open positions. Paginates by default to catch all resolved losers."""
    all_positions = []
    offset = 0
    limit = 500
    pages = 0
    while True:
        try:
            resp = await client.get(
                f"{DATA_API}/positions",
                params={"user": address, "limit": limit, "offset": offset, "sizeThreshold": 0},
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            all_positions.extend(data)
            pages += 1
            if not paginate or len(data) < limit or pages >= max_pages:
                break
            offset += limit
        except Exception as e:
            log.warning(f"positions for {address[:10]} offset={offset}: {e}")
            break
        await asyncio.sleep(0.3)
    return all_positions


def true_pnl(closed: list[dict], open_positions: list[dict]) -> dict:
    """
    Compute true PnL corrected for survivorship bias.

    PM closed-positions only shows winners for resolved markets.
    Losers stay in open positions with curPrice=0 (or near 0).
    We include those as losses.
    """
    # PnL from closed positions (resolved winners)
    closed_pnl = 0.0
    closed_invested = 0.0
    closed_wins = 0
    closed_losses = 0

    for p in closed:
        pnl = float(p.get("realizedPnl", 0) or 0)
        bought = float(p.get("totalBought", 0) or 0)
        price = float(p.get("avgPrice", 0) or 0)
        cost = bought * price
        closed_pnl += pnl
        closed_invested += cost
        if pnl > 0:
            closed_wins += 1
        else:
            closed_losses += 1

    # Losses from open positions (resolved losers: curPrice == 0, size > 0)
    # curPrice must be exactly 0 (or <0.005) = resolved market.
    # curPrice 0.01-0.02 = still-open market with low odds, NOT a loss yet.
    open_losses_pnl = 0.0
    open_losses_invested = 0.0
    open_losses_count = 0
    live_positions = 0

    for p in open_positions:
        size = float(p.get("size", 0) or 0)
        cur_price = float(p.get("curPrice", 0) or 0)
        avg_price = float(p.get("avgPrice", 0) or 0)

        if size <= 0:
            continue

        # Resolved loser: price is effectively 0 (market settled against us)
        if cur_price < 0.005 and avg_price > 0.05:
            cost = size * avg_price
            open_losses_pnl -= cost  # Total loss
            open_losses_invested += cost
            open_losses_count += 1
        else:
            live_positions += 1

    total_pnl = closed_pnl + open_losses_pnl
    total_invested = closed_invested + open_losses_invested
    total_wins = closed_wins
    total_losses = closed_losses + open_losses_count
    total_count = total_wins + total_losses

    return {
        "total_pnl": round(total_pnl, 2),
        "total_invested": round(total_invested, 2),
        "roi": round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0,
        "win_rate": round(total_wins / total_count, 4) if total_count > 0 else 0,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_count": total_count,
        "closed_pnl": round(closed_pnl, 2),
        "open_losses_pnl": round(open_losses_pnl, 2),
        "open_losses_count": open_losses_count,
        "live_positions": live_positions,
    }


def resolved_losers_as_closed(open_positions: list[dict]) -> list[dict]:
    """
    Convert open positions that are resolved losers (curPrice < 0.005) into
    pseudo-closed position dicts compatible with group_by_event/classify_game.

    This fixes survivorship bias in per-league breakdowns: without this,
    only winners appear in closed-positions, inflating per-league WR.
    """
    pseudo = []
    for p in open_positions:
        size = float(p.get("size", 0) or 0)
        cur_price = float(p.get("curPrice", 0) or 0)
        avg_price = float(p.get("avgPrice", 0) or 0)

        if size <= 0 or cur_price >= 0.005 or avg_price <= 0.05:
            continue

        cost = size * avg_price
        pseudo.append({
            # Preserve fields needed by group_by_event, classify_game, is_sport, detect_league
            "title": p.get("title", ""),
            "slug": p.get("slug", ""),
            "eventSlug": p.get("eventSlug", ""),
            "conditionId": p.get("conditionId", ""),
            "outcome": p.get("outcome", ""),
            "totalBought": size,
            "avgPrice": avg_price,
            "realizedPnl": -cost,  # Total loss
            "_source": "open_resolved_loser",
        })
    return pseudo


def send_telegram(message: str, level: str = "INFO") -> bool:
    """
    Send a Telegram message. Returns True on success.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
    Silently returns False if not configured.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.debug("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
        return False

    prefix = {"CRITICAL": "\u26a0\ufe0f CRITICAL", "WARNING": "\u26a0 WARNING", "INFO": "\u2139\ufe0f"}.get(level, "")
    text = f"{prefix} {message}" if prefix else message

    # Escape Markdown special chars in non-formatting parts would be fragile;
    # use HTML parse mode instead — only our code generates the messages.
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Convert Markdown bold (*text*) to HTML (<b>text</b>) for safe sending
    import re
    html_text = re.sub(r'\*([^*]+)\*', r'<b>\1</b>', text)
    html_text = re.sub(r'`([^`]+)`', r'<code>\1</code>', html_text)
    html_text = re.sub(r'```(.*?)```', r'<pre>\1</pre>', html_text, flags=re.DOTALL)
    html_text = re.sub(r'_([^_]+)_', r'<i>\1</i>', html_text)
    payload = json.dumps({"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def group_by_event(positions: list[dict]) -> dict[str, list[dict]]:
    """Group positions by eventSlug."""
    games = defaultdict(list)
    for p in positions:
        slug = p.get("eventSlug") or p.get("slug") or ""
        if slug:
            games[slug].append(p)
    return dict(games)


def classify_game(positions: list[dict]) -> dict:
    """Classify a game from its positions. Returns game dict with legs, costs, PnL."""
    outcomes = {}
    for p in positions:
        outcome = p.get("outcome") or p.get("title") or "?"
        bought = float(p.get("totalBought", 0) or 0)
        price = float(p.get("avgPrice", 0) or 0)
        pnl = float(p.get("realizedPnl", 0) or 0)
        cost = bought * price

        if outcome not in outcomes:
            outcomes[outcome] = {"outcome": outcome, "shares": 0, "cost": 0, "pnl": 0, "prices": []}
        outcomes[outcome]["shares"] += bought
        outcomes[outcome]["cost"] += cost
        outcomes[outcome]["pnl"] += pnl
        outcomes[outcome]["prices"].append(price)

    legs = sorted(outcomes.values(), key=lambda x: x["cost"], reverse=True)
    total_cost = sum(l["cost"] for l in legs)
    total_pnl = sum(l["pnl"] for l in legs)

    slug = positions[0].get("eventSlug") or positions[0].get("slug") or ""
    title = positions[0].get("title") or ""
    league = detect_league(title, slug)

    return {
        "slug": slug,
        "title": title,
        "league": league,
        "n_legs": len(legs),
        "legs": legs,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "game_won": total_pnl > 0,
    }
