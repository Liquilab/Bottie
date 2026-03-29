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


async def check_copyability(positions: list[dict]) -> float:
    """Check what fraction of positions are both-sides (arb/hedge).

    Returns both_sides_ratio (0.0 = clean directional, 1.0 = all both-sides).
    Wallets with high both_sides_ratio are arb traders whose edge is NOT copyable.
    """
    from collections import defaultdict
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

    # Win rate and PnL — survivorship corrected
    # Closed-positions only shows winners. Losses sit in open positions at curPrice=0.
    open_losers = []
    for p in positions:
        cp = float(p.get("curPrice", 0) or 0)
        avg = float(p.get("avgPrice", 0) or 0)
        sz = float(p.get("size", 0) or 0)
        if cp < 0.005 and avg > 0.05 and sz > 0:
            open_losers.append(-sz * avg)  # loss = negative PnL

    all_pnls = [float(p.get("realizedPnl", 0) or 0) for p in closed] + open_losers

    if all_pnls:
        wins = sum(1 for pnl in all_pnls if pnl > 0)
        win_rate = wins / len(all_pnls)
        total_pnl = sum(all_pnls)
        avg_pnl = total_pnl / len(all_pnls)

        if len(all_pnls) > 1:
            std_pnl = statistics.stdev(all_pnls)
            sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0
        else:
            sharpe = 0

        worst_trade = min(all_pnls)
    else:
        win_rate = 0
        total_pnl = 0
        avg_pnl = 0
        sharpe = 0
        worst_trade = 0

    # Copyability check
    both_sides_ratio = await check_copyability(positions)

    # Last activity: check most recent closed position timestamp
    last_activity_days = 999
    for p in closed:
        ts = p.get("resolvedAt") or p.get("endDate") or ""
        if ts:
            try:
                from datetime import datetime, timezone
                resolved = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - resolved).days
                last_activity_days = min(last_activity_days, days_ago)
            except Exception:
                pass

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
        "both_sides_ratio": round(both_sides_ratio, 3),
        "last_activity_days": last_activity_days,
    }


def score_wallet(eval_data: dict, leaderboard_pnl: float, leaderboard_volume: float, is_current: bool = False) -> float:
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
    last_activity = eval_data.get("last_activity_days", 999)
    if last_activity > 4:
        return 0  # Inactive for >4 days — can't copy stale wallets
    if win_rate < 0.60:
        return 0  # Too many losses for our bankroll
    if win_rate >= 0.95 and closed >= 50 and not is_current:
        return 0  # Suspiciously perfect — likely data artifact (skip for current wallets, API is unreliable for them)

    # Copyability check: penalize arb/hedge wallets
    both_sides_ratio = eval_data.get("both_sides_ratio", 0)
    if both_sides_ratio >= 0.30 and not is_current:
        return 0  # >30% both-sides = arb trader, not copyable
    # Moderate both-sides: 15-30% gets penalty applied below

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

    # Copyability penalty: 15-30% both-sides = 20% score reduction
    if both_sides_ratio >= 0.15:
        penalty = min(0.4, both_sides_ratio)  # max 40% penalty
        score *= (1.0 - penalty)

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
            eval_data["score"] = score_wallet(eval_data, pnl, volume, is_current=is_current)

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
                f"bs={eval_data.get('both_sides_ratio', 0):>4.0%} | "
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
