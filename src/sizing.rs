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

/// Size specifically for copy trades — Tiered bet sizing based on price.
///
/// Bet = portfolio_pct × portfolio_reference_usdc
///
/// Price tiers:
///   0.30–0.45 → 1% of portfolio  (uncertain, small bet)
///   0.45–0.58 → 2% of portfolio
///   0.58–0.83 → 3% of portfolio
///   0.83–0.95 → 4% of portfolio  (high confidence, bigger bet)
///
/// portfolio_reference_usdc = configured total portfolio value (cash + positions).
/// Falls back to live bankroll if 0.
pub fn copy_trade_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
) -> f64 {
    let price = signal.price;

    // Skip out-of-range prices (min_price/max_price set the tradeable window)
    if price < config.min_price || price > config.max_price || price <= 0.0 || price >= 1.0 {
        return 0.0;
    }

    if bankroll < 1.0 {
        return 0.0;
    }

    // Reference portfolio for percentage calculation
    let portfolio = if config.portfolio_reference_usdc > 0.0 {
        config.portfolio_reference_usdc
    } else {
        bankroll
    };

    // Tiered bet percentage based on price confidence
    let pct = if price < 0.45 {
        0.01 // 1%
    } else if price < 0.58 {
        0.02 // 2%
    } else if price < 0.83 {
        0.03 // 3%
    } else {
        0.04 // 4%
    };

    let mut size_usdc = portfolio * pct;

    // Never bet more than available cash
    size_usdc = size_usdc.min(bankroll);

    // Enforce PM minimum: 5 shares
    let min_cost = 5.0 * price;
    if size_usdc < min_cost && bankroll >= min_cost {
        size_usdc = min_cost;
    }

    if size_usdc < MIN_ORDER_VALUE {
        return 0.0;
    }

    size_usdc / price
}
