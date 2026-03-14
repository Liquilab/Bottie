"""
Wallet Scout Agent — discovers and evaluates wallets for the copy trading watchlist.

Runs every hour. Fetches the Polymarket leaderboard, evaluates each wallet using
/closed-positions (win rate, PnL, sharpe) and /positions (active positions, sport focus).
Outputs recommendations to data/scout_report.json for autoresearch to consume.

Data sources (verified reliable):
- /v1/leaderboard → candidate discovery
- /positions → active position count, sport focus from title/slug
- /closed-positions → win rate, realized PnL, sharpe approximation

NOT used (unreliable):
- /activity → returns wrong data per wallet
"""

import asyncio
import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path

import httpx

from data_loader import load_config
from scraper import scrape_leaderboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wallet_scout")

DATA_API = "https://data-api.polymarket.com"
REPORT_PATH = "data/scout_report.json"

SPORT_KEYWORDS = [
    "win on", "spread", "o/u", "over/under", "both teams",
    "nba", "nfl", "nhl", "mlb", "soccer", "football", "tennis",
    "ufc", "mma", "epl", "premier league", "champions league",
    "serie a", "bundesliga", "la liga", "mls", "ncaa", "college",
    "knicks", "lakers", "celtics", "bulls", "pacers", "clippers",
    "valorant", "esports", "league of legends",
    "fc", "united", "city fc", "real madrid", "barcelona",
    "vs.", "grizzlies", "warriors", "trail blazers", "nuggets",
    "rockets", "spurs", "bucks", "heat", "nets", "hawks",
    "map 1 winner", "map 2 winner",
]

SPORT_SLUG_PREFIXES = [
    "nba-", "nfl-", "nhl-", "mlb-", "ufc-", "mma-",
    "epl-", "ucl-", "soccer-", "tennis-", "ncaa-",
    "bundesliga-", "serie-a-", "la-liga-", "mls-",
    "valorant-", "csgo-", "lol-",
]


def is_sport(position: dict) -> bool:
    """Check if a position is sport-related using title and slug."""
    title = (position.get("title") or "").lower()
    slug = (position.get("slug") or position.get("eventSlug") or "").lower()
    if any(slug.startswith(p) for p in SPORT_SLUG_PREFIXES):
        return True
    return any(kw in title for kw in SPORT_KEYWORDS)


async def fetch_positions(client: httpx.AsyncClient, address: str) -> list[dict]:
    """Fetch current positions for a wallet."""
    try:
        resp = await client.get(
            f"{DATA_API}/positions",
            params={"user": address, "limit": 500, "sizeThreshold": 0},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"failed to fetch positions for {address[:10]}: {e}")
        return []


async def fetch_closed_positions(client: httpx.AsyncClient, address: str, max_pages: int = 10) -> list[dict]:
    """Fetch all closed positions for a wallet (paginated)."""
    all_closed = []
    for page in range(max_pages):
        try:
            resp = await client.get(
                f"{DATA_API}/closed-positions",
                params={"user": address, "limit": 50, "offset": page * 50},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            all_closed.extend(batch)
        except Exception as e:
            log.warning(f"failed to fetch closed positions page {page} for {address[:10]}: {e}")
            break
    return all_closed


async def evaluate_wallet(client: httpx.AsyncClient, address: str) -> dict:
    """Full wallet evaluation using /positions and /closed-positions."""
    positions = await fetch_positions(client, address)
    closed = await fetch_closed_positions(client, address)

    active = [p for p in positions if p.get("size", 0) > 0]
    sport_active = sum(1 for p in positions if is_sport(p))
    sport_closed = sum(1 for p in closed if is_sport(p))
    total_items = len(positions) + len(closed)
    total_sport = sport_active + sport_closed
    sport_pct = total_sport / total_items if total_items > 0 else 0

    # Win rate and PnL from closed positions
    if closed:
        pnls = [p.get("realizedPnl", 0) or 0 for p in closed]
        wins = sum(1 for pnl in pnls if pnl > 0)
        win_rate = wins / len(closed)
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / len(closed)

        # Sharpe approximation: mean / stdev of per-trade PnL
        if len(pnls) > 1:
            std_pnl = statistics.stdev(pnls)
            sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0
        else:
            sharpe = 0

        worst_trade = min(pnls)
    else:
        win_rate = 0
        total_pnl = 0
        avg_pnl = 0
        sharpe = 0
        worst_trade = 0

    return {
        "total_positions": len(positions),
        "active_positions": len(active),
        "closed_positions": len(closed),
        "sport_pct": round(sport_pct, 2),
        "win_rate": round(win_rate, 4),
        "total_realized_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "sharpe": round(sharpe, 3),
        "worst_trade": round(worst_trade, 2),
    }


def score_wallet(eval_data: dict, leaderboard_pnl: float, leaderboard_volume: float) -> float:
    """Score a wallet based on metrics that predict future profitability.

    Goal: find wallets that will help grow $200 → $10,000.
    Key: high win rate + consistency (sharpe) + enough track record.
    """
    closed = eval_data.get("closed_positions", 0)
    active = eval_data.get("active_positions", 0)
    win_rate = eval_data.get("win_rate", 0)
    sharpe = eval_data.get("sharpe", 0)

    # Hard filters
    if closed < 10:
        return 0  # Not enough track record
    if active == 0:
        return 0  # Not currently trading
    if win_rate < 0.60:
        return 0  # Too many losses for our bankroll

    # Scoring (0-100 scale)
    # Win rate: most important — every loss hurts at $200 bankroll
    wr_score = min(win_rate, 1.0) * 40  # max 40

    # Sharpe: consistency matters — we want steady growth, not lucky streaks
    sharpe_score = min(max(sharpe, 0), 1.5) / 1.5 * 30  # max 30

    # Track record depth: more closed trades = more confidence
    track_score = min(closed / 200, 1.0) * 15  # max 15 (200+ trades = full score)

    # Activity: must be actively trading now
    activity_score = min(active / 50, 1.0) * 10  # max 10

    # Volume sanity: leaderboard volume should be real
    volume_score = 5 if leaderboard_volume > 50000 else 0  # max 5

    score = wr_score + sharpe_score + track_score + activity_score + volume_score

    return round(score, 2)


async def scout_cycle():
    """Run one wallet scouting cycle."""
    log.info("=== WALLET SCOUT START ===")

    config = load_config("config.yaml")
    current_watchlist = config.get("copy_trading", {}).get("watchlist", [])
    current_addresses = {w["address"].lower() for w in current_watchlist}

    log.info(f"current watchlist: {len(current_addresses)} wallets")

    # Fetch leaderboard
    candidates = []
    for category in ["SPORTS", "OVERALL"]:
        for period in ["WEEK", "MONTH"]:
            try:
                lb = await scrape_leaderboard(
                    category=category,
                    time_period=period,
                    order_by="PNL",
                    limit=50,
                )
                log.info(f"leaderboard {category}/{period}: {len(lb)} traders")
                candidates.extend(lb)
            except Exception as e:
                log.warning(f"leaderboard fetch failed for {category}/{period}: {e}")

    # Deduplicate
    seen = set()
    unique_candidates = []
    for c in candidates:
        addr = (c.get("proxyWallet") or "").lower()
        if addr and addr not in seen:
            seen.add(addr)
            unique_candidates.append(c)

    log.info(f"unique candidates: {len(unique_candidates)}")

    # Evaluate each candidate
    evaluations = []
    async with httpx.AsyncClient(timeout=30) as client:
        for c in unique_candidates:
            addr = (c.get("proxyWallet") or "").lower()
            if not addr:
                continue

            pnl = c.get("pnl", 0) or 0
            volume = c.get("vol", 0) or 0
            name = c.get("userName") or addr[:10]
            rank = c.get("rank", "?")
            is_current = addr in current_addresses

            eval_data = await evaluate_wallet(client, addr)
            eval_data["score"] = score_wallet(eval_data, pnl, volume)

            evaluations.append({
                "address": addr,
                "name": name,
                "rank": rank,
                "pnl": pnl,
                "volume": volume,
                "is_current_watchlist": is_current,
                **eval_data,
            })

            log.info(
                f"  {name:20s} | {eval_data['closed_positions']:>4d} closed | "
                f"{eval_data['win_rate']:>5.1%} WR | sharpe={eval_data['sharpe']:>5.2f} | "
                f"sport={eval_data['sport_pct']:>4.0%} | "
                f"score={eval_data['score']:>5.1f}"
            )

            await asyncio.sleep(0.3)

    # Sort by score
    evaluations.sort(key=lambda x: x["score"], reverse=True)

    new_candidates = [e for e in evaluations if not e["is_current_watchlist"] and e["score"] > 0]
    current_performance = [e for e in evaluations if e["is_current_watchlist"]]
    underperformers = [e for e in current_performance if e["score"] == 0]

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_watchlist_count": len(current_addresses),
        "candidates_evaluated": len(evaluations),
        "top_new_candidates": new_candidates[:10],
        "current_wallet_scores": sorted(current_performance, key=lambda x: x["score"], reverse=True),
        "underperformers": underperformers,
        "recommended_additions": [
            {
                "address": c["address"],
                "name": c["name"],
                "score": c["score"],
                "pnl": c["pnl"],
                "win_rate": c["win_rate"],
                "sharpe": c["sharpe"],
                "sport_pct": c["sport_pct"],
                "closed_positions": c["closed_positions"],
                "active_positions": c["active_positions"],
            }
            for c in new_candidates[:5]
        ],
        "recommended_removals": [
            {
                "address": c["address"],
                "name": c["name"],
                "score": c["score"],
                "win_rate": c["win_rate"],
                "sharpe": c["sharpe"],
                "reason": f"WR={c['win_rate']:.0%} sharpe={c['sharpe']:.2f}",
            }
            for c in underperformers
        ],
    }

    Path(REPORT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(REPORT_PATH).write_text(json.dumps(report, indent=2, default=str))

    log.info(f"scout report saved to {REPORT_PATH}")
    log.info("top 5 new candidates:")
    for c in new_candidates[:5]:
        log.info(
            f"  {c['name']:20s} | WR={c['win_rate']:.0%} | sharpe={c['sharpe']:.2f} | "
            f"pnl=${c['pnl']:.0f} | closed={c['closed_positions']} | "
            f"score={c['score']:.1f}"
        )
    if underperformers:
        log.info("underperforming current wallets:")
        for u in underperformers:
            log.info(f"  {u['name']:20s} | WR={u['win_rate']:.0%} | sharpe={u['sharpe']:.2f}")

    log.info("=== WALLET SCOUT COMPLETE ===")
    return report


async def main():
    """Run scout every hour."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        await scout_cycle()
        return

    while True:
        try:
            await scout_cycle()
        except Exception as e:
            log.error(f"scout cycle failed: {e}")

        log.info("sleeping 1 hour until next scout cycle...")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
