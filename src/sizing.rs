use crate::config::SizingConfig;
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

    // Enforce minimums
    if size_usdc < 1.0 {
        return 0.0;
    }

    shares
}

/// Size specifically for copy trades — we trust the wallet, not Kelly
pub fn copy_trade_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
) -> f64 {
    // Skip penny markets and invalid prices
    if signal.price < config.min_price || signal.price >= 1.0 || signal.price <= 0.0 {
        return 0.0;
    }

    if bankroll < 3.5 {
        return 0.0;
    }

    // Base size: percentage of bankroll, but always at least $3.50 per trade
    let base_usdc = (bankroll * config.copy_base_size_pct / 100.0).max(3.5);

    // Cap at max bet, but never below $3.50 minimum
    let max_bet = (bankroll * config.max_bet_pct / 100.0).max(3.5);
    let final_usdc = base_usdc.min(max_bet).min(bankroll);

    // Convert to shares
    final_usdc / signal.price
}
