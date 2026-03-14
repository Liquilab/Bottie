"""
Evolutionary autoresearch — optimizes the wallet portfolio through Darwinian selection.

Inspired by distributed AGI evolutionary systems. Runs every 2 hours:
1. Load scout report + research DAG + playbook
2. Determine mode (conservative/normal/aggressive) based on bankroll
3. Generate 30 mutations of the current wallet portfolio
4. Score each with composite fitness (WR, sharpe, consistency, parsimony, resilience)
5. Best mutation survives if it beats current by 2+ points
6. Every 3rd cycle: LLM curator distills patterns into playbook

The ONLY thing this changes is config.yaml's watchlist. Never touches
sizing, kelly, delays, or any other bot parameter.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from curator import curate_playbook, load_playbook, save_playbook
from dag import append_decision, diff_portfolios, load_dag, update_outcomes
from fitness import composite_fitness
from portfolio import (
    Portfolio,
    available_candidates,
    generate_mutations,
    portfolio_from_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("autoresearch")

CONFIG_PATH = "config.yaml"
SCOUT_REPORT_PATH = "data/scout_report.json"

# Pinned wallets — autoresearch must NEVER remove these
PINNED_WALLETS = {
    "0x17559efac103ac7f361be37ec0b93888d4c55aac",  # moisturizer — stocks, inactive on weekends
}

cycle_count = 0


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save_config(config: dict, path: str = CONFIG_PATH):
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def load_scout_report() -> dict:
    p = Path(SCOUT_REPORT_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        log.error(f"failed to load scout report: {e}")
        return {}


def get_bankroll() -> float:
    """Get current bankroll from bot's last status in trades.jsonl."""
    trades_path = Path("data/trades.jsonl")
    if not trades_path.exists():
        return 30.0  # default

    # Read last line for most recent state
    lines = trades_path.read_text().strip().splitlines()
    if not lines:
        return 30.0

    # Count resolved PnL (rough estimate — UI is the real source)
    total_pnl = 0
    for line in lines:
        try:
            t = json.loads(line)
            if t.get("filled") and not t.get("dry_run") and t.get("pnl") is not None:
                total_pnl += t["pnl"]
        except (json.JSONDecodeError, KeyError):
            continue

    # Start capital was ~$250, but we can't know deposits. Use a safe estimate.
    return max(1.0, 250 + total_pnl)


def determine_mode(bankroll: float) -> str:
    if bankroll < 25:
        return "conservative"
    elif bankroll > 50:
        return "aggressive"
    return "normal"


def apply_portfolio(portfolio: Portfolio, config: dict):
    """Write the new watchlist to config.yaml."""
    watchlist = []
    for w in portfolio.wallets:
        watchlist.append({
            "address": w.address,
            "name": w.name,
            "weight": round(w.weight, 2),
            "sports": ["all"],
        })
    config["copy_trading"]["watchlist"] = watchlist
    save_config(config)


async def evolution_cycle():
    """Run one evolutionary cycle."""
    global cycle_count
    cycle_count += 1
    log.info(f"=== EVOLUTION CYCLE {cycle_count} START ===")

    # 1. Load everything
    config = load_config()
    scout = load_scout_report()
    dag = load_dag()
    playbook = load_playbook()
    watchlist = config.get("copy_trading", {}).get("watchlist", [])

    if not scout:
        log.warning("no scout report — skipping cycle")
        log.info("=== EVOLUTION CYCLE COMPLETE ===")
        return

    # Check scout report age
    report_age = 0
    if scout.get("timestamp"):
        try:
            report_time = datetime.fromisoformat(scout["timestamp"])
            report_age = (datetime.now(timezone.utc) - report_time).total_seconds() / 3600
        except Exception:
            pass
    if report_age > 6:
        log.warning(f"scout report is {report_age:.1f}h old — skipping cycle")
        log.info("=== EVOLUTION CYCLE COMPLETE ===")
        return

    log.info(f"watchlist: {len(watchlist)} wallets | scout: {scout.get('candidates_evaluated', 0)} candidates | DAG: {len(dag)} decisions")

    # 2. Update DAG outcomes from trade data
    dag = update_outcomes(dag)

    # 3. Build current portfolio and score it
    current = portfolio_from_config(watchlist, scout)
    current.fitness = composite_fitness(current, dag)
    log.info(f"current portfolio fitness: {current.fitness:.1f}/100 ({len(current.wallets)} wallets)")

    # 4. Determine mode
    bankroll = get_bankroll()
    mode = determine_mode(bankroll)
    log.info(f"mode: {mode} (bankroll ~${bankroll:.0f})")

    # 5. Get candidates
    current_addrs = {w.address for w in current.wallets}
    candidates = available_candidates(scout, current_addrs)
    log.info(f"available candidates: {len(candidates)}")

    # 6. Generate 30 mutations
    mutations = generate_mutations(current, candidates, mode, n=30)

    # 7. Score each mutation
    for m in mutations:
        m.fitness = composite_fitness(m, dag)

    # Sort by fitness
    mutations.sort(key=lambda m: m.fitness, reverse=True)

    # Log top 5
    for i, m in enumerate(mutations[:5]):
        log.info(f"  #{i+1} {m.mutation_type:10s} fitness={m.fitness:5.1f} wallets={len(m.wallets)}")

    # 8. Select best — must beat current by 2+ points
    best = mutations[0]
    improvement = best.fitness - current.fitness

    # Ensure pinned wallets are in every mutation
    for m in mutations:
        m_addrs = {w.address for w in m.wallets}
        for pinned_addr in PINNED_WALLETS:
            if pinned_addr not in m_addrs:
                # Find pinned wallet in current portfolio and add it
                for w in current.wallets:
                    if w.address == pinned_addr:
                        import copy as _copy
                        m.wallets.append(_copy.deepcopy(w))
                        break
        m.fitness = composite_fitness(m, dag)

    mutations.sort(key=lambda m: m.fitness, reverse=True)
    best = mutations[0]
    improvement = best.fitness - current.fitness

    # Limit how many wallets can change per cycle (max 3 add + 3 remove)
    if improvement >= 2.0:
        decisions_preview = diff_portfolios(watchlist, best.wallets, best.fitness)
        adds = sum(1 for d in decisions_preview if d["action"] == "add")
        removes = sum(1 for d in decisions_preview if d["action"] == "remove")
        if adds > 3 or removes > 3:
            log.warning(f"mutation too aggressive ({adds} adds, {removes} removes) — capping changes")
            # Fall back to best non-random mutation
            for m in mutations:
                if m.mutation_type != "random" and m.fitness > current.fitness + 2.0:
                    best = m
                    improvement = best.fitness - current.fitness
                    break
                else:
                    log.info("no moderate mutation found — keeping current portfolio")
                    improvement = 0

    if improvement >= 2.0:
        log.info(f"EVOLVING: {best.mutation_type} (fitness {current.fitness:.1f} → {best.fitness:.1f}, +{improvement:.1f})")

        # Log decisions to DAG
        decisions = diff_portfolios(watchlist, best.wallets, best.fitness)
        for d in decisions:
            d["mutation_type"] = best.mutation_type
            d["cycle"] = cycle_count
            append_decision(d)
            action = d["action"]
            name = d["wallet_name"]
            if action == "add":
                log.info(f"  ADD: {name} weight={d['new_weight']:.2f}")
            elif action == "remove":
                log.info(f"  REMOVE: {name}")
            elif action == "reweight":
                log.info(f"  REWEIGHT: {name} {d['old_weight']:.2f} → {d['new_weight']:.2f}")

        # Apply to config
        apply_portfolio(best, config)
        log.info(f"config.yaml updated — {len(best.wallets)} wallets")
    else:
        log.info(f"no improvement found (best: +{improvement:.1f}, need +2.0) — keeping current portfolio")

    # 9. Playbook curator (every 3rd cycle)
    ar_config = config.get("autoresearch", {})
    curator_interval = ar_config.get("curator_every_n_cycles", 3)
    model = ar_config.get("claude_model", "claude-sonnet-4-20250514")

    if cycle_count % curator_interval == 0:
        log.info("running playbook curator...")
        try:
            new_playbook = await curate_playbook(dag, playbook, current.fitness, model=model)
            save_playbook(new_playbook)
            log.info("playbook updated")
        except Exception as e:
            log.error(f"curator failed: {e}")

    log.info(f"=== EVOLUTION CYCLE {cycle_count} COMPLETE ===")


async def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        await evolution_cycle()
        return

    while True:
        try:
            await evolution_cycle()
        except Exception as e:
            log.error(f"evolution cycle failed: {e}")

        config = load_config()
        interval = config.get("autoresearch", {}).get("interval_hours", 2)
        log.info(f"sleeping {interval} hours until next cycle...")
        await asyncio.sleep(interval * 3600)


if __name__ == "__main__":
    asyncio.run(main())
