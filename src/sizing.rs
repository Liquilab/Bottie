/// Base sizing: flat 2.5% for both football and NBA (2026-04-10).
///
/// History: confidence-based (2.5-10%) → flat 5% → flat 2% (2026-04-09)
/// → flat 2.5% + Cannae CV ladder (2026-04-10).
pub fn confidence_pct(_confidence: f64) -> f64 {
    2.5
}

/// Cannae conviction ladder (2026-04-10).
///
/// Size multiplier based on the true hauptbet's current_value for the game.
/// Returns None for CV below the skip-floor ($500).
///
/// Reverses the old `feedback_hauptbet_strategy` rule that forbade $-based
/// sizing. Data: 7d analysis showed <$500 bucket is 77% WR vs 90%+ above;
/// user opted for aggressive ladder above the floor based on conviction priors.
///
/// Applied uniformly to all legs (hauptbet + hedge companions) on the game.
pub fn cannae_cv_multiplier(cv_usdc: f64) -> Option<f64> {
    if cv_usdc < 500.0     { return None; }        // skip floor
    if cv_usdc < 2_000.0   { return Some(0.5); }   // 1.25%
    if cv_usdc < 5_000.0   { return Some(1.0); }   // 2.5%
    if cv_usdc < 10_000.0  { return Some(1.5); }   // 3.75%
    if cv_usdc < 20_000.0  { return Some(2.0); }   // 5.0%
    Some(3.0)                                       // >$20k → 7.5%
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
