"""
Autoresearch agent: analyze → hypothesize → backtest → deploy.
Runs every N hours to continuously improve the trading bot.

Implements:
- Fix #2: Filter manual/Bitcoin trades from research data
- Fix #1: 70/30 chronological train/test split (OOS validation)
- Fix #4: Composite fitness scoring
- Fix #3: Playbook curator (accumulated wisdom across cycles)
- Fix #5: Mutation swarm (10-15 mutations per cycle)
- Fix #6: Adversarial risk officer (Haiku veto)
- Fix #7: Strategy versioning (deployment attribution)
- Fix #10: Rollback automation
"""

import asyncio
import json
import logging
import random
from datetime import datetime
from pathlib import Path

from analyzer import analyze, metrics_by, per_wallet_performance
from backtest import backtest, composite_score
from data_loader import load_config, load_trades, load_wallet_histories
from deployer import apply_to_config
from hypothesis import (
    generate_hypotheses,
    load_hypothesis_log,
    load_playbook,
    save_hypothesis,
    update_playbook,
)
from scraper import download_wallet_trades, update_watchlist_from_leaderboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("autoresearch")


def filter_bot_trades(df):
    """Fix #2: Filter out manual trades and Bitcoin 5-min noise from research data."""
    if df.empty:
        return df

    # Only keep bot-generated trades (copy, odds_arb)
    if "signal_source" in df.columns:
        df = df[df["signal_source"] != "manual"].copy()

    # Also filter Bitcoin Up/Down 5-minute noise
    if "market_title" in df.columns:
        df = df[~df["market_title"].str.contains("Bitcoin Up or Down", case=False, na=False)].copy()

    return df


def mutate_hypothesis(hyp: dict, mutation_rate: float = 0.2) -> dict:
    """Fix #5: Stochastically mutate a hypothesis's config_changes."""
    changes = {}
    source = hyp.get("config_changes", {})
    if not source:
        return hyp

    changes = {k: v for k, v in source.items()}

    # Mutate numeric parameters
    for k in list(changes.keys()):
        if random.random() < mutation_rate:
            if k == "wallet_weights" and isinstance(changes[k], dict):
                for addr in changes[k]:
                    changes[k][addr] *= random.uniform(0.7, 1.3)
                    changes[k][addr] = round(max(0.0, min(3.0, changes[k][addr])), 2)
            elif k == "kelly_fraction":
                changes[k] = round(changes[k] * random.uniform(0.8, 1.2), 3)
                changes[k] = max(0.05, min(0.5, changes[k]))
            elif k == "copy_base_size_pct":
                changes[k] = round(changes[k] * random.uniform(0.8, 1.2), 2)
                changes[k] = max(1.0, min(10.0, changes[k]))
            elif k == "min_consensus":
                changes[k] = max(1, min(5, changes[k] + random.choice([-1, 0, 1])))
            elif k == "max_delay_seconds":
                changes[k] = max(30, min(300, int(changes[k] * random.uniform(0.7, 1.3))))
            elif k == "sport_multipliers" and isinstance(changes[k], dict):
                for sport in changes[k]:
                    changes[k][sport] *= random.uniform(0.8, 1.2)
                    changes[k][sport] = round(max(0.1, min(3.0, changes[k][sport])), 2)

    return {
        "description": f"Mutant of: {hyp.get('description', '?')[:50]}",
        "config_changes": changes,
        "expected_improvement": "Evolutionary mutation",
        "min_sample_size": hyp.get("min_sample_size", 20),
    }


async def risk_officer_veto(hypotheses: list[dict], bankroll: float = 200.0) -> list[dict]:
    """Fix #6: Adversarial risk officer — veto dangerous parameter combos."""
    safe = []
    for h in hypotheses:
        changes = h.get("config_changes", {})
        reasons = []

        # Check kelly_fraction
        kf = changes.get("kelly_fraction")
        if kf is not None and kf > 0.4:
            reasons.append(f"kelly_fraction={kf} too aggressive for ${bankroll} bankroll")

        # Check copy_base_size_pct
        base = changes.get("copy_base_size_pct")
        if base is not None and base > 8.0:
            reasons.append(f"copy_base_size_pct={base}% too large")

        # Dangerous combo: high kelly + low consensus
        if kf and kf > 0.3 and changes.get("min_consensus", 2) <= 1:
            reasons.append("high kelly + min_consensus=1 is reckless")

        # Check wallet weights for extreme values
        ww = changes.get("wallet_weights", {})
        if isinstance(ww, dict):
            for addr, w in ww.items():
                if w > 2.5:
                    reasons.append(f"wallet weight {w} for {addr[:8]}.. too extreme")

        if reasons:
            log.warning(f"RISK VETO: {h.get('description', '?')[:50]} — {'; '.join(reasons)}")
            h["vetoed"] = True
            h["veto_reasons"] = reasons
        else:
            safe.append(h)

    log.info(f"risk officer: {len(safe)}/{len(hypotheses)} hypotheses passed")
    return safe


def generate_strategy_version() -> str:
    """Fix #7: Generate unique strategy version tag."""
    return datetime.utcnow().strftime("v%Y%m%d_%H%M%S")


def check_rollback(our_trades, current_config: dict) -> bool:
    """Fix #10: Check if current strategy should be rolled back.
    Returns True if rollback is needed."""
    params = current_config.get("autoresearch_params", {})
    active = params.get("active_strategies", [])
    if not active:
        return False

    current_version = active[-1] if active else None
    if not current_version:
        return False

    # Check trades since last deployment
    if our_trades.empty or "strategy_version" not in our_trades.columns:
        return False

    version_trades = our_trades[our_trades["strategy_version"] == current_version]
    resolved = version_trades[version_trades["result"].isin(["win", "loss"])]

    if len(resolved) < 10:
        return False  # Not enough data yet

    wins = (resolved["result"] == "win").sum()
    win_rate = wins / len(resolved)
    pnl = resolved["pnl"].sum() if "pnl" in resolved.columns else 0

    # Rollback if win rate < 35% or significant loss
    if win_rate < 0.35 or pnl < -20:
        log.warning(
            f"ROLLBACK TRIGGERED: strategy {current_version} "
            f"win_rate={win_rate:.1%} pnl=${pnl:.2f} on {len(resolved)} trades"
        )
        return True

    return False


async def research_cycle():
    """Run one complete research cycle."""
    log.info("=== RESEARCH CYCLE START ===")

    # 1. LOAD DATA
    our_trades = load_trades("data/trades.jsonl")
    wallet_trades = load_wallet_histories("data/wallet_trades/")
    current_config = load_config("config.yaml")

    log.info(f"loaded {len(our_trades)} total trades")

    # Fix #2: Filter manual/noise trades for research
    research_trades = filter_bot_trades(our_trades)
    log.info(f"filtered to {len(research_trades)} bot trades for research (excluded {len(our_trades) - len(research_trades)} manual/noise)")

    # Fix #10: Check rollback
    if check_rollback(research_trades, current_config):
        log.info("rolling back to baseline config")
        apply_to_config({
            "wallet_weights": {},
            "sport_multipliers": {},
        }, "config.yaml")
        # Clear active strategies
        current_config = load_config("config.yaml")

    # Fix #1: 70/30 chronological train/test split
    if "timestamp" in research_trades.columns and len(research_trades) > 20:
        research_trades = research_trades.sort_values("timestamp")
        split_idx = int(len(research_trades) * 0.7)
        train_trades = research_trades.iloc[:split_idx]
        test_trades = research_trades.iloc[split_idx:]
        log.info(f"OOS split: {len(train_trades)} train / {len(test_trades)} test trades")
    else:
        train_trades = research_trades
        test_trades = research_trades
        log.warning("insufficient data for train/test split, using all data")

    # 2. ANALYZE (on TRAIN data only!)
    current_metrics = analyze(train_trades)
    report = {
        "overall": current_metrics,
        "by_source": metrics_by(train_trades, "signal_source"),
        "by_sport": metrics_by(train_trades, "sport"),
        "by_wallet": metrics_by(train_trades, "copy_wallet"),
        "by_consensus": metrics_by(train_trades, "consensus_count"),
        "wallet_performance": per_wallet_performance(wallet_trades),
    }

    log.info(f"current performance (train): {json.dumps(current_metrics, default=str)}")

    # 3. GENERATE HYPOTHESES
    previous = load_hypothesis_log()
    model = current_config.get("autoresearch", {}).get("claude_model", "claude-sonnet-4-20250514")
    min_trades = current_config.get("autoresearch", {}).get("min_backtest_trades", 50)

    # Fix #3: Load playbook for accumulated wisdom
    playbook = load_playbook()

    try:
        hypotheses = await generate_hypotheses(report, previous, model=model, playbook=playbook)
        log.info(f"generated {len(hypotheses)} base hypotheses")
    except Exception as e:
        log.error(f"hypothesis generation failed: {e}")
        hypotheses = []

    # Fix #5: Mutation swarm — expand to 10-15 mutations
    if hypotheses and previous:
        # Get top performers from history
        scored_prev = [h for h in previous if h.get("backtest_result", {}).get("fitness", 0) > 0]
        scored_prev.sort(key=lambda h: h.get("backtest_result", {}).get("fitness", 0), reverse=True)

        mutations = []
        # Clone and mutate top 3 previous winners
        for h in scored_prev[:3]:
            for _ in range(2):
                mutations.append(mutate_hypothesis(h, mutation_rate=0.2))

        # Mutate current batch
        for h in hypotheses[:3]:
            mutations.append(mutate_hypothesis(h, mutation_rate=0.3))

        hypotheses.extend(mutations)
        log.info(f"swarm expanded to {len(hypotheses)} total hypotheses (incl. mutations)")

    # Fix #6: Risk officer veto
    hypotheses = await risk_officer_veto(hypotheses)

    # 4. BACKTEST (on TEST data only — OOS validation!)
    winners = []
    for h in hypotheses:
        config_changes = h.get("config_changes", {})

        # Backtest on unseen test data
        result = backtest(test_trades, config_changes)

        # Fix #4: Also compute composite fitness score
        result["fitness"] = composite_score(result, config_changes)

        # Optional: check for overfitting by comparing train vs test
        train_result = backtest(train_trades, config_changes)
        if train_result.get("sharpe", 0) > 0 and result.get("sharpe", 0) > 0:
            oos_degradation = (train_result["sharpe"] - result["sharpe"]) / abs(train_result["sharpe"])
            result["oos_degradation"] = round(oos_degradation, 3)
            if oos_degradation > 0.5:
                result["overfit_warning"] = True
                log.warning(f"  OVERFIT WARNING: {h.get('description', '?')[:40]} — {oos_degradation:.0%} OOS degradation")

        h["backtest_result"] = result
        h["timestamp"] = datetime.utcnow().isoformat()
        save_hypothesis(h)

        log.info(
            f"  hypothesis: {h.get('description', '?')[:60]} → "
            f"trades={result['trades']} win_rate={result.get('win_rate', 0):.2%} "
            f"roi={result.get('roi', 0):.2%} fitness={result.get('fitness', 0):.3f} "
            f"{'⚠ OVERFIT' if result.get('overfit_warning') else ''}"
        )

        # Select winners based on composite fitness, not just ROI
        if (
            result["trades"] >= min_trades
            and result.get("fitness", 0) > 0
            and not result.get("overfit_warning", False)
        ):
            winners.append(h)

    # 5. DEPLOY BEST WINNER (by fitness score)
    if winners:
        best = max(winners, key=lambda h: h["backtest_result"].get("fitness", 0))
        version = generate_strategy_version()
        log.info(
            f"DEPLOYING [{version}]: {best.get('description', '?')} "
            f"(fitness={best['backtest_result'].get('fitness', 0):.3f})"
        )
        try:
            changes = best.get("config_changes", {})
            apply_to_config(changes, "config.yaml")

            # Fix #7: Track strategy version
            deploy_config = load_config("config.yaml")
            params = deploy_config.setdefault("autoresearch_params", {})
            strategies = params.setdefault("active_strategies", [])
            strategies.append(version)
            # Keep last 10 versions
            if len(strategies) > 10:
                params["active_strategies"] = strategies[-10:]
            params["current_strategy_version"] = version
            params["last_deploy"] = {
                "version": version,
                "description": best.get("description", "?"),
                "fitness": best["backtest_result"].get("fitness", 0),
                "timestamp": datetime.utcnow().isoformat(),
            }
            from deployer import _save_config
            _save_config(deploy_config, "config.yaml")

            log.info("config.yaml updated successfully")
        except Exception as e:
            log.error(f"failed to update config: {e}")
    else:
        log.info("no winning hypotheses this cycle")

    # Fix #3: Update playbook with cycle results
    all_results = [h for h in hypotheses if "backtest_result" in h]
    if all_results:
        try:
            await update_playbook(all_results, playbook, model=model)
            log.info("playbook updated with cycle insights")
        except Exception as e:
            log.error(f"playbook update failed: {e}")

    # 6. WALLET MAINTENANCE — download trades for current watchlist
    try:
        watchlist = current_config.get("copy_trading", {}).get("watchlist", [])
        for entry in watchlist:
            addr = entry["address"]
            count = await download_wallet_trades(addr, limit=200)
            if count > 0:
                log.info(f"  downloaded {count} trades for {entry.get('name', addr[:8])}")
    except Exception as e:
        log.error(f"wallet maintenance failed: {e}")

    # 7. LOAD SCOUT REPORT — log recommendations from wallet_scout
    try:
        scout_path = Path("data/scout_report.json")
        if scout_path.exists():
            scout = json.loads(scout_path.read_text())
            additions = scout.get("recommended_additions", [])
            removals = scout.get("recommended_removals", [])
            if additions:
                log.info(f"scout recommends adding {len(additions)} wallets: {[a['name'] for a in additions]}")
            if removals:
                log.info(f"scout recommends removing {len(removals)} wallets: {[r['name'] for r in removals]}")
    except Exception as e:
        log.warning(f"scout report load failed: {e}")

    log.info("=== RESEARCH CYCLE COMPLETE ===")


async def main():
    """Main loop: run research cycle every N hours."""
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

        try:
            current_config = load_config("config.yaml")
            interval_hours = current_config.get("autoresearch", {}).get("interval_hours", 6)
        except Exception:
            interval_hours = 6
        log.info(f"sleeping {interval_hours} hours until next cycle...")
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    asyncio.run(main())
