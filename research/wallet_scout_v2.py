#!/usr/bin/env python3
"""
Wallet Scout v2 — Market-First Discovery

Strategy: Start from markets resolving within 48h → find who's betting →
evaluate their track record with survivorship-corrected stats.

Output: data/scout_v2_report.json

Usage:
    cd /opt/bottie/research
    python3 wallet_scout_v2.py
"""

import asyncio
import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from lib.pm_api import (
    fetch_positions,
    fetch_closed_positions,
    true_pnl,
    resolved_losers_as_closed,
    is_sport,
    detect_market_type,
    detect_league,
    check_both_sides,
    group_by_event,
    DATA_API,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scout_v2")

GAMMA_API = "https://gamma-api.polymarket.com"
OUTPUT = Path("data/scout_v2_report.json")

# Filters
MIN_RESOLVED_TRADES = 30
MIN_ROI_PCT = 0.0
MAX_BOTH_SIDES = 0.15
MAX_STALE_DAYS = 7
MIN_RECENT_TRADES = 5
MIN_MEDIAN_COST = 30
MAX_MEDIAN_COST = 10_000
POSITION_SIZE_THRESHOLD = 10


async def fetch_upcoming_markets(client: httpx.AsyncClient) -> list[dict]:
    """Fetch active SPORT markets resolving within 48 hours from Gamma API."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=48)

    SPORT_TAGS = [
        "nba", "nhl", "nfl", "mlb", "ncaa", "cbb", "soccer", "epl",
        "bundesliga", "la-liga", "serie-a", "ligue-1", "ucl", "mls",
        "mma", "ufc", "tennis", "atp", "wta",
    ]

    all_markets = []

    for tag in SPORT_TAGS:
        offset = 0
        limit = 100
        while True:
            try:
                resp = await client.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "closed": "false",
                        "active": "true",
                        "tag": tag,
                        "end_date_max": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                markets = resp.json()
                if not isinstance(markets, list) or not markets:
                    break
                all_markets.extend(markets)
                log.info(f"  [{tag}] Fetched {len(markets)} markets (offset={offset})")
                if len(markets) < limit:
                    break
                offset += limit
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"Failed to fetch {tag} markets offset={offset}: {e}")
                break
        await asyncio.sleep(0.3)

    # Dedup by conditionId
    seen_cids = set()
    deduped = []
    for m in all_markets:
        cid = m.get("conditionId") or m.get("condition_id") or ""
        if cid and cid not in seen_cids:
            seen_cids.add(cid)
            deduped.append(m)

    filtered = []
    for m in deduped:
        cid = m.get("conditionId") or m.get("condition_id") or ""
        volume = float(m.get("volumeNum", 0) or m.get("volume", 0) or 0)
        if cid and volume >= 1000:
            filtered.append(m)

    # Cap at top 200 by volume to limit API calls
    filtered.sort(key=lambda m: float(m.get("volumeNum", 0) or m.get("volume", 0) or 0), reverse=True)
    capped = filtered[:200]
    log.info(f"Step 1: {len(all_markets)} sport markets ({len(deduped)} unique), {len(filtered)} with volume >$1K, capped to {len(capped)}")
    return capped


async def find_bettors_on_market(client: httpx.AsyncClient, condition_id: str) -> list[str]:
    """Fetch traders on a market via trades endpoint (positions endpoint doesn't support conditionId filter)."""
    wallets = set()
    try:
        # Fetch recent trades on this market — both makers and takers
        for offset in [0, 100]:
            resp = await client.get(
                f"{DATA_API}/trades",
                params={
                    "conditionId": condition_id,
                    "limit": 100,
                    "offset": offset,
                },
            )
            if resp.status_code == 429:
                await asyncio.sleep(2)
                continue
            resp.raise_for_status()
            trades = resp.json()
            if not isinstance(trades, list) or not trades:
                break
            for t in trades:
                addr = t.get("proxyWallet") or ""
                if addr and len(addr) == 42:
                    wallets.add(addr.lower())
            if len(trades) < 100:
                break
            await asyncio.sleep(0.1)
    except Exception as e:
        log.debug(f"Failed to fetch traders for {condition_id[:12]}: {e}")
    return list(wallets)


async def discover_wallets(client: httpx.AsyncClient, markets: list[dict], batch_size: int = 5) -> dict[str, int]:
    """For each market, find who's betting. Returns wallet → market_count."""
    wallet_counts: dict[str, int] = defaultdict(int)

    for i in range(0, len(markets), batch_size):
        batch = markets[i:i + batch_size]
        tasks = []
        for m in batch:
            cid = m.get("conditionId") or m.get("condition_id") or ""
            if cid:
                tasks.append(find_bettors_on_market(client, cid))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                for wallet in result:
                    wallet_counts[wallet] += 1

        log.info(f"  Markets {i+1}-{i+len(batch)} / {len(markets)}, unique wallets: {len(wallet_counts)}")
        await asyncio.sleep(1.0)

    log.info(f"Step 2: {len(wallet_counts)} unique wallets across {len(markets)} markets")
    return dict(wallet_counts)


async def evaluate_wallet(client: httpx.AsyncClient, address: str) -> dict | None:
    """Full wallet evaluation with survivorship-corrected stats."""
    open_pos = await fetch_positions(client, address, paginate=True, max_pages=20)
    await asyncio.sleep(0.3)
    closed_pos = await fetch_closed_positions(client, address, max_pages=20)

    if not open_pos and not closed_pos:
        return None

    closed_sport = [p for p in closed_pos if is_sport(p)]
    open_sport = [p for p in open_pos if is_sport(p)]

    stats = true_pnl(closed_sport, open_sport)

    total_resolved = stats["total_wins"] + stats["total_losses"]
    if total_resolved < MIN_RESOLVED_TRADES:
        return None
    if stats["roi"] < MIN_ROI_PCT:
        return None
    if stats["win_rate"] < 0.40:
        return None

    both_sides = check_both_sides(open_pos)
    if both_sides > MAX_BOTH_SIDES:
        return None

    # Activity + recency
    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    recent_count = 0
    last_trade_ts = None
    for p in closed_pos:
        ts_str = p.get("resolvedAt") or p.get("endDate") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if last_trade_ts is None or ts > last_trade_ts:
                last_trade_ts = ts
            if ts > cutoff_7d:
                recent_count += 1
        except (ValueError, TypeError):
            continue

    if recent_count < MIN_RECENT_TRADES:
        return None
    if last_trade_ts and (now - last_trade_ts).days > MAX_STALE_DAYS:
        return None

    # Median bet size
    costs = []
    for p in closed_sport:
        bought = float(p.get("totalBought", 0) or 0)
        price = float(p.get("avgPrice", 0) or 0)
        cost = bought * price
        if cost > 0:
            costs.append(cost)
    if not costs:
        return None
    median_cost = statistics.median(costs)
    if median_cost < MIN_MEDIAN_COST or median_cost > MAX_MEDIAN_COST:
        return None

    # Per-league and per-market-type breakdown (survivorship corrected)
    pseudo_closed = resolved_losers_as_closed(open_sport)
    all_closed = closed_sport + pseudo_closed

    league_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "invested": 0.0})
    mt_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "invested": 0.0})

    for p in all_closed:
        title = p.get("title", "")
        slug = p.get("eventSlug", "") or p.get("slug", "")
        league = detect_league(title, slug)
        mt = detect_market_type(title)
        pnl = float(p.get("realizedPnl", 0) or 0)
        bought = float(p.get("totalBought", 0) or 0)
        price = float(p.get("avgPrice", 0) or 0)
        cost = bought * price
        won = pnl > 0

        for bucket, key in [(league_stats, league), (mt_stats, mt)]:
            bucket[key]["pnl"] += pnl
            bucket[key]["invested"] += cost
            if won:
                bucket[key]["wins"] += 1
            else:
                bucket[key]["losses"] += 1

    def summarize(stats_dict):
        result = {}
        for key, s in stats_dict.items():
            total = s["wins"] + s["losses"]
            if total >= 5:
                roi = (s["pnl"] / s["invested"] * 100) if s["invested"] > 0 else 0
                wr = s["wins"] / total if total > 0 else 0
                result[key] = {
                    "total": total, "wins": s["wins"], "losses": s["losses"],
                    "win_rate": round(wr, 3), "roi_pct": round(roi, 1), "pnl": round(s["pnl"], 2),
                }
        return result

    return {
        "address": address,
        "total_resolved": total_resolved,
        "wins": stats["total_wins"],
        "losses": stats["total_losses"],
        "win_rate": round(stats["win_rate"], 3),
        "roi_pct": round(stats["roi"], 1),
        "total_pnl": round(stats["total_pnl"], 2),
        "open_losses": stats.get("open_losses_count", 0),
        "both_sides_ratio": round(both_sides, 3),
        "recent_7d_trades": recent_count,
        "median_cost": round(median_cost, 2),
        "sport_positions": len(closed_sport),
        "last_trade": last_trade_ts.isoformat() if last_trade_ts else None,
        "league_breakdown": summarize(league_stats),
        "market_type_breakdown": summarize(mt_stats),
    }


async def run_scout():
    log.info("=" * 60)
    log.info("WALLET SCOUT V2 — Market-First Discovery")
    log.info("=" * 60)

    # Load watchlist to exclude
    watchlist_addrs = set()
    try:
        import yaml
        with open("/opt/bottie/config.yaml") as f:
            cfg = yaml.safe_load(f)
        for w in cfg.get("copy_trading", {}).get("watchlist", []):
            watchlist_addrs.add(w["address"].lower())
    except Exception:
        pass
    # Our funder
    watchlist_addrs.add("0x9f23f6d5d18f9fc5aef42efec8f63a7db3db6d15")

    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "Bottie/2.0"}) as client:

        log.info("\n--- Step 1: Fetch markets resolving within 48h ---")
        markets = await fetch_upcoming_markets(client)
        if not markets:
            log.warning("No markets found")
            return

        log.info("\n--- Step 2: Discover wallets ---")
        wallet_counts = await discover_wallets(client, markets)

        candidates = {a: c for a, c in wallet_counts.items() if a not in watchlist_addrs}
        log.info(f"After excluding watchlist: {len(candidates)}")

        # Pre-filter: active on 2+ markets
        active = {a: c for a, c in candidates.items() if c >= 2}
        log.info(f"Active on 2+ markets: {len(active)}")

        MAX_EVALUATE = 300
        if len(active) > MAX_EVALUATE:
            active = dict(sorted(active.items(), key=lambda x: -x[1])[:MAX_EVALUATE])
            log.info(f"Capped at {MAX_EVALUATE}")

        log.info(f"\n--- Step 3: Evaluate {len(active)} wallets ---")
        results = []
        for i, (addr, count) in enumerate(active.items()):
            if (i + 1) % 20 == 0:
                log.info(f"  {i+1}/{len(active)}...")
            result = await evaluate_wallet(client, addr)
            if result:
                result["markets_discovered_on"] = count
                results.append(result)
            await asyncio.sleep(0.3)

        results.sort(key=lambda x: x["roi_pct"], reverse=True)
        top = results[:20]

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "markets_scanned": len(markets),
            "wallets_discovered": len(wallet_counts),
            "wallets_evaluated": len(active),
            "wallets_passed": len(results),
            "top_candidates": top,
        }

        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, indent=2))
        log.info(f"\nSaved to {OUTPUT}")

        log.info(f"\n{'='*80}")
        log.info(f"TOP {len(top)} (by corrected ROI)")
        log.info(f"{'='*80}")
        for i, w in enumerate(top, 1):
            a = w["address"][:6] + ".." + w["address"][-4:]
            leagues = ", ".join(
                f"{l}({s['roi_pct']:+.0f}%)"
                for l, s in sorted(w["league_breakdown"].items(), key=lambda x: -x[1]["total"])[:3]
            )
            log.info(
                f"{i:2d}. {a} ROI={w['roi_pct']:+5.1f}% WR={w['win_rate']*100:4.0f}% "
                f"trades={w['total_resolved']:>4} PnL=${w['total_pnl']:>9,.0f} "
                f"med=${w['median_cost']:>5.0f} | {leagues}"
            )

        log.info(f"\n{len(markets)} markets → {len(wallet_counts)} wallets → {len(results)} passed → top {len(top)}")


if __name__ == "__main__":
    asyncio.run(run_scout())
