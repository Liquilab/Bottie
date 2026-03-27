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

/// Proportional copy-trade sizing.
///
/// Formula: bankroll × leg_weight × conviction × max_pct
///   - leg_weight = cannae_leg_usdc / cannae_game_total (how much of game Cannae puts here)
///   - conviction = best_usdc / (best_usdc + second_usdc) per conditionId
///   - max_pct = 8% (from config.max_bet_pct)
///
/// Skip if result < $2.50 (PM minimum).
/// Returns shares (not USDC).
pub fn proportional_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
    cannae_game_total_usdc: f64,
    conviction: f64,
) -> f64 {
    let price = signal.price;

    if price <= 0.0 || price >= 1.0 || bankroll < 1.0 {
        return 0.0;
    }

    if cannae_game_total_usdc <= 0.0 || signal.source_size_usdc <= 0.0 {
        return 0.0;
    }

    // leg_weight: proportion of Cannae's game total on this leg
    let leg_weight = signal.source_size_usdc / cannae_game_total_usdc;

    // max_pct from config (8% = 0.08)
    let max_pct = config.max_bet_pct / 100.0;

    // our_usdc = bankroll × leg_weight × conviction × max_pct
    let our_usdc = bankroll * leg_weight * conviction * max_pct;

    // Skip if below $2.50 minimum (not bump — skip)
    if our_usdc < 2.50 {
        return 0.0;
    }

    // Never bet more than available cash
    if our_usdc > bankroll {
        return 0.0;
    }

    let shares = our_usdc / price;

    // Enforce PM minimum: 5 shares
    if shares < 5.0 {
        if bankroll >= 5.0 * price {
            return 5.0;
        }
        return 0.0;
    }

    shares
}

/// Backward-compatible wrapper — used by non-game-context paths.
pub fn copy_trade_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
    cannae_game_total_usdc: f64,
) -> f64 {
    // Default: full conviction, treat source_size_usdc as leg weight
    proportional_size(bankroll, signal, config, cannae_game_total_usdc, 1.0)
}
