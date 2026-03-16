"""
Consensus Scout — discovers wallets that CREATE consensus with our existing watchlist.

Runs every 30 min. For each leaderboard wallet:
1. Has live open positions? (not just historical)
2. Good win rate? (WR >= 50%, closed >= 10)
3. Not a spread farmer? (both_sides < 15%)
4. Creates overlap with our existing wallets? (shared events)

Wallets that pass all checks get added to config.yaml with weight=0.15.
Wallets with 0 live positions get removed.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("consensus_scout")

DATA_API = "https://data-api.polymarket.com"
CONFIG_PATH = "config.yaml"

# Quality filters
MIN_CLOSED = 10
MIN_WIN_RATE = 0.50
MAX_BOTH_SIDES = 0.15
MIN_LIVE_POSITIONS = 2
MAX_WALLETS = 80  # cap total watchlist size


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(config):
    import os
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, CONFIG_PATH)


async def fetch_leaderboard(client, category, period, order_by, limit=100):
    all_entries = []
    for offset in range(0, limit, 50):
        batch_limit = min(50, limit - offset)
        try:
            resp = await client.get(
                f"{DATA_API}/v1/leaderboard",
                params={"category": category, "timePeriod": period, "orderBy": order_by,
                        "limit": batch_limit, "offset": offset},
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
        await asyncio.sleep(0.3)
    return all_entries


async def fetch_positions(client, address):
    try:
        resp = await client.get(
            f"{DATA_API}/positions",
            params={"user": address, "limit": 200, "sizeThreshold": 0},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def fetch_closed_positions(client, address, max_pages=5):
    all_closed = []
    for page in range(max_pages):
        try:
            resp = await client.get(
                f"{DATA_API}/closed-positions",
                params={"user": address, "limit": 50, "offset": page * 50, "sortBy": "TIMESTAMP"},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            all_closed.extend(batch)
            if len(batch) < 50:
                break
        except Exception:
            break
        await asyncio.sleep(0.2)
    return all_closed


def check_both_sides(positions):
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
    return sum(1 for o in by_cid.values() if len(o) > 1) / len(by_cid)


async def scout_cycle():
    log.info("=== CONSENSUS SCOUT START ===")

    config = load_config()
    watchlist = config.get("copy_trading", {}).get("watchlist", [])
    overrides = config.get("autoresearch_params", {}).get("wallet_weights_override", {})
    current_addrs = {w["address"].lower() for w in watchlist}
    active_addrs = {w["address"].lower() for w in watchlist
                    if overrides.get(w["address"].lower(), w.get("weight", 0)) > 0}

    log.info(f"Current watchlist: {len(current_addrs)} total, {len(active_addrs)} active")

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Bottie/1.0"}) as client:

        # Step 1: Get existing wallet event slugs (for overlap check)
        our_events = defaultdict(set)  # eventSlug -> set of wallet names
        for addr in list(active_addrs)[:50]:  # cap to avoid too many API calls
            name = next((w["name"] for w in watchlist if w["address"].lower() == addr), addr[:10])
            positions = await fetch_positions(client, addr)
            for p in positions:
                size = float(p.get("size", 0) or 0)
                price = float(p.get("curPrice", 0) or 0)
                if size > 0 and 0.05 < price < 0.95:
                    slug = (p.get("eventSlug") or p.get("slug") or "").lower()
                    title = (p.get("title") or "").lower()
                    if slug and "up or down" not in title:
                        our_events[slug].add(name)
            await asyncio.sleep(0.1)

        log.info(f"Our wallets cover {len(our_events)} live events")

        # Step 2: Fetch leaderboard candidates (DAY + WEEK)
        candidates = []
        for category in ["SPORTS", "OVERALL"]:
            for period in ["DAY", "WEEK"]:
                for order_by in ["PNL", "VOL"]:
                    lb = await fetch_leaderboard(client, category, period, order_by, limit=100)
                    log.info(f"  {category}/{period}/{order_by}: {len(lb)} entries")
                    candidates.extend(lb)
                    await asyncio.sleep(0.3)

        # Dedupe
        seen = set()
        unique = []
        for c in candidates:
            addr = (c.get("proxyWallet") or "").lower()
            if addr and addr not in seen and addr not in current_addrs:
                seen.add(addr)
                unique.append(c)

        log.info(f"New candidates to evaluate: {len(unique)}")

        # Step 3: Evaluate each candidate
        qualified = []
        for i, c in enumerate(unique):
            addr = (c.get("proxyWallet") or "").lower()
            name = c.get("userName") or addr[:10]

            # Check live positions
            positions = await fetch_positions(client, addr)
            live = [p for p in positions if float(p.get("size", 0) or 0) > 0
                    and 0.05 < float(p.get("curPrice", 0) or 0) < 0.95
                    and "up or down" not in (p.get("title") or "").lower()]

            if len(live) < MIN_LIVE_POSITIONS:
                await asyncio.sleep(0.1)
                continue

            # Check overlap with our events
            overlap = 0
            for p in live:
                slug = (p.get("eventSlug") or p.get("slug") or "").lower()
                if slug in our_events:
                    overlap += 1

            # Check win rate from closed positions
            closed = await fetch_closed_positions(client, addr)
            if len(closed) < MIN_CLOSED:
                await asyncio.sleep(0.1)
                continue

            pnls = [float(p.get("realizedPnl", 0) or 0) for p in closed]
            wins = sum(1 for pnl in pnls if pnl > 0)
            wr = wins / len(closed) if closed else 0

            if wr < MIN_WIN_RATE:
                await asyncio.sleep(0.1)
                continue

            # Check both-sides ratio
            bs = check_both_sides(positions)
            if bs > MAX_BOTH_SIDES:
                await asyncio.sleep(0.1)
                continue

            qualified.append({
                "address": addr,
                "name": name[:30],
                "live_count": len(live),
                "overlap": overlap,
                "win_rate": round(wr, 3),
                "closed": len(closed),
                "both_sides": round(bs, 3),
                "pnl": c.get("pnl", 0) or 0,
            })

            log.info(
                f"  [{i+1}/{len(unique)}] {name[:20]:20s} | "
                f"live={len(live):3d} overlap={overlap:2d} WR={wr:.0%} "
                f"closed={len(closed):4d} bs={bs:.0%} | QUALIFIED"
            )
            await asyncio.sleep(0.15)

            if (i + 1) % 50 == 0:
                log.info(f"  ... {i+1}/{len(unique)} evaluated")

        # Score function: overlap × WR (higher = better signal source)
        def wallet_score(overlap, wr, live_count):
            return overlap * wr * (1 + min(live_count, 20) / 20)

        for w in qualified:
            w["score"] = round(wallet_score(w["overlap"], w["win_rate"], w["live_count"]), 2)

        qualified.sort(key=lambda w: -w["score"])

        log.info(f"\nQualified new wallets: {len(qualified)}")
        for w in qualified[:10]:
            log.info(f"  {w['name']:20s} score={w['score']:5.1f} live={w['live_count']} overlap={w['overlap']} WR={w['win_rate']:.0%}")

        # Step 4: Score existing wallets + remove inactive
        removed = []
        kept_watchlist = []
        existing_scores = []  # (index_in_kept, score, name) for swap comparison

        for w in watchlist:
            addr = w["address"].lower()
            weight = overrides.get(addr, w.get("weight", 0))

            # Keep disabled wallets (weight 0) and original high-weight wallets untouched
            if weight <= 0 or weight >= 0.5:
                kept_watchlist.append(w)
                continue

            # Score active low-weight wallets (the signal sources we manage)
            positions = await fetch_positions(client, addr)
            live = [p for p in positions if float(p.get("size", 0) or 0) > 0
                    and 0.05 < float(p.get("curPrice", 0) or 0) < 0.95
                    and "up or down" not in (p.get("title") or "").lower()]

            if len(live) == 0:
                removed.append(w["name"])
                log.info(f"  REMOVE: {w['name']} (0 live positions)")
                continue

            # Count overlap with other wallets
            overlap = 0
            for p in live:
                slug = (p.get("eventSlug") or p.get("slug") or "").lower()
                if slug in our_events:
                    overlap += 1

            # Fetch WR for scoring
            closed = await fetch_closed_positions(client, addr, max_pages=3)
            wr = sum(1 for p in closed if float(p.get("realizedPnl", 0) or 0) > 0) / len(closed) if closed else 0.5

            score = wallet_score(overlap, wr, len(live))
            kept_watchlist.append(w)
            existing_scores.append((len(kept_watchlist) - 1, score, w["name"], addr))
            await asyncio.sleep(0.1)

        # Step 5: Fill empty slots first
        slots = MAX_WALLETS - len(kept_watchlist)
        added = []
        candidates_left = list(qualified)  # copy for swapping later

        if slots > 0:
            for w in candidates_left[:slots]:
                kept_watchlist.append({
                    "address": w["address"],
                    "name": w["name"],
                    "weight": 0.15,
                    "sports": ["all"],
                })
                added.append(f"{w['name']} (score={w['score']:.1f})")
                log.info(f"  ADD: {w['name']} (score={w['score']:.1f} live={w['live_count']} overlap={w['overlap']} WR={w['win_rate']:.0%})")
            candidates_left = candidates_left[slots:]

        # Step 6: Swap — replace worst existing wallets with better candidates
        MAX_SWAPS = 5
        swaps = 0

        if existing_scores and candidates_left:
            existing_scores.sort(key=lambda x: x[1])  # worst first

            for idx, score, name, addr in existing_scores:
                if swaps >= MAX_SWAPS or not candidates_left:
                    break

                best_candidate = candidates_left[0]
                if best_candidate["score"] > score * 1.5:  # must be 50% better to justify swap
                    # Swap
                    log.info(f"  SWAP: {name} (score={score:.1f}) → {best_candidate['name']} (score={best_candidate['score']:.1f})")
                    kept_watchlist[idx] = {
                        "address": best_candidate["address"],
                        "name": best_candidate["name"],
                        "weight": 0.15,
                        "sports": ["all"],
                    }
                    candidates_left.pop(0)
                    swaps += 1
                else:
                    break  # candidates are sorted, no point continuing

        # Step 7: Save config if changed
        if added or removed or swaps > 0:
            config["copy_trading"]["watchlist"] = kept_watchlist
            save_config(config)
            log.info(f"Config updated: +{len(added)} added, -{len(removed)} removed, {swaps} swaps = {len(kept_watchlist)} total")
        else:
            log.info("No changes needed")

    log.info("=== CONSENSUS SCOUT COMPLETE ===")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        await scout_cycle()
        return

    while True:
        try:
            await scout_cycle()
        except Exception as e:
            log.error(f"scout cycle failed: {e}")
        log.info("sleeping 1 hour until next cycle...")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
