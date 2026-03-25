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

    // Tiered sizing: scale with Cannae's game total as confidence signal.
    // Data (173 resolved bets, Jan-Mar 2026):
    //   Q1 <$68:    70% WR, +89% ROI
    //   Q2 $68-480: 86% WR, +72% ROI
    //   Q3 $480-1879: 91% WR, +80% ROI
    //   Q4 >$1879:  93% WR, +57% ROI
    let pct = if cannae_game_total_usdc < 100.0 {
        0.010 // 1.0% — low confidence
    } else if cannae_game_total_usdc < 500.0 {
        0.020 // 2.0% — base
    } else if cannae_game_total_usdc < 2000.0 {
        0.030 // 3.0% — high confidence
    } else {
        0.040 // 4.0% — very high confidence (93% WR)
    };
    let leg_budget = bankroll * pct;
    let our_shares = leg_budget / price;
    let our_usdc = leg_budget;

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
