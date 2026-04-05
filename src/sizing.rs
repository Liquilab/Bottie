/// Flat sizing: all bets get the same bankroll %.
///
/// Previously confidence-based (2.5%-10%), now flat 3% pending SSOT
/// game-level analysis to validate whether variable sizing adds value.
pub fn confidence_pct(_confidence: f64) -> f64 {
    3.0
}

/// Flat sizing: bankroll × pct / price = shares.
///
/// No proportional weighting, no conviction, no Kelly.
/// The pct is determined by sport + game line (from SportSizingConfig),
/// capped by wave budget (bankroll / total_lines_in_wave).
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
