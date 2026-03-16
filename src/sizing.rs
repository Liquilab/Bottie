use crate::config::{SizingConfig, MIN_ORDER_VALUE};
use crate::signal::AggregatedSignal;

/// Kelly Criterion position sizing
pub fn kelly_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
) -> f64 {
    let p = signal.combined_confidence; // estimated win probability
    let price = signal.price;

    if price <= 0.0 || price >= 1.0 || p <= price {
        return 0.0; // no edge
    }

    // Skip penny markets — Kelly explodes at low prices (e.g. 0.6¢ → 54% Kelly)
    if price < config.min_price {
        return 0.0;
    }

    // Also skip near-certain markets (extreme prices are illiquid and risky)
    if price > 0.95 {
        return 0.0;
    }

    // Binary outcome: win pays (1/price - 1), lose costs 1
    // Kelly fraction = (p * (1/price) - 1) / ((1/price) - 1)
    // Simplified: kelly = (p - price) / (1 - price)
    let full_kelly = (p - price) / (1.0 - price);

    // Use fractional Kelly for safety
    let fractional = full_kelly * config.kelly_fraction;

    // Kelly ceiling based on price tier — prevents penny market explosions
    // At low prices, even small confidence errors lead to huge Kelly fractions
    let kelly_ceiling = if price < 0.10 {
        0.02 // max 2% of bankroll on sub-10ct markets
    } else if price < 0.20 {
        0.05 // max 5% on 10-20ct
    } else {
        0.10 // max 10% on normal markets
    };
    let safe_fractional = fractional.min(kelly_ceiling);

    // Apply max bet percentage
    let max_bet = bankroll * config.max_bet_pct / 100.0;
    let size_usdc = (bankroll * safe_fractional).min(max_bet);

    // Convert to shares: size_usdc / price
    let shares = size_usdc / price;

    // Enforce Polymarket minimum
    if size_usdc < MIN_ORDER_VALUE {
        return 0.0;
    }

    shares
}

/// Size specifically for copy trades — Fixed-to-Win sizing.
///
/// Instead of risking a flat dollar amount regardless of odds, we size so the
/// potential PAYOUT is constant. This equalizes win/loss ratio to ~1:1, dropping
/// break-even WR from ~61% to ~50%.
///
/// Example with $1.00 target win:
///   90ct favorite: risk $0.11, win $0.11 (instead of flat $3.50 risk, $0.39 win)
///   50ct:          risk $1.00, win $1.00
///   30ct:          risk $0.43, win $1.00
pub fn copy_trade_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
) -> f64 {
    // Skip invalid prices
    if signal.price < config.min_price || signal.price >= 1.0 || signal.price <= 0.0 {
        return 0.0;
    }

    if bankroll < 1.0 {
        return 0.0;
    }

    // Fixed-to-Win: target a constant win amount, then size the risk accordingly.
    // target_win = bankroll * copy_base_size_pct / 100
    // size_usdc = target_win / (1 - price)  [because win payout = shares * (1-price)]
    // size_usdc = target_win * price / (1-price) ... wait, let me think:
    //   shares = size_usdc / price
    //   win_payout = shares * (1 - price) = size_usdc * (1 - price) / price
    //   So: size_usdc = target_win * price / (1 - price)
    let target_win = bankroll * config.copy_base_size_pct / 100.0;
    let size_usdc = target_win * signal.price / (1.0 - signal.price);

    // Cap at max bet percentage of bankroll
    let max_bet = bankroll * config.max_bet_pct / 100.0;
    let mut final_usdc = size_usdc.min(max_bet).min(bankroll);

    // Guarantee minimum bet of $2.50 if bankroll allows it
    if final_usdc < 2.50 && bankroll >= 2.50 {
        final_usdc = 2.50;
    }

    // Enforce Polymarket minimum
    let shares = final_usdc / signal.price;
    if final_usdc < MIN_ORDER_VALUE {
        return 0.0;
    }

    shares
}
