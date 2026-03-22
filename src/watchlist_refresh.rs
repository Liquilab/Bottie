//! Dynamisch de watchlist updaten op basis van wie NU goed scoort.
//!
//! Draait elke 2 uur. Zoekt wallets die:
//! - Over MEERDERE diverse markten actief zijn (voorkennis-signaal)
//! - Hoog volume handelen (overtuiging)
//! - Op extreme prijzen kopen (hoge confidence)
//! - Niet beperkt tot sports — alle markten

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use anyhow::Result;
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::clob::types::DataApiTrade;
use crate::config::SharedConfig;
use crate::wallet_tracker::{WalletTracker, WatchedWallet};

struct WalletScore {
    address: String,
    name: String,
    trades: u32,
    markets: u32,
    volume: f64,
    conviction: f64, // ratio of bets at extreme prices
    score: f64,
}

/// Scrape recent trades and score wallets by "info edge" likelihood.
/// Returns top N wallets sorted by score.
async fn discover_hot_wallets(client: &ClobClient, top_n: usize) -> Result<Vec<WalletScore>> {
    // Fetch multiple pages of recent trades
    let mut all_trades: Vec<DataApiTrade> = Vec::new();
    for offset in (0..5000).step_by(1000) {
        match client.get_recent_trades_offset(1000, offset).await {
            Ok(batch) => {
                if batch.is_empty() {
                    break;
                }
                all_trades.extend(batch);
            }
            Err(e) => {
                warn!("failed to fetch trades at offset {offset}: {e}");
                break;
            }
        }
    }

    if all_trades.is_empty() {
        return Ok(vec![]);
    }

    info!("analyzing {} trades for hot wallet discovery", all_trades.len());

    // Group by wallet
    let mut wallets: HashMap<String, WalletData> = HashMap::new();

    for trade in &all_trades {
        let addr = match &trade.proxy_wallet {
            Some(w) => w.to_lowercase(),
            None => continue,
        };

        let price = trade.price_f64();
        let size = trade.size_f64();
        let title = trade.title.as_deref().unwrap_or("");
        let name = trade.name.as_deref()
            .or(trade.pseudonym.as_deref())
            .unwrap_or(&addr[..10.min(addr.len())])
            .to_string();

        let entry = wallets.entry(addr.clone()).or_insert_with(|| WalletData {
            name,
            trades: 0,
            markets: HashSet::new(),
            volume: 0.0,
            prices: Vec::new(),
        });

        entry.trades += 1;
        entry.markets.insert(title.to_string());
        entry.volume += size * price;
        entry.prices.push(price);
    }

    // Score wallets
    let mut scored: Vec<WalletScore> = wallets
        .into_iter()
        .filter(|(_, d)| d.trades >= 3) // minimum activity
        .map(|(addr, d)| {
            let n_markets = d.markets.len() as u32;
            let extreme = d.prices.iter().filter(|&&p| p < 0.15 || p > 0.85).count();
            let conviction = if d.prices.is_empty() {
                0.0
            } else {
                extreme as f64 / d.prices.len() as f64
            };

            // Score: diversity^1.5 × volume^0.5 × (1 + conviction)
            // Diversity is king — wallets that trade many different markets
            // likely have real information, not just gambling on one thing
            let score = (n_markets as f64).powf(1.5) * d.volume.sqrt() * (1.0 + conviction);

            WalletScore {
                address: addr,
                name: d.name,
                trades: d.trades,
                markets: n_markets,
                volume: d.volume,
                conviction,
                score,
            }
        })
        .collect();

    scored.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    scored.truncate(top_n);

    Ok(scored)
}

struct WalletData {
    name: String,
    trades: u32,
    markets: HashSet<String>,
    volume: f64,
    prices: Vec<f64>,
}

/// Refresh loop: every 2 hours, discover hot wallets and merge into tracker
pub async fn watchlist_refresh_loop(
    client: Arc<ClobClient>,
    tracker: Arc<RwLock<WalletTracker>>,
    _config: SharedConfig,
) {
    loop {
        info!("starting watchlist refresh...");

        match discover_hot_wallets(&client, 30).await {
            Ok(hot_wallets) => {
                let mut tracker = tracker.write().await;
                let mut added = 0;

                for w in &hot_wallets {
                    if !tracker.is_watched(&w.address) {
                        // New discovers start with very low weight (observation mode).
                        // Weight only increases once record_trade_result builds
                        // up actual win rate data (see N7: unvalidated wallets).
                        let provisional_weight = 0.05;

                        tracker.add_wallet(WatchedWallet {
                            address: w.address.clone(),
                            name: w.name.clone(),
                            overall_win_rate: 0.50, // neutral default, not optimistic
                            sport_win_rates: HashMap::new(),
                            sport_trade_counts: HashMap::new(),
                            avg_roi: 0.0,
                            total_tracked_trades: 0,
                            total_wins: 0,
                            weight: provisional_weight,
                            last_seen: chrono::Utc::now(),
                        });
                        added += 1;
                    }
                }

                info!(
                    "watchlist refresh: found {} hot wallets, added {} new | total tracked: {}",
                    hot_wallets.len(),
                    added,
                    tracker.wallet_count()
                );

                // Log top discoveries
                for w in hot_wallets.iter().take(5) {
                    info!(
                        "  HOT: {} ({}) | trades={} mkts={} vol=${:.0} conv={:.0}%",
                        w.name, &w.address[..10], w.trades, w.markets, w.volume, w.conviction * 100.0
                    );
                }
            }
            Err(e) => {
                warn!("watchlist refresh failed: {e}");
            }
        }

        // Sleep 2 hours
        tokio::time::sleep(std::time::Duration::from_secs(2 * 3600)).await;
    }
}
