"""
Consensus Pipeline — Data Preparation (immutable, Karpathy-style)

Downloads leaderboard top 100 SPORTS wallets + their closed/open positions.
Saves everything to data/consensus_bulk.json for score.py to consume.

Usage:
    python research/consensus/prepare.py
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("consensus_prepare")

DATA_API = "https://data-api.polymarket.com"
OUTPUT_PATH = Path("data/consensus_bulk.json")

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


def detect_market_type(title: str) -> str:
    """Detect market type from title: moneyline, spread, total, prop, other."""
    t = title.lower()
    if "spread" in t or "handicap" in t:
        return "spread"
    if "o/u " in t or "over/under" in t or "total" in t:
        return "total"
    if "both teams" in t or "btts" in t:
        return "btts"
    if "win on" in t or "will " in t and " win" in t:
        return "moneyline"
    if "draw" in t:
        return "draw"
    if "map " in t and "winner" in t:
        return "esports_map"
    if "best of" in t or "winner" in t:
        return "match_winner"
    return "other"


def is_sport(position: dict) -> bool:
    title = (position.get("title") or "").lower()
    slug = (position.get("slug") or position.get("eventSlug") or "").lower()
    if any(slug.startswith(p) for p in SPORT_SLUG_PREFIXES):
        return True
    return any(kw in title for kw in SPORT_KEYWORDS)


def detect_domain(title: str, slug: str = "") -> str:
    """Detect domain: sports, finance, politics, entertainment, crypto, etc."""
    combined = f"{title} {slug}".lower()
    # Sports
    if "nba" in combined or "basketball" in combined:
        return "nba"
    if "nfl" in combined:
        return "nfl"
    if "nhl" in combined or "hockey" in combined:
        return "nhl"
    if "mlb" in combined or "baseball" in combined:
        return "mlb"
    if any(kw in combined for kw in ["soccer", "epl", "premier league", "champions league", "ucl", "serie a", "bundesliga", "la liga", "fc ", " fc"]):
        return "soccer"
    if any(kw in combined for kw in ["tennis", "atp", "wta", "bnp paribas"]):
        return "tennis"
    if any(kw in combined for kw in ["mma", "ufc"]):
        return "mma"
    if any(kw in combined for kw in ["esport", "dota", "cs2", "csgo", "valorant", "league of legends", "counter-strike"]):
        return "esports"
    if "ncaa" in combined or "college" in combined:
        return "ncaa"
    # Finance/Economics
    if any(kw in combined for kw in ["fed ", "interest rate", "cpi", "inflation", "gdp", "unemployment", "fomc", "treasury", "yield"]):
        return "economics"
    if any(kw in combined for kw in ["stock", "s&p", "nasdaq", "dow jones", "market cap", "ipo", "earnings"]):
        return "stocks"
    if any(kw in combined for kw in ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana"]):
        return "crypto"
    # Politics
    if any(kw in combined for kw in ["president", "election", "vote", "senate", "congress", "governor", "trump", "biden", "political"]):
        return "politics"
    # Entertainment/Culture
    if any(kw in combined for kw in ["oscar", "academy award", "grammy", "emmy", "box office", "movie", "film"]):
        return "entertainment"
    if any(kw in combined for kw in ["weather", "temperature", "hurricane"]):
        return "weather"
    return "other"


# Keep backward compat alias
def detect_sport(title: str, slug: str = "") -> str:
    return detect_domain(title, slug)


async def fetch_leaderboard(client: httpx.AsyncClient, category: str, period: str, order_by: str, limit: int = 100) -> list[dict]:
    """Fetch leaderboard page."""
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


async def fetch_closed_positions(client: httpx.AsyncClient, address: str, max_pages: int = 20) -> list[dict]:
    """Fetch closed positions (paginated)."""
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


async def fetch_positions(client: httpx.AsyncClient, address: str) -> list[dict]:
    """Fetch current open positions."""
    try:
        resp = await client.get(
            f"{DATA_API}/positions",
            params={"user": address, "limit": 500, "sizeThreshold": 0},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"positions for {address[:10]}: {e}")
        return []


def check_both_sides(positions: list[dict]) -> float:
    """Compute both-sides ratio (spread farmer detection)."""
    by_cid = defaultdict(set)
    for p in positions:
        size = p.get("size", 0)
        if isinstance(size, str):
            try:
                size = float(size)
            except ValueError:
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


def is_recent(position: dict, days: int = 7) -> bool:
    """Check if a closed position resolved within the last N days."""
    ts = position.get("resolvedAt") or position.get("endDate") or ""
    if not ts:
        return True  # Keep if no timestamp (can't filter)
    try:
        resolved = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - resolved).days <= days
    except Exception:
        return True


async def prepare():
    """Main data download pipeline."""
    log.info("=" * 60)
    log.info("CONSENSUS PREPARE — Bulk wallet data download")
    log.info("=" * 60)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "Bottie/1.0"},
    ) as client:

        # Step 1: Fetch leaderboard — DAY period for fresh signal sources
        # DAY captures currently active wallets (consensus needs live traders)
        log.info("Step 1: Fetching leaderboards (DAY + WEEK)...")
        candidates = []
        categories = ["SPORTS", "OVERALL"]
        for category in categories:
            for period in ["DAY", "WEEK"]:
                for order_by in ["PNL", "VOL"]:
                    lb = await fetch_leaderboard(client, category, period, order_by, limit=100)
                    log.info(f"  {category}/{period}/{order_by}: {len(lb)} entries")
                    candidates.extend(lb)
                    await asyncio.sleep(0.5)

        # Deduplicate by address
        seen = set()
        unique = []
        for c in candidates:
            addr = (c.get("proxyWallet") or "").lower()
            if addr and addr not in seen:
                seen.add(addr)
                unique.append(c)

        log.info(f"Unique candidate wallets: {len(unique)}")

        # Step 2: Evaluate each wallet
        log.info("Step 2: Downloading positions for each wallet...")
        wallets = []
        for i, c in enumerate(unique):
            addr = (c.get("proxyWallet") or "").lower()
            name = c.get("userName") or addr[:10]
            lb_pnl = c.get("pnl", 0) or 0
            lb_volume = c.get("vol", 0) or 0
            rank = c.get("rank", "?")

            # Fetch data
            closed_raw = await fetch_closed_positions(client, addr)
            positions = await fetch_positions(client, addr)

            # Filter to recent
            closed = [p for p in closed_raw if is_recent(p, days=7)]

            # Classify each closed position across multiple dimensions
            sport_count = 0
            position_sports = defaultdict(int)
            events = {}  # eventSlug -> rich metadata for multi-dimensional consensus

            for p in closed:
                pnl = float(p.get("realizedPnl", 0) or 0)
                title = p.get("title") or ""
                slug = p.get("slug") or p.get("eventSlug") or ""
                event_slug = p.get("eventSlug") or slug
                outcome = p.get("outcome") or ""
                sport = detect_sport(title, slug)
                avg_price = float(p.get("avgPrice", 0) or 0)
                condition_id = p.get("conditionId") or ""

                if is_sport(p):
                    sport_count += 1
                position_sports[sport] += 1

                # Detect market type from title
                market_type = detect_market_type(title)

                # Price tier
                if avg_price >= 0.70:
                    price_tier = "favorite"
                elif avg_price >= 0.50:
                    price_tier = "mid"
                elif avg_price >= 0.30:
                    price_tier = "underdog"
                else:
                    price_tier = "longshot"

                if event_slug:
                    events[event_slug] = {
                        "outcome": outcome,
                        "won": pnl > 0,
                        "pnl": pnl,
                        "sport": sport,
                        "title": title[:80],
                        "market_type": market_type,
                        "price_tier": price_tier,
                        "avg_price": round(avg_price, 3),
                        "condition_id": condition_id,
                    }

            # Both-sides check
            both_sides = check_both_sides(positions)

            # Win rate
            pnls = [float(p.get("realizedPnl", 0) or 0) for p in closed]
            wins = sum(1 for pnl in pnls if pnl > 0)
            wr = wins / len(closed) if closed else 0

            # Sharpe
            import statistics
            if len(pnls) > 1:
                avg_pnl = sum(pnls) / len(pnls)
                std_pnl = statistics.stdev(pnls)
                sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0
            else:
                sharpe = 0

            # Last activity
            last_activity_days = 999
            for p in closed:
                ts = p.get("resolvedAt") or p.get("endDate") or ""
                if ts:
                    try:
                        resolved = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        days_ago = (datetime.now(timezone.utc) - resolved).days
                        last_activity_days = min(last_activity_days, days_ago)
                    except Exception:
                        pass

            # Sport concentration (Herfindahl Index)
            total_sports = sum(position_sports.values())
            hhi = sum((c / total_sports) ** 2 for c in position_sports.values()) if total_sports > 0 else 0

            # Top sport
            top_sport = max(position_sports, key=position_sports.get) if position_sports else "unknown"

            sport_pct = sport_count / len(closed) if closed else 0

            wallet_data = {
                "address": addr,
                "name": name,
                "rank": rank,
                "lb_pnl": lb_pnl,
                "lb_volume": lb_volume,
                "closed_count": len(closed),
                "closed_count_all": len(closed_raw),
                "active_positions": len([p for p in positions if float(p.get("size", 0) or 0) > 0]),
                "win_rate": round(wr, 4),
                "sharpe": round(sharpe, 3),
                "sport_pct": round(sport_pct, 2),
                "both_sides_ratio": round(both_sides, 3),
                "last_activity_days": last_activity_days,
                "hhi": round(hhi, 3),
                "top_sport": top_sport,
                "sport_breakdown": dict(position_sports),
                "events": events,  # eventSlug -> {outcome, won, pnl, sport, title}
            }

            wallets.append(wallet_data)

            # Check for crypto up/down trades
            crypto_updown_count = sum(
                1 for e in events.values()
                if "up or down" in (e.get("title", "") or "").lower()
            )
            crypto_updown_pct = crypto_updown_count / len(closed) if closed else 0

            wallet_data["crypto_updown_pct"] = round(crypto_updown_pct, 2)

            # Progress
            status = "OK"
            if both_sides > 0.15:
                status = "SPREAD_FARMER"
            elif len(closed) < 10:
                status = "LOW_DATA"
            elif last_activity_days > 2:
                status = "STALE"
            elif wr < 0.50:
                status = "LOW_WR"
            elif crypto_updown_pct > 0.50:
                status = "CRYPTO_UPDOWN"
            elif sport_pct < 0.30:
                status = "NON_SPORT"

            log.info(
                f"  [{i+1}/{len(unique)}] {name:20s} | "
                f"{len(closed):3d} closed | WR={wr:.0%} | "
                f"sharpe={sharpe:.2f} | sport={sport_pct:.0%} | "
                f"bs={both_sides:.0%} | hhi={hhi:.2f} | "
                f"{top_sport:8s} | [{status}]"
            )

            await asyncio.sleep(0.5)

        # Step 3: Save
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "wallet_count": len(wallets),
            "wallets": wallets,
        }
        OUTPUT_PATH.write_text(json.dumps(output, indent=2))
        log.info(f"\nSaved {len(wallets)} wallets to {OUTPUT_PATH}")

        # Quick stats — relaxed filters for broader signal source pool
        valid = [w for w in wallets
                 if w["closed_count"] >= 10
                 and w["both_sides_ratio"] <= 0.15
                 and w["last_activity_days"] <= 2
                 and w["sport_pct"] >= 0.30
                 and w["win_rate"] >= 0.50]
        log.info(f"Valid candidates (after filters): {len(valid)}")

        # Show crypto up/down filtered wallets
        crypto_updown = [w for w in wallets
                         if any("up or down" in (e.get("title", "") or "").lower()
                                for e in w.get("events", {}).values())]
        if crypto_updown:
            log.info(f"Wallets with crypto up/down trades: {len(crypto_updown)} (excluded from quality pool)")

        log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(prepare())
