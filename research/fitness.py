"""Composite fitness scoring for wallet portfolios."""

import statistics
from portfolio import Portfolio


def composite_fitness(portfolio: Portfolio, dag_history: list[dict] = None) -> float:
    """Score a portfolio on a 0-100 scale.

    Components:
    - Win rate (25 pts): weighted avg WR across wallets
    - Sharpe (25 pts): weighted avg sharpe
    - Consistency (15 pts): low variance in per-wallet quality
    - Sport diversity (10 pts): different market categories
    - Crisis resilience (10 pts): no single catastrophically bad wallet
    - Parsimony (15 pts): fewer high-quality wallets beat many mediocre ones
    """
    if not portfolio.wallets:
        return 0.0

    wallets = portfolio.wallets
    total_weight = sum(w.weight for w in wallets) or 1.0

    # 1. Win rate score (0-25)
    weighted_wr = sum(w.win_rate * w.weight for w in wallets) / total_weight
    wr_score = min(weighted_wr, 1.0) * 25

    # 2. Sharpe score (0-25)
    weighted_sharpe = sum(w.sharpe * w.weight for w in wallets) / total_weight
    sharpe_score = min(max(weighted_sharpe, 0), 2.0) / 2.0 * 25

    # 3. Consistency score (0-15) — low variance in per-wallet scores
    if len(wallets) > 1:
        scores = [w.score for w in wallets]
        cv = statistics.stdev(scores) / (statistics.mean(scores) or 1)
        consistency_score = max(0, 15 * (1 - cv))
    else:
        consistency_score = 7.5  # neutral for single wallet

    # 4. Sport diversity (0-10)
    high_sport = sum(1 for w in wallets if w.sport_pct > 0.5)
    low_sport = len(wallets) - high_sport
    if high_sport > 0 and low_sport > 0:
        diversity_score = 10.0  # mix of sport and non-sport
    elif high_sport > 0:
        diversity_score = 7.0  # all sport is OK
    else:
        diversity_score = 3.0  # no sport focus is risky

    # 5. Crisis resilience (0-10) — worst wallet shouldn't be terrible
    if wallets:
        worst_score = min(w.score for w in wallets)
        if worst_score >= 50:
            resilience_score = 10.0
        elif worst_score >= 20:
            resilience_score = 5.0 + (worst_score - 20) / 30 * 5
        else:
            resilience_score = worst_score / 20 * 5
    else:
        resilience_score = 0.0

    # 6. Parsimony bonus (0-5) — sweet spot is 6-10 wallets
    n = len(wallets)
    if n < 4:
        parsimony_score = 0  # too few is risky
    elif 6 <= n <= 10:
        parsimony_score = 5  # sweet spot
    elif n <= 15:
        parsimony_score = 3
    else:
        parsimony_score = 1  # too many dilutes quality

    # 7. DAG bonus/penalty (-5 to +5)
    dag_bonus = 0.0
    if dag_history:
        wallet_addrs = {w.address for w in wallets}
        for decision in dag_history[-50:]:  # last 50 decisions
            addr = (decision.get("wallet_address") or "").lower()
            if addr in wallet_addrs:
                outcome = decision.get("outcome_pnl")
                if outcome is not None:
                    if outcome > 0:
                        dag_bonus += 0.5
                    elif outcome < -5:
                        dag_bonus -= 1.0
        dag_bonus = max(-5, min(5, dag_bonus))

    total = wr_score + sharpe_score + consistency_score + diversity_score + resilience_score + parsimony_score + dag_bonus
    return round(max(0, min(100, total)), 2)
