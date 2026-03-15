"""Backtest hypotheses against historical data by simulating the full sizing policy."""

import pandas as pd


def composite_score(result: dict, config_changes: dict = None) -> float:
    """Fix #4: Composite fitness score — Sharpe-first, drawdown-penalized, complexity-penalized.

    Components:
    - Sharpe ratio (50% weight): risk-adjusted return
    - Win rate (50% weight): consistency
    - Drawdown penalty: heavy penalty for max drawdown (×2)
    - Complexity penalty: 2% per changed parameter (parsimony)
    """
    sharpe = result.get("sharpe", 0)
    win_rate = result.get("win_rate", 0)
    max_drawdown = abs(result.get("max_drawdown", 0))
    trades = result.get("trades", 0)

    if trades < 10:
        return 0.0

    # Small sample penalty: linearly penalize if fewer than 50 trades
    sample_adj = min(1.0, trades / 50.0)

    # Drawdown penalty (relative to bankroll)
    try:
        from autoresearch import get_bankroll
        bankroll_ref = max(25.0, get_bankroll())
    except Exception:
        bankroll_ref = 200.0
    dd_penalty = (max_drawdown / bankroll_ref) * 2.0

    # Complexity penalty: 2% per parameter changed
    n_params = len(config_changes.keys()) if config_changes else 0
    complexity_penalty = n_params * 0.02

    fitness = ((sharpe * 0.5) + (win_rate * 0.5)) * sample_adj - dd_penalty - complexity_penalty

    return round(fitness, 4)


def backtest(trades: pd.DataFrame, config_changes: dict) -> dict:
    """
    Backtest a hypothesis by simulating config changes against historical trades.
    Simulates the actual Rust sizing logic (Kelly + copy trade sizing) to compute
    realistic PnL impact of parameter changes.

    Returns metrics: win_rate, roi, sharpe, max_drawdown, roi_improvement, fitness
    """
    if trades.empty or len(trades) < 10:
        return {
            "trades": 0,
            "win_rate": 0,
            "roi": 0,
            "sharpe": 0,
            "max_drawdown": 0,
            "roi_improvement": 0,
            "fitness": 0,
            "error": "insufficient data",
        }

    resolved = trades[trades["result"].isin(["win", "loss"])].copy()
    if len(resolved) < 10:
        return {
            "trades": len(resolved),
            "win_rate": 0,
            "roi": 0,
            "sharpe": 0,
            "max_drawdown": 0,
            "roi_improvement": 0,
            "fitness": 0,
            "error": "insufficient resolved trades",
        }

    # Baseline metrics (actual historical performance)
    baseline_pnl = resolved["pnl"].sum() if "pnl" in resolved.columns else 0
    baseline_invested = resolved["size_usdc"].sum() if "size_usdc" in resolved.columns else 1
    baseline_roi = baseline_pnl / baseline_invested if baseline_invested > 0 else 0

    # --- Simulate with proposed config changes ---
    # Default parameters (matching Rust defaults)
    kelly_fraction = config_changes.get("kelly_fraction", 0.25)
    copy_base_pct = config_changes.get("copy_base_size_pct", 3.0)
    min_consensus = config_changes.get("min_consensus", 1)
    max_delay_ms = config_changes.get("max_delay_seconds", 300) * 1000
    min_edge_pct = config_changes.get("min_edge_pct", 0.0)
    min_price = config_changes.get("min_price", 0.05)
    max_price = config_changes.get("max_price", 1.0)
    max_open_bets = config_changes.get("max_open_bets", 200)
    max_resolution_days = config_changes.get("max_resolution_days", 7)
    wallet_weights = config_changes.get("wallet_weights", {})
    sport_multipliers = config_changes.get("sport_multipliers", {})

    # Use real bankroll if available, otherwise default
    try:
        from autoresearch import get_bankroll
        real_bankroll = get_bankroll()
    except Exception:
        real_bankroll = 200.0
    sim_bankroll = max(25.0, real_bankroll)  # floor at $25 to avoid division issues
    sim_pnl = 0.0
    sim_invested = 0.0
    sim_wins = 0
    sim_trades = 0
    pnl_series = []
    fee_bps = config_changes.get("fee_bps", 200)  # default 2% taker fee (200 bps)

    for _, row in resolved.iterrows():
        price = row.get("price", 0)
        confidence = row.get("confidence", 0.55)
        result = row.get("result", "")
        consensus = row.get("consensus_count", 1) or 1
        delay_ms = row.get("signal_delay_ms", 0) or 0
        wallet = row.get("copy_wallet", "")
        sport = row.get("sport", "")
        edge = row.get("edge_pct", 0) or 0

        # --- Apply filters ---
        # Consensus filter
        if consensus < min_consensus:
            continue

        # Delay filter
        if max_delay_ms < 300000 and delay_ms > max_delay_ms:
            continue

        # Edge filter (for arb signals)
        if min_edge_pct > 0 and edge < min_edge_pct:
            continue

        # --- Apply weight/multiplier adjustments ---
        # Wallet weight override
        w_weight = 1.0
        if wallet_weights and isinstance(wallet_weights, dict):
            if isinstance(wallet, str) and wallet:
                w_weight = wallet_weights.get(wallet, 1.0)
                # Weight 0 = skip this wallet
                if w_weight <= 0:
                    continue

        # Sport multiplier
        s_mult = 1.0
        if sport_multipliers and isinstance(sport_multipliers, dict):
            if isinstance(sport, str) and sport:
                s_mult = sport_multipliers.get(sport, 1.0)
                if s_mult <= 0:
                    continue

        # --- Simulate sizing (mirrors Rust copy_trade_size logic) ---
        # Skip invalid prices
        if price <= 0 or price >= 1.0:
            continue

        # Price filters
        if price < min_price:
            continue
        if price > max_price:
            continue

        # Copy trade base sizing — we trust the wallet, no Kelly edge required
        base_usdc = sim_bankroll * (copy_base_pct / 100.0) * w_weight

        # Cap at max bet (5% of bankroll)
        max_bet_pct = config_changes.get("max_bet_pct", 5.0)
        sim_size = min(base_usdc, sim_bankroll * max_bet_pct / 100.0)
        if sim_size < 1.0:
            continue

        # Deduct taker fee from trade size (realistic simulation)
        fee = sim_size * (fee_bps / 10000.0)
        sim_size_after_fee = sim_size - fee

        sim_invested += sim_size
        sim_trades += 1

        if result == "win":
            shares = sim_size_after_fee / price
            trade_pnl = shares * (1.0 - price) - fee  # win payout minus entry fee
            sim_pnl += trade_pnl
            sim_wins += 1
        else:
            trade_pnl = -sim_size  # lose full investment (fee already included)
            sim_pnl += trade_pnl

        pnl_series.append(trade_pnl)

    if sim_trades < 10:
        return {
            "trades": sim_trades,
            "win_rate": 0,
            "roi": 0,
            "sharpe": 0,
            "max_drawdown": 0,
            "roi_improvement": 0,
            "fitness": 0,
            "error": "too few trades after simulation",
        }

    win_rate = sim_wins / sim_trades
    roi = sim_pnl / sim_invested if sim_invested > 0 else 0

    # Sharpe ratio
    if len(pnl_series) > 1:
        returns = pd.Series(pnl_series)
        sharpe = float(returns.mean() / returns.std()) if returns.std() > 0 else 0
    else:
        sharpe = 0

    # Max drawdown
    if pnl_series:
        cumulative = pd.Series(pnl_series).cumsum()
        running_max = cumulative.cummax()
        max_drawdown = float((cumulative - running_max).min())
    else:
        max_drawdown = 0

    roi_improvement = (roi - baseline_roi) / abs(baseline_roi) * 100 if baseline_roi != 0 else 0

    result_dict = {
        "trades": sim_trades,
        "win_rate": float(win_rate),
        "roi": float(roi),
        "sharpe": float(sharpe),
        "max_drawdown": float(max_drawdown),
        "roi_improvement": float(roi_improvement),
    }

    # Add fitness score
    result_dict["fitness"] = composite_score(result_dict, config_changes)

    return result_dict
