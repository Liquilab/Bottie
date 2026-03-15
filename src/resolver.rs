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
    let mut tp_counter: u32 = 0;

    loop {
        check_resolutions(&client, &logger, &risk, &tracker, &mut check_state).await;

        // Take-profit check every 2 minutes: sell positions above 95ct
        tp_counter += 1;
        if tp_counter % 2 == 0 {
            take_profit_check(&client, &logger, &risk).await;
        }

        // Sync phantom positions every 5 minutes:
        // mark trades as "phantom" if we no longer hold them on PM
        if tp_counter % 5 == 0 {
            sync_phantoms(&client, &logger, &risk).await;
        }

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

    // Collect unique condition_ids of open, filled, LIVE (non-dry-run) positions
    let mut open_markets: HashMap<String, Vec<usize>> = HashMap::new();
    for (i, trade) in trades.iter().enumerate() {
        if trade.result.is_none() && trade.filled && !trade.dry_run && !trade.outcome.is_empty() {
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

        // Note: Polymarket auto-redeems winning positions after resolution.
        // No manual /redeem API call needed (endpoint doesn't exist in CLOB API).
    }
}

/// Take-profit: sell open positions where best bid > 95ct.
/// At 95ct+ the risk/reward of holding is terrible: risk full investment for 5ct extra.
const TAKE_PROFIT_THRESHOLD: f64 = 0.95;

async fn take_profit_check(
    client: &ClobClient,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
) {
    let mut trades = logger.load_all();

    // Find open live positions
    let open_indices: Vec<usize> = trades.iter().enumerate()
        .filter(|(_, t)| t.filled && t.result.is_none() && !t.dry_run && !t.token_id.is_empty())
        .map(|(i, _)| i)
        .collect();

    if open_indices.is_empty() {
        return;
    }

    let mut sold_count = 0u32;
    let mut total_profit = 0.0f64;
    let mut sold_meta: Vec<(String, String, f64)> = Vec::new();

    for &idx in &open_indices {
        let token_id = trades[idx].token_id.clone();
        let entry_price = trades[idx].price;
        let shares = trades[idx].size_shares;
        let title = trades[idx].market_title.clone();

        // Check best bid
        let best_bid = match client.get_best_bid(&token_id).await {
            Ok(b) => b,
            Err(_) => continue,
        };

        if best_bid < TAKE_PROFIT_THRESHOLD {
            continue;
        }

        // Sell at best bid via FOK
        info!(
            "TAKE PROFIT: {} | bid={:.0}ct (entry {:.0}ct) | selling {:.1} shares",
            &title[..title.len().min(50)], best_bid * 100.0, entry_price * 100.0, shares
        );

        // Get fee rate (don't assume 0 — fee markets would fail silently)
        let fee_bps = client.get_fee_rate_bps(&token_id).await.unwrap_or(0);

        match client.create_and_post_order(
            &token_id,
            best_bid,
            shares,
            crate::clob::client::Side::Sell,
            crate::clob::client::OrderType::FOK,
            fee_bps,
        ).await {
            Ok(resp) => {
                if resp.is_filled() {
                    let actual_shares = { let fs = resp.filled_size(); if fs > 0.0 { fs } else { shares } };
                    let sell_usdc = actual_shares * best_bid;
                    let fee_cost = sell_usdc * (fee_bps as f64 / 10000.0);
                    let profit = sell_usdc - fee_cost - trades[idx].size_usdc;
                    let wallet = trades[idx].copy_wallet.clone().unwrap_or_default();
                    let sport = trades[idx].sport.clone();
                    trades[idx].result = Some("take_profit".to_string());
                    trades[idx].pnl = Some(profit);
                    trades[idx].resolved_at = Some(Utc::now());
                    sold_count += 1;
                    total_profit += profit;
                    sold_meta.push((wallet, sport, profit));

                    info!(
                        "TAKE PROFIT FILLED: {} | profit=${:.2} | bid={:.0}ct",
                        &title[..title.len().min(40)], profit, best_bid * 100.0
                    );
                } else {
                    warn!("TAKE PROFIT NOT FILLED: {} (bid may have moved)", &title[..title.len().min(40)]);
                }
            }
            Err(e) => {
                warn!("TAKE PROFIT ERROR: {} | {}", &title[..title.len().min(40)], e);
            }
        }

        // Rate limit between sells
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    if sold_count > 0 {
        logger.rewrite_all(trades);
        let mut r = risk.write().await;
        for (wallet, sport, pnl) in &sold_meta {
            r.record_trade_closed_with_context(
                *pnl,
                if wallet.is_empty() { None } else { Some(wallet.as_str()) },
                sport,
            );
        }
        info!("take-profit: sold {} positions, total profit ${:.2}", sold_count, total_profit);
    }
}

/// Sync trade log with on-chain positions. Mark trades as "phantom" if
/// we no longer hold them on Polymarket (manually sold or never filled).
async fn sync_phantoms(
    client: &ClobClient,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
) {
    let funder = client.funder_address();

    let our_positions = match client.get_wallet_positions(&funder, 500).await {
        Ok(p) => p,
        Err(_) => return,
    };

    let mut held = std::collections::HashSet::new();
    for pos in &our_positions {
        if pos.size_f64() > 0.01 {
            held.insert(pos.position_key());
        }
    }

    let mut trades = logger.load_all();
    let mut phantom_count = 0u32;

    for trade in trades.iter_mut() {
        if trade.result.is_some() || !trade.filled || trade.dry_run {
            continue;
        }
        let key = format!("{}:{}", trade.condition_id, trade.outcome);
        if !held.contains(&key) {
            trade.result = Some("phantom".to_string());
            trade.pnl = Some(0.0);
            trade.resolved_at = Some(Utc::now());
            phantom_count += 1;
        }
    }

    if phantom_count > 0 {
        logger.rewrite_all(trades);
        let mut r = risk.write().await;
        for _ in 0..phantom_count {
            r.record_trade_closed_with_context(0.0, None, "");
        }
        info!("phantom-sync: marked {} positions as phantom (not held on PM)", phantom_count);
    }
}

