use chrono::{DateTime, Utc};

/// Signal from copy trading
#[derive(Debug, Clone)]
pub struct CopySignal {
    pub source_wallet: String,
    pub source_name: String,
    pub token_id: String,
    pub condition_id: String,
    pub side: String,
    pub price: f64,
    pub size: f64,
    pub market_title: String,
    pub sport: String,
    pub outcome: String,
    pub event_slug: String,
    pub confidence: f64,
    pub consensus_count: u32,
    pub timestamp: DateTime<Utc>,
    pub signal_delay_ms: u64,
}

/// Signal from odds arbitrage
#[derive(Debug, Clone)]
pub struct ArbSignal {
    pub token_id: String,
    pub condition_id: String,
    pub side: String,
    pub pm_price: f64,
    pub implied_prob: f64,
    pub bookmaker_prob: f64,
    pub edge_pct: f64,
    pub market_title: String,
    pub sport: String,
    pub bookmaker: String,
}

/// Combined signal source
#[derive(Debug, Clone)]
pub enum SignalSource {
    Copy(CopySignal),
    OddsArb(ArbSignal),
}

/// Aggregated signal ready for sizing + execution
#[derive(Debug, Clone)]
pub struct AggregatedSignal {
    pub token_id: String,
    pub condition_id: String,
    pub side: String,
    pub price: f64,
    pub market_title: String,
    pub sport: String,
    pub outcome: String,
    pub event_slug: String,
    pub sources: Vec<SignalSource>,
    pub combined_confidence: f64,
    pub edge_pct: f64,
}

pub struct SignalAggregator;

impl SignalAggregator {
    /// Aggregate copy signals and arb signals into trading decisions
    pub fn aggregate(
        copy_signals: &[CopySignal],
        arb_signals: &[ArbSignal],
    ) -> Vec<AggregatedSignal> {
        let mut result = Vec::new();

        // Group copy signals by token+side
        let mut copy_by_token: std::collections::HashMap<String, Vec<&CopySignal>> =
            std::collections::HashMap::new();
        for sig in copy_signals {
            let key = format!("{}:{}:{}", sig.condition_id, sig.outcome, sig.side);
            copy_by_token.entry(key).or_default().push(sig);
        }

        // Process copy signals
        for (_key, signals) in &copy_by_token {
            let first = signals[0];

            // Check if there's a conflicting arb signal
            let matching_arb = arb_signals.iter().find(|a| {
                a.condition_id == first.condition_id
            });

            let arb_aligned = matching_arb
                .map(|a| a.side == first.side)
                .unwrap_or(true); // no arb = neutral, proceed

            if !arb_aligned {
                // Copy and arb conflict → skip
                tracing::info!(
                    "SKIP: copy/arb conflict on {} (copy={}, arb={})",
                    first.market_title,
                    first.side,
                    matching_arb.unwrap().side
                );
                continue;
            }

            let mut combined_confidence = first.confidence;
            let mut sources: Vec<SignalSource> = signals
                .iter()
                .map(|s| SignalSource::Copy((*s).clone()))
                .collect();

            // Boost if arb aligns
            if let Some(arb) = matching_arb {
                combined_confidence = (combined_confidence * 1.5).min(0.95);
                sources.push(SignalSource::OddsArb(arb.clone()));
            }

            // Edge is only meaningful when an arb signal provides an independent
            // probability estimate. For pure copy signals, confidence is wallet
            // signal strength (not an outcome probability), so edge = 0.
            let edge = if let Some(arb) = matching_arb {
                arb.edge_pct
            } else {
                0.0
            };

            result.push(AggregatedSignal {
                token_id: first.token_id.clone(),
                condition_id: first.condition_id.clone(),
                side: first.side.clone(),
                price: first.price,
                market_title: first.market_title.clone(),
                sport: first.sport.clone(),
                outcome: first.outcome.clone(),
                event_slug: first.event_slug.clone(),
                sources,
                combined_confidence,
                edge_pct: edge,
            });
        }

        // Process standalone arb signals (no copy signal for same market)
        for arb in arb_signals {
            let has_copy = copy_signals
                .iter()
                .any(|c| c.condition_id == arb.condition_id);
            if !has_copy {
                result.push(AggregatedSignal {
                    token_id: arb.token_id.clone(),
                    condition_id: arb.condition_id.clone(),
                    side: arb.side.clone(),
                    price: arb.pm_price,
                    market_title: arb.market_title.clone(),
                    sport: arb.sport.clone(),
                    outcome: String::new(),
                    event_slug: String::new(),
                    sources: vec![SignalSource::OddsArb(arb.clone())],
                    combined_confidence: arb.bookmaker_prob,
                    edge_pct: arb.edge_pct,
                });
            }
        }

        result
    }
}
