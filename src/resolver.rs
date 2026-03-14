//! Resolution tracker: periodically checks if open positions have resolved
//! and updates the trade log with win/loss/pnl. Also calls record_trade_closed
//! so the risk manager and bankroll stay accurate.
//!
//! Smart scheduling: checks markets more frequently as they approach their end_date.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::logger::TradeLogger;
use crate::risk::RiskManager;
use crate::wallet_tracker::WalletTracker;

/// Tracks when each condition_id was last checked and its known end_date
struct MarketCheckState {
    last_checked: DateTime<Utc>,
    end_date: Option<DateTime<Utc>>,
}

pub async fn resolver_loop(
    client: Arc<ClobClient>,
    logger: Arc<TradeLogger>,
    risk: Arc<RwLock<RiskManager>>,
    tracker: Arc<RwLock<WalletTracker>>,
) {
    let mut check_state: HashMap<String, MarketCheckState> = HashMap::new();

    loop {
        check_resolutions(&client, &logger, &risk, &tracker, &mut check_state).await;

        // Base tick: every 60 seconds. Individual markets are skipped if not due.
        tokio::time::sleep(Duration::from_secs(60)).await;
    }
}

/// Determine how often a market should be checked based on its end_date.
fn check_interval(end_date: Option<&DateTime<Utc>>) -> Duration {
    let Some(end) = end_date else {
        // Unknown end_date: check every 5 minutes (conservative default)
        return Duration::from_secs(300);
    };

    let now = Utc::now();
    let until_end = end.signed_duration_since(now);

    if until_end.num_seconds() <= 0 {
        // Past end_date: should be resolved soon, check every 60s
        Duration::from_secs(60)
    } else if until_end.num_hours() < 1 {
        // < 1 hour: check every 2 minutes
        Duration::from_secs(120)
    } else if until_end.num_hours() < 24 {
        // < 1 day: check every 5 minutes
        Duration::from_secs(300)
    } else if until_end.num_days() < 3 {
        // < 3 days: check every 30 minutes
        Duration::from_secs(1800)
    } else {
        // > 3 days: check every hour
        Duration::from_secs(3600)
    }
}

async fn check_resolutions(
    client: &ClobClient,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
    tracker: &Arc<RwLock<WalletTracker>>,
    check_state: &mut HashMap<String, MarketCheckState>,
) {
    let mut trades = logger.load_all();

    // Collect unique condition_ids of open, filled positions
    let mut open_markets: HashMap<String, Vec<usize>> = HashMap::new();
    for (i, trade) in trades.iter().enumerate() {
        if trade.result.is_none() && trade.filled && !trade.outcome.is_empty() {
            open_markets
                .entry(trade.condition_id.clone())
                .or_default()
                .push(i);
        }
    }

    if open_markets.is_empty() {
        return;
    }

    // Clean up state for markets no longer open
    check_state.retain(|cid, _| open_markets.contains_key(cid));

    // Determine which markets are due for checking
    let now = Utc::now();
    let mut due_markets: Vec<String> = Vec::new();
    for condition_id in open_markets.keys() {
        let state = check_state.get(condition_id.as_str());
        let interval = match state {
            Some(s) => check_interval(s.end_date.as_ref()),
            None => Duration::from_secs(0), // never checked → check now
        };

        let last_checked = state.map(|s| s.last_checked).unwrap_or(DateTime::UNIX_EPOCH);
        let elapsed = now.signed_duration_since(last_checked);

        if elapsed.to_std().unwrap_or(Duration::MAX) >= interval {
            due_markets.push(condition_id.clone());
        }
    }

    if due_markets.is_empty() {
        return;
    }

    info!(
        "resolver: checking {}/{} markets due",
        due_markets.len(),
        open_markets.len()
    );

    let mut resolved_count = 0u32;
    let mut live_resolved_count = 0u32;

    // (wallet, sport, won, pnl, is_dry_run)
    let mut wallet_results: Vec<(String, String, bool, f64, bool)> = Vec::new();
    let mut to_redeem: Vec<String> = Vec::new();

    for condition_id in &due_markets {
        // Rate-limit: 200ms between market status checks
        tokio::time::sleep(Duration::from_millis(200)).await;

        let market_info = match client.get_market_info(condition_id).await {
            Ok(Some(m)) => m,
            Ok(None) => {
                // Update last_checked even on miss
                check_state.insert(condition_id.clone(), MarketCheckState {
                    last_checked: now,
                    end_date: None,
                });
                continue;
            }
            Err(e) => {
                warn!(
                    "resolver: failed to check {}: {e}",
                    &condition_id[..12.min(condition_id.len())]
                );
                continue;
            }
        };

        // Parse and store end_date for smart scheduling
        let end_date = market_info.end_date.as_ref().and_then(|s| {
            DateTime::parse_from_rfc3339(s)
                .or_else(|_| DateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.fZ"))
                .ok()
                .map(|dt| dt.with_timezone(&Utc))
        });

        check_state.insert(condition_id.clone(), MarketCheckState {
            last_checked: now,
            end_date,
        });

        let winner = match market_info.winning_outcome() {
            Some(w) => w,
            None => continue, // still open
        };

        let indices = match open_markets.get(condition_id) {
            Some(idx) => idx,
            None => continue,
        };

        info!(
            "RESOLVED: {} → winner: {}",
            &condition_id[..12.min(condition_id.len())],
            winner
        );

        for &idx in indices {
            let trade = &mut trades[idx];
            if trade.result.is_some() {
                continue; // already resolved (race guard)
            }

            let is_invalid = winner.to_lowercase() == "invalid";
            let won = trade.outcome.to_lowercase() == winner.to_lowercase();
            let pnl = if is_invalid {
                0.0 // market invalidated — capital is refunded
            } else if won {
                trade.size_shares * (1.0 - trade.price)
            } else {
                -trade.size_usdc
            };

            trade.result = Some(
                if is_invalid { "refund".to_string() }
                else if won { "win".to_string() }
                else { "loss".to_string() }
            );
            trade.pnl = Some(pnl);
            trade.resolved_at = Some(Utc::now());

            resolved_count += 1;

            if !trade.dry_run {
                live_resolved_count += 1;
                if won {
                    to_redeem.push(condition_id.clone());
                }
            }

            // Collect for risk manager + wallet tracker update
            wallet_results.push((
                trade.copy_wallet.clone().unwrap_or_default(),
                trade.sport.clone(),
                won,
                pnl,
                trade.dry_run,
            ));
        }
    }

    if resolved_count > 0 {
        logger.rewrite_all(trades);

        // Update risk manager: decrement open_bets + per-wallet/per-sport for live trades
        {
            let mut r = risk.write().await;
            for (wallet, sport, _won, pnl, is_dry_run) in &wallet_results {
                if *is_dry_run {
                    continue;
                }
                r.record_trade_closed_with_context(
                    *pnl,
                    if wallet.is_empty() { None } else { Some(wallet.as_str()) },
                    sport,
                );
            }
        }

        // Update wallet tracker with resolved trade results
        if !wallet_results.is_empty() {
            let mut t = tracker.write().await;
            for (wallet, sport, won, pnl, _dry) in &wallet_results {
                if !wallet.is_empty() {
                    t.record_trade_result(wallet, sport, *won, *pnl);
                }
            }
        }

        info!(
            "resolver: resolved {} trades ({} live, {} dry-run)",
            resolved_count, live_resolved_count, resolved_count - live_resolved_count
        );

        // Redeem winning positions
        to_redeem.sort();
        to_redeem.dedup();
        for condition_id in &to_redeem {
            tokio::time::sleep(Duration::from_millis(500)).await;
            match client.redeem_position(condition_id).await {
                Ok(()) => info!("REDEEMED: {}", &condition_id[..12.min(condition_id.len())]),
                Err(e) => warn!("redeem failed for {}: {e}", &condition_id[..12.min(condition_id.len())]),
            }
        }
    }
}
