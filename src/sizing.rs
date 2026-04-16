/// Base sizing: 5% (2026-04-16, proportional sizing).
///
/// History: confidence-based → flat 5% → flat 2.5% + CV ladder → flat 7.5% → proportional 5% base.
/// The base is multiplied by the trader's conviction ratio (their bet / their average).
/// At 1.0× conviction = 5%. At 3.0× conviction = 15% (cap).
pub fn confidence_pct(_confidence: f64) -> f64 {
    5.0
}

/// Proportional sizing: trader's conviction drives the multiplier.
///
/// cv_usdc = trader's current_value on this game
/// avg_cv = trader's average bet size on this league (from config)
///
/// multiplier = cv_usdc / avg_cv, clamped to [0.5, 3.0]
/// Result: base 5% × 0.5 = 2.5% (min) to 5% × 3.0 = 15% (max)
///
/// If avg_cv is 0 or not configured, falls back to 1.0× (= 5%).
/// Returns None if cv_usdc is 0 (no position = skip).
pub fn cannae_cv_multiplier(cv_usdc: f64, _league: &str) -> Option<f64> {
    if cv_usdc <= 0.0 { return None; }
    Some(1.0) // Default 1.0×; proportional override happens in main.rs using avg_source_usdc
}

/// Proportional multiplier: trader bet / trader average, clamped [0.5, 3.0].
pub fn proportional_multiplier(trader_bet: f64, trader_avg: f64) -> f64 {
    if trader_avg <= 0.0 {
        return 1.0;
    }
    (trader_bet / trader_avg).clamp(0.5, 3.0)
}

/// Flat sizing: bankroll × pct / price = shares.
///
/// No proportional weighting, no Kelly. pct is the ladder-adjusted % from
/// the per-game conviction multiplier. Capped by wave budget elsewhere.
///
/// Returns shares (not USDC). Returns 0 if below PM minimum.
pub fn flat_size(bankroll: f64, pct: f64, price: f64) -> f64 {
    if price <= 0.0 || price >= 1.0 || bankroll < 1.0 || pct <= 0.0 {
        return 0.0;
    }

    let usdc = bankroll * pct / 100.0;

    // Skip if below $2.50 minimum
    if usdc < 2.50 {
        return 0.0;
    }

    // Never bet more than available cash
    if usdc > bankroll {
        return 0.0;
    }

    let shares = usdc / price;

    // Enforce PM minimum: 5 shares
    if shares < 5.0 {
        return 0.0;
    }

    shares
}
