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

/// Size for copy trades — Proportional shares sizing.
///
/// Copies Cannae's exact shares distribution within a game, scaled to our budget.
///
/// Logic:
///   factor = game_budget / cannae_game_total_usdc
///   our_shares = cannae_shares × factor
///   our_usdc = our_shares × price
///
/// Game budget: $50 (normal) or $100 (if Cannae's game total > $100K)
///
/// Returns shares (not USDC).
pub fn copy_trade_size(
    bankroll: f64,
    signal: &AggregatedSignal,
    config: &SizingConfig,
    cannae_game_total_usdc: f64,
) -> f64 {
    let price = signal.price;

    if price <= 0.0 || price >= 1.0 || bankroll < 1.0 {
        return 0.0;
    }

    if cannae_game_total_usdc <= 0.0 || signal.source_shares <= 0.0 {
        return 0.0;
    }

    // Tiered sizing: scale with Cannae's conviction (game size).
    // Cannae bets more on games he's more confident about — follow that signal.
    //   < $15K  → 1% of bankroll (base, testing tier)
    //   $15-50K → 1.5%
    //   $50-100K → 2%
    //   > $100K → 3% (max conviction)
    let game_budget_pct = if cannae_game_total_usdc >= 100_000.0 {
        0.03
    } else if cannae_game_total_usdc >= 50_000.0 {
        0.02
    } else if cannae_game_total_usdc >= 15_000.0 {
        0.015
    } else {
        0.01
    };
    let game_budget = bankroll * game_budget_pct;

    // Factor: scale Cannae's position to our budget
    let factor = game_budget / cannae_game_total_usdc;

    // Our shares = Cannae's shares × factor
    let our_shares = signal.source_shares * factor;
    let our_usdc = our_shares * price;

    // Floor at $2.50 minimum bet
    let (final_shares, final_usdc) = if our_usdc < 2.50 {
        let min_shares = 2.50 / price;
        (min_shares, 2.50)
    } else {
        (our_shares, our_usdc)
    };

    // Never bet more than available cash
    if final_usdc > bankroll {
        return 0.0;
    }

    // Enforce PM minimum: 5 shares
    if final_shares < 5.0 {
        if bankroll >= 5.0 * price {
            return 5.0;
        }
        return 0.0;
    }

    if final_usdc < MIN_ORDER_VALUE {
        return 0.0;
    }

    final_shares
}
