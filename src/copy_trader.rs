use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::config::SharedConfig;
use crate::signal::CopySignal;
use crate::wallet_tracker::WalletTracker;

pub struct CopyTrader {
    client: Arc<ClobClient>,
    tracker: Arc<RwLock<WalletTracker>>,
    config: SharedConfig,
    /// Previous position snapshots per wallet: position_key → size
    /// Used for snapshot-diff to detect new positions
    prev_positions: HashMap<String, HashMap<String, f64>>,
    /// Per-market consensus: token_key → vec of recent bets
    recent_bets: HashMap<String, Vec<RecentBet>>,
}

#[derive(Debug, Clone)]
struct RecentBet {
    wallet: String,
    outcome: String,
    weight: f64,
    usdc_size: f64,
    timestamp: DateTime<Utc>,
}

impl CopyTrader {
    pub fn new(
        client: Arc<ClobClient>,
        tracker: Arc<RwLock<WalletTracker>>,
        config: SharedConfig,
    ) -> Self {
        Self {
            client,
            tracker,
            config,
            prev_positions: HashMap::new(),
            recent_bets: HashMap::new(),
        }
    }

    /// Poll watched wallets for new positions using /positions snapshot-diff.
    /// This replaces the unreliable /activity endpoint.
    pub async fn poll(&mut self) -> Vec<CopySignal> {
        let config = self.config.read().await;
        let max_delay = Duration::from_secs(config.copy_trading.max_delay_seconds);
        let _ = max_delay; // delay check is implicit: new positions detected = fresh enough

        // Read autoresearch_params for wallet weight overrides and sport multipliers
        let wallet_overrides = config.autoresearch_params.wallet_weights_override.clone();
        let sport_multipliers = config.autoresearch_params.sport_multipliers.clone();

        let watchlist: Vec<(String, String, f64)> = config
            .copy_trading
            .watchlist
            .iter()
            .map(|e| {
                let addr = e.address.to_lowercase();
                let base_weight = e.weight;
                let weight = wallet_overrides.get(&addr).copied().unwrap_or(base_weight);
                (addr, e.name.clone(), weight)
            })
            .collect();
        drop(config);

        let tracker = self.tracker.read().await;
        let mut signals = Vec::new();
        let mut total_new = 0u32;

        // Rate-limit to stay under API limits
        let delay_per_wallet = if !watchlist.is_empty() {
            let ms = (60_000 / watchlist.len().max(1) as u64).min(2000).max(200);
            Duration::from_millis(ms)
        } else {
            Duration::from_millis(200)
        };

        for (address, name, weight) in &watchlist {
            if *weight <= 0.0 {
                continue;
            }

            tokio::time::sleep(delay_per_wallet).await;

            // Fetch current positions via /positions (reliable endpoint)
            let positions = match self.client.get_wallet_positions(address, 100).await {
                Ok(p) => p,
                Err(e) => {
                    warn!("positions fetch failed for {name}: {e}");
                    continue;
                }
            };

            // Build current snapshot: position_key → size
            let mut current_snapshot: HashMap<String, f64> = HashMap::new();
            for pos in &positions {
                let key = pos.position_key();
                if key == ":" {
                    continue; // skip invalid positions
                }
                current_snapshot.insert(key, pos.size_f64());
            }

            // Compare with previous snapshot to find NEW positions
            let prev = self.prev_positions.get(address);

            for pos in &positions {
                let key = pos.position_key();
                if key == ":" {
                    continue;
                }

                let cur_size = pos.size_f64();
                if cur_size <= 0.0 {
                    continue;
                }

                // Detect new or increased positions
                let is_new = match prev {
                    None => true, // first poll for this wallet — treat all as new on first run
                    Some(prev_snap) => {
                        let prev_size = prev_snap.get(&key).copied().unwrap_or(0.0);
                        cur_size > prev_size * 1.05 // >5% increase = new buy activity
                    }
                };

                if !is_new {
                    continue;
                }

                // Skip if this is the first poll (seeding phase — don't trade on historical positions)
                if prev.is_none() {
                    continue;
                }

                total_new += 1;

                let price = pos.avg_price_f64();
                if price <= 0.0 || price >= 1.0 {
                    continue;
                }
                let usdc_size = pos.initial_value_f64();
                let condition_id = pos.condition_id.clone().unwrap_or_default();
                let asset = pos.asset.clone().unwrap_or_default();
                let outcome = pos.outcome.clone().unwrap_or_default();
                let title = pos.title.clone().unwrap_or_else(|| {
                    condition_id[..12.min(condition_id.len())].to_string()
                });
                let sport = Self::detect_sport(&title, pos.slug.as_deref().unwrap_or(""));

                // Track for weighted consensus
                let token_key = format!("{}:{}", condition_id, outcome);
                self.recent_bets
                    .entry(token_key.clone())
                    .or_default()
                    .push(RecentBet {
                        wallet: address.clone(),
                        outcome: outcome.clone(),
                        weight: *weight,
                        usdc_size,
                        timestamp: Utc::now(),
                    });

                // Prune bets older than 2 hours
                if let Some(bets) = self.recent_bets.get_mut(&token_key) {
                    bets.retain(|b| {
                        Utc::now().signed_duration_since(b.timestamp) < chrono::Duration::hours(2)
                    });
                }

                // Consensus scoring by outcome
                let (outcome_score, consensus_wallets) = self
                    .recent_bets
                    .get(&token_key)
                    .map(|bets| {
                        let mut score = 0.0f64;
                        let mut wallets = std::collections::HashSet::new();
                        for b in bets {
                            score += b.weight * (1.0 + b.usdc_size.sqrt() / 100.0);
                            wallets.insert(b.wallet.clone());
                        }
                        (score, wallets.len() as u32)
                    })
                    .unwrap_or((*weight, 1));

                // Confidence: anchor to market price with conservative edge assumption
                let wallet_info = tracker.get_wallet(address);
                let implied_prob = price;

                let base_win_rate = match wallet_info {
                    Some(w) if w.total_tracked_trades >= 10 => w.overall_win_rate,
                    Some(w) if w.total_tracked_trades > 0 => {
                        let trust = w.total_tracked_trades as f64 / 10.0;
                        let market_anchored = implied_prob * 1.10;
                        market_anchored * (1.0 - trust) + w.overall_win_rate * trust
                    }
                    _ => {
                        // No data: assume 10% edge over market
                        implied_prob * 1.10
                    }
                };

                let config = self.config.read().await;
                let consensus_multiplier = match consensus_wallets {
                    0 | 1 => 1.0,
                    2 => config.copy_trading.consensus.multiplier_2,
                    _ => config.copy_trading.consensus.multiplier_3plus,
                };
                drop(config);

                // Apply sport multiplier from autoresearch
                let sport_mult = sport_multipliers.get(&sport).copied().unwrap_or(1.0);

                let confidence = (base_win_rate * consensus_multiplier * sport_mult).min(0.95);

                info!(
                    "SIGNAL: {} ({:.0}ct) | {} | {:.0}$ | {} wallets (score:{:.1}) | conf={:.0}% base_wr={:.0}%",
                    name,
                    price * 100.0,
                    title.chars().take(50).collect::<String>(),
                    usdc_size,
                    consensus_wallets,
                    outcome_score,
                    confidence * 100.0,
                    base_win_rate * 100.0
                );

                signals.push(CopySignal {
                    source_wallet: address.clone(),
                    source_name: name.clone(),
                    token_id: asset,
                    condition_id,
                    side: "BUY".to_string(),
                    price,
                    size: usdc_size,
                    market_title: title,
                    sport,
                    outcome,
                    confidence,
                    consensus_count: consensus_wallets,
                    timestamp: Utc::now(),
                    signal_delay_ms: 0, // positions-based: no delay concept
                });
            }

            // Store current snapshot for next comparison
            self.prev_positions.insert(address.clone(), current_snapshot);
        }

        if total_new > 0 || !signals.is_empty() {
            info!(
                "POLL COMPLETE: {} new positions across {} wallets → {} signals",
                total_new,
                watchlist.len(),
                signals.len()
            );
        }

        signals
    }

    /// Detect sport from title/slug. Public static version for use by sync module.
    pub fn detect_sport_static(title: &str, event_slug: &str) -> String {
        Self::detect_sport(title, event_slug)
    }

    fn detect_sport(title: &str, event_slug: &str) -> String {
        let combined = format!("{} {}", title.to_lowercase(), event_slug.to_lowercase());
        if combined.contains("nba") || combined.contains("basketball") {
            "nba".to_string()
        } else if combined.contains("nfl") || combined.contains("football") {
            "nfl".to_string()
        } else if combined.contains("nhl") || combined.contains("hockey") {
            "nhl".to_string()
        } else if combined.contains("mlb") || combined.contains("baseball") {
            "mlb".to_string()
        } else if combined.contains("soccer")
            || combined.contains("epl")
            || combined.contains("premier league")
            || combined.contains("champions league")
            || combined.contains("ucl")
        {
            "soccer".to_string()
        } else if combined.contains("tennis") || combined.contains("atp") || combined.contains("wta")
        {
            "tennis".to_string()
        } else if combined.contains("mma") || combined.contains("ufc") {
            "mma".to_string()
        } else if combined.contains("esport")
            || combined.contains("dota")
            || combined.contains("cs2")
            || combined.contains("csgo")
        {
            "esports".to_string()
        } else {
            "other".to_string()
        }
    }
}
