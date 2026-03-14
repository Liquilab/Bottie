"""
Autoresearch agent — manages the watchlist based on wallet performance.

Runs every 2 hours. Reads the scout report, evaluates current wallets,
and updates the watchlist: add winners, remove losers, adjust weights.

This is a COPY TRADING bot. The only thing that matters is:
which wallets to follow and how much to trust them.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from data_loader import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("autoresearch")

SCOUT_REPORT_PATH = "data/scout_report.json"
CONFIG_PATH = "config.yaml"


def load_scout_report() -> dict:
    """Load the latest scout report."""
    p = Path(SCOUT_REPORT_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        log.error(f"failed to load scout report: {e}")
        return {}


def save_config(config: dict):
    """Save config.yaml preserving structure."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


async def research_cycle():
    """Run one research cycle: evaluate wallets, update watchlist."""
    log.info("=== RESEARCH CYCLE START ===")

    config = load_config(CONFIG_PATH)
    watchlist = config.get("copy_trading", {}).get("watchlist", [])
    current_addresses = {w["address"].lower(): w for w in watchlist}

    log.info(f"current watchlist: {len(watchlist)} wallets")

    # 1. LOAD SCOUT REPORT
    report = load_scout_report()
    if not report:
        log.warning("no scout report found — skipping cycle")
        log.info("=== RESEARCH CYCLE COMPLETE ===")
        return

    report_age_hours = 0
    if report.get("timestamp"):
        try:
            report_time = datetime.fromisoformat(report["timestamp"])
            report_age_hours = (datetime.now(timezone.utc) - report_time).total_seconds() / 3600
        except Exception:
            pass

    log.info(f"scout report: {report.get('candidates_evaluated', 0)} candidates evaluated, {report_age_hours:.1f}h old")

    # 2. EVALUATE CURRENT WALLETS
    current_scores = report.get("current_wallet_scores", [])
    changes_made = False

    for wallet_eval in current_scores:
        addr = wallet_eval.get("address", "").lower()
        name = wallet_eval.get("name", addr[:10])
        score = wallet_eval.get("score", 0)
        win_rate = wallet_eval.get("win_rate", 0)
        sharpe = wallet_eval.get("sharpe", 0)
        closed = wallet_eval.get("closed_positions", 0)

        if addr not in current_addresses:
            continue

        current_entry = current_addresses[addr]
        current_weight = current_entry.get("weight", 1.0)

        # Adjust weight based on performance
        if closed >= 20:
            if score >= 80:
                new_weight = 1.0
            elif score >= 60:
                new_weight = 0.7
            elif score >= 40:
                new_weight = 0.4
            elif score > 0:
                new_weight = 0.2
            else:
                new_weight = 0.05  # observation mode
        elif closed >= 10:
            if win_rate >= 0.70:
                new_weight = 0.6
            elif win_rate >= 0.55:
                new_weight = 0.3
            else:
                new_weight = 0.1
        else:
            continue  # not enough data to judge

        # Only change if significantly different
        if abs(new_weight - current_weight) >= 0.1:
            log.info(
                f"  ADJUST: {name:20s} weight {current_weight:.2f} → {new_weight:.2f} "
                f"(score={score:.0f} WR={win_rate:.0%} sharpe={sharpe:.2f})"
            )
            current_entry["weight"] = round(new_weight, 2)
            changes_made = True

    # 3. CHECK FOR WALLETS TO REMOVE (score 0 with enough data)
    underperformers = report.get("underperformers", [])
    for up in underperformers:
        addr = up.get("address", "").lower()
        name = up.get("name", addr[:10])
        if addr in current_addresses:
            current_weight = current_addresses[addr].get("weight", 1.0)
            if current_weight > 0.05:
                log.info(f"  DEMOTE: {name:20s} → observation mode (score=0)")
                current_addresses[addr]["weight"] = 0.05
                changes_made = True

    # 4. ADD TOP NEW CANDIDATES FROM SCOUT
    additions = report.get("recommended_additions", [])
    added = 0
    for candidate in additions:
        addr = candidate.get("address", "").lower()
        name = candidate.get("name", addr[:10])
        score = candidate.get("score", 0)
        win_rate = candidate.get("win_rate", 0)
        sharpe = candidate.get("sharpe", 0)
        closed = candidate.get("closed_positions", 0)

        if addr in current_addresses:
            continue
        if score < 50:
            continue
        if closed < 20:
            continue

        # Start new wallets at moderate weight
        if score >= 80:
            start_weight = 0.6
        elif score >= 60:
            start_weight = 0.4
        else:
            start_weight = 0.2

        new_entry = {
            "address": addr,
            "name": name,
            "weight": start_weight,
            "sports": ["all"],
        }
        watchlist.append(new_entry)
        current_addresses[addr] = new_entry
        added += 1
        changes_made = True
        log.info(
            f"  ADD: {name:20s} weight={start_weight} "
            f"(score={score:.0f} WR={win_rate:.0%} sharpe={sharpe:.2f} closed={closed})"
        )

        if added >= 3:
            break  # max 3 new wallets per cycle

    # 5. SAVE IF CHANGED
    if changes_made:
        config["copy_trading"]["watchlist"] = watchlist
        save_config(config)
        log.info(f"config.yaml updated — {len(watchlist)} wallets on watchlist")
    else:
        log.info("no changes needed this cycle")

    log.info("=== RESEARCH CYCLE COMPLETE ===")


async def main():
    """Main loop: run research cycle every 2 hours."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        await research_cycle()
        return

    while True:
        try:
            await research_cycle()
        except Exception as e:
            log.error(f"research cycle failed: {e}")

        config = load_config(CONFIG_PATH)
        interval = config.get("autoresearch", {}).get("interval_hours", 2)
        log.info(f"sleeping {interval} hours until next cycle...")
        await asyncio.sleep(interval * 3600)


if __name__ == "__main__":
    asyncio.run(main())
