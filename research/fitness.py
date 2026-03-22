"""Composite fitness scoring for wallet portfolios.

Uses OUR trade data (trades.jsonl) as primary signal, not the unreliable
closed-positions API. API data is fallback only for wallets we haven't traded yet.
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from portfolio import Portfolio


def load_our_wallet_performance(trades_path: str = "data/trades.jsonl") -> dict:
    """Compute per-wallet WR, PnL, and EV from OUR actual trades.

    Returns {address: {"wr": float, "pnl": float, "n": int, "ev": float}}
    """
    p = Path(trades_path)
    if not p.exists():
        return {}

    by_wallet = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for line in p.read_text().strip().splitlines():
        try:
            t = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not t.get("filled") or t.get("dry_run"):
            continue
        if t.get("result") not in ("win", "loss"):
            continue
        # Skip crypto up/down
        title = (t.get("market_title") or "").lower()
        if "up or down" in title:
            continue

        addr = (t.get("copy_wallet") or "").lower()
        if not addr:
            continue

        pnl = t.get("pnl", 0) or 0
        if t["result"] == "win":
            by_wallet[addr]["wins"] += 1
        else:
            by_wallet[addr]["losses"] += 1
        by_wallet[addr]["pnl"] += pnl

    result = {}
    for addr, stats in by_wallet.items():
        n = stats["wins"] + stats["losses"]
        if n > 0:
            result[addr] = {
                "wr": stats["wins"] / n,
                "pnl": stats["pnl"],
                "n": n,
                "ev": stats["pnl"] / n,
            }
    return result


def composite_fitness(portfolio: Portfolio, dag_history: list[dict] = None) -> float:
    """Score a portfolio on a 0-100 scale.

    Components:
    - Win rate (20 pts): weighted avg WR — uses OUR trades if available
    - Sharpe (15 pts): weighted avg sharpe (scout data, less weight)
    - Our PnL (20 pts): per-wallet PnL from our actual trades
    - Consistency (10 pts): low variance in per-wallet quality
    - Sport diversity (10 pts): different market categories
    - Crisis resilience (10 pts): no single catastrophically bad wallet
    - Parsimony (10 pts): fewer high-quality wallets beat many mediocre ones
    - DAG bonus (-5 to +5)
    """
    if not portfolio.wallets:
        return 0.0

    wallets = portfolio.wallets
    total_weight = sum(w.weight for w in wallets) or 1.0

    # Load our actual trade performance
    our_perf = load_our_wallet_performance()

    # 1. Win rate score (0-20) — prefer OUR data over scout API
    weighted_wr = 0.0
    for w in wallets:
        our = our_perf.get(w.address, {})
        if our.get("n", 0) >= 10:
            # Use our actual WR (trusted)
            wr = our["wr"]
        elif our.get("n", 0) >= 3:
            # Blend: 60% our data, 40% scout
            wr = our["wr"] * 0.6 + w.win_rate * 0.4
        else:
            # No our data, use scout (untrusted, capped)
            wr = min(w.win_rate, 0.80)  # cap to avoid phantom 100% WR
        weighted_wr += wr * w.weight
    weighted_wr /= total_weight
    wr_score = min(weighted_wr, 1.0) * 20

    # 2. Sharpe score (0-15) — scout data, less weight than before
    weighted_sharpe = sum(w.sharpe * w.weight for w in wallets) / total_weight
    sharpe_score = min(max(weighted_sharpe, 0), 2.0) / 2.0 * 15

    # 3. Our PnL score (0-20) — NEW: based on actual trade results
    pnl_score = 0.0
    wallets_with_data = 0
    for w in wallets:
        our = our_perf.get(w.address, {})
        if our.get("n", 0) >= 5:
            wallets_with_data += 1
            ev = our["ev"]
            # +$0.50/trade EV = full score, -$1.00/trade = zero
            wallet_pnl_score = max(0, min(1.0, (ev + 1.0) / 1.5))
            pnl_score += wallet_pnl_score * w.weight
    if wallets_with_data > 0:
        pnl_score = (pnl_score / total_weight) * 20
    else:
        pnl_score = 10.0  # neutral if no data yet

    # 4. Consistency score (0-10) — low variance in per-wallet scores
    if len(wallets) > 1:
        scores = [w.score for w in wallets]
        mean_score = statistics.mean(scores) or 1
        cv = statistics.stdev(scores) / mean_score if mean_score > 0 else 0
        consistency_score = max(0, 10 * (1 - cv))
    else:
        consistency_score = 5.0

    # 5. Sport diversity (0-10)
    high_sport = sum(1 for w in wallets if w.sport_pct > 0.5)
    low_sport = len(wallets) - high_sport
    if high_sport > 0 and low_sport > 0:
        diversity_score = 10.0
    elif high_sport > 0:
        diversity_score = 7.0
    else:
        diversity_score = 3.0

    # 6. Crisis resilience (0-10) — worst wallet shouldn't be terrible
    if wallets:
        # Use our PnL data if available, otherwise scout score
        worst_metric = float("inf")
        for w in wallets:
            our = our_perf.get(w.address, {})
            if our.get("n", 0) >= 5:
                metric = our["ev"] * 10 + 50  # scale to ~0-100
            else:
                metric = w.score
            worst_metric = min(worst_metric, metric)

        if worst_metric >= 50:
            resilience_score = 10.0
        elif worst_metric >= 20:
            resilience_score = 5.0 + (worst_metric - 20) / 30 * 5
        else:
            resilience_score = max(0, worst_metric / 20 * 5)
    else:
        resilience_score = 0.0

    # 7. Parsimony bonus (0-10) — sweet spot is 6-10 wallets
    n = len(wallets)
    if n < 4:
        parsimony_score = 0
    elif 6 <= n <= 10:
        parsimony_score = 10
    elif n <= 15:
        parsimony_score = 5
    else:
        parsimony_score = 2

    # 8. DAG bonus/penalty (-5 to +5)
    dag_bonus = 0.0
    if dag_history:
        wallet_addrs = {w.address for w in wallets}
        for decision in dag_history[-50:]:
            addr = (decision.get("wallet_address") or "").lower()
            if addr in wallet_addrs:
                outcome = decision.get("outcome_pnl")
                if outcome is not None:
                    if outcome > 0:
                        dag_bonus += 0.5
                    elif outcome < -5:
                        dag_bonus -= 1.0
        dag_bonus = max(-5, min(5, dag_bonus))

    total = wr_score + sharpe_score + pnl_score + consistency_score + diversity_score + resilience_score + parsimony_score + dag_bonus
    return round(max(0, min(100, total)), 2)
