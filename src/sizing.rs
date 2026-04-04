/// Confidence-based sizing: maps signal confidence (0-1) to bankroll %.
///
/// Confidence is derived from the bet price: (price × 1.10).min(0.95).
/// Based on 4-week Cannae analysis (142 trades):
///   < 0.60 → 2.5%  (close games, lower ROI)
///   0.60-0.70 → 5.0%
///   0.70-0.80 → 10.0% (best ROI bucket)
///   0.80-0.90 → 5.0%  (anomaly: n=17, pending more data)
///   ≥ 0.90 → 10.0%  (strong favorites, solid ROI)
pub fn confidence_pct(confidence: f64) -> f64 {
    if confidence >= 0.90 {
        10.0
    } else if confidence >= 0.80 {
        5.0
    } else if confidence >= 0.70 {
        10.0
    } else if confidence >= 0.60 {
        5.0
    } else {
        2.5
    }
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
