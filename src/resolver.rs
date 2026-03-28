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

use crate::clob::client::{ClobClient, OrderType, Side};
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
    config: crate::config::SharedConfig,
) {
    let mut check_state: HashMap<String, MarketCheckState> = HashMap::new();
    // Track recent failed sell attempts to avoid retrying every cycle
    let mut tp_cooldown: HashMap<String, DateTime<Utc>> = HashMap::new();
    let mut auto_sell_cooldown: HashMap<String, DateTime<Utc>> = HashMap::new();
    let mut auto_sell_pending: HashMap<String, String> = HashMap::new();

    loop {
        // Phantom sync FIRST — catch false fills before resolution resolves them as win/loss
        sync_phantoms(&client, &logger, &risk).await;

        check_resolutions(&client, &logger, &risk, &tracker, &mut check_state).await;

        // Auto-sell: sell open positions when best bid >= min_bid (e.g. 98ct)
        // Frees cash before PM resolution (2-4 hour delay after game end)
        {
            let cfg = config.read().await;
            if cfg.auto_sell.enabled {
                auto_sell_check(&client, &logger, &risk, cfg.auto_sell.min_bid, &mut auto_sell_cooldown, &mut auto_sell_pending).await;
            }
        }

        // Take-profit DISABLED — need data lake analysis first (RUS-234)
        // let tp_config = config.read().await.take_profit.clone();
        // take_profit_check(&client, &logger, &risk, &tp_config, &mut tp_cooldown).await;

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

/// Truncate a string to at most `max` bytes, but always on a char boundary.
fn truncate_title(s: &str, max: usize) -> &str {
    if s.len() <= max {
        return s;
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Auto-sell positions when best bid >= min_bid (e.g. 0.98).
/// Frees cash immediately instead of waiting 2-4h for PM resolution.
async fn auto_sell_check(
    client: &ClobClient,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
    min_bid: f64,
    cooldown: &mut HashMap<String, DateTime<Utc>>,
    pending_sell_orders: &mut HashMap<String, String>,
) {
    let mut trades = logger.load_all();

    // Find open live positions with a token_id
    let open_indices: Vec<usize> = trades.iter().enumerate()
        .filter(|(_, t)| t.filled && t.result.is_none() && !t.dry_run && !t.token_id.is_empty())
        .map(|(i, _)| i)
        .collect();

    if open_indices.is_empty() {
        return;
    }

    let mut sold_meta: Vec<(String, String, f64)> = Vec::new();

    for &idx in &open_indices {
        let token_id = trades[idx].token_id.clone();
        let shares = trades[idx].size_shares;
        let title = trades[idx].market_title.clone();
        let short_title = truncate_title(&title, 50);

        // 5 min cooldown after failed/partial sell
        if let Some(last) = cooldown.get(&token_id) {
            if Utc::now().signed_duration_since(*last) < chrono::Duration::minutes(5) {
                continue;
            }
        }

        // Check best bid
        let best_bid = match client.get_best_bid(&token_id).await {
            Ok(b) => b,
            Err(_) => continue,
        };

        if best_bid < min_bid {
            continue;
        }

        // Sell at best_bid (not min_bid) for accurate PnL and price improvement
        let sell_price = best_bid;

        info!(
            "AUTO SELL: {} | bid={:.0}ct | selling {:.1} shares @ {:.0}ct",
            short_title, best_bid * 100.0, shares, sell_price * 100.0
        );

        // Cancel any pending sell order from a previous attempt before placing a new one
        if let Some(prev_order_id) = pending_sell_orders.remove(&token_id) {
            if let Err(e) = client.cancel_order(&prev_order_id).await {
                warn!("AUTO SELL: failed to cancel previous order {}: {}", &prev_order_id[..prev_order_id.len().min(12)], e);
                // Continue anyway — the old order might have filled or expired
            }
        }

        // Get fee rate — retry with parsed fee from error if needed
        let fee_bps = client.get_fee_rate_bps(&token_id).await.unwrap_or(0);

        let (resp, actual_fee) = match client.create_and_post_order(
            &token_id, sell_price, shares, Side::Sell, OrderType::GTC, fee_bps,
        ).await {
            Ok(r) => (r, fee_bps),
            Err(e) => {
                let err_str = e.to_string();
                // Fee retry: parse correct fee from error message
                if let Some(idx_fee) = err_str.find("taker fee: ")
                    .or_else(|| err_str.find("maker fee: "))
                {
                    let fee_str = &err_str[idx_fee + 11..];
                    let end = fee_str.find(|c: char| !c.is_ascii_digit()).unwrap_or(fee_str.len());
                    if let Ok(correct_fee) = fee_str[..end].parse::<u32>() {
                        info!("AUTO SELL: retrying with fee={} (was {})", correct_fee, fee_bps);
                        match client.create_and_post_order(
                            &token_id, sell_price, shares, Side::Sell, OrderType::GTC, correct_fee,
                        ).await {
                            Ok(r) => (r, correct_fee),
                            Err(e2) => {
                                warn!("AUTO SELL ERROR (retry): {} | {}", truncate_title(&title, 40), e2);
                                cooldown.insert(token_id.clone(), Utc::now());
                                continue;
                            }
                        }
                    } else {
                        warn!("AUTO SELL ERROR: {} | {}", truncate_title(&title, 40), e);
                        cooldown.insert(token_id.clone(), Utc::now());
                        continue;
                    }
                } else {
                    warn!("AUTO SELL ERROR: {} | {}", truncate_title(&title, 40), e);
                    cooldown.insert(token_id.clone(), Utc::now());
                    continue;
                }
            }
        };

        if resp.is_filled() {
            let filled = resp.filled_size();
            // Partial fill: if < 95% filled, cooldown + retry next cycle
            if filled > 0.0 && filled < shares * 0.95 {
                warn!(
                    "AUTO SELL PARTIAL: {} | {:.1}/{:.1} shares",
                    truncate_title(&title, 40), filled, shares
                );
                // Track order ID so we can cancel it before retrying
                if let Some(oid) = resp.effective_id() {
                    pending_sell_orders.insert(token_id.clone(), oid.to_string());
                }
                cooldown.insert(token_id.clone(), Utc::now());
                continue;
            }

            let actual = if filled > 0.0 { filled } else { shares };
            let sell_usdc = actual * sell_price;
            let fee_cost = sell_usdc * (actual_fee as f64 / 10000.0);
            let profit = sell_usdc - fee_cost - trades[idx].size_usdc;
            let roi = profit / trades[idx].size_usdc * 100.0;
            let wallet = trades[idx].copy_wallet.clone().unwrap_or_default();
            let sport = trades[idx].sport.clone();

            trades[idx].result = Some("take_profit".to_string());
            trades[idx].pnl = Some(profit);
            trades[idx].actual_pnl = Some(profit);
            trades[idx].sell_price = Some(sell_price);
            trades[idx].exit_type = Some("auto_sell".to_string());
            trades[idx].resolved_at = Some(Utc::now());

            sold_meta.push((wallet, sport, profit));

            info!(
                "AUTO SELL FILLED: {} | profit=${:.2} ({:.0}%) | {:.1} shares @ {:.0}ct",
                truncate_title(&title, 40), profit, roi, actual, sell_price * 100.0
            );
        } else {
            // GTC not filled — track order ID for cancellation on next attempt
            if let Some(oid) = resp.effective_id() {
                pending_sell_orders.insert(token_id.clone(), oid.to_string());
            }
            warn!("AUTO SELL NOT FILLED: {} (bid may have moved, GTC order placed)", truncate_title(&title, 40));
            cooldown.insert(token_id.clone(), Utc::now());
        }

        // Rate limit between sells
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    if !sold_meta.is_empty() {
        logger.rewrite_all(trades);
        let mut r = risk.write().await;
        for (wallet, sport, pnl) in &sold_meta {
            r.record_trade_closed_with_context(
                *pnl,
                if wallet.is_empty() { None } else { Some(wallet.as_str()) },
                sport,
            );
        }
        info!("auto-sell: sold {} positions at {}ct+", sold_meta.len(), (min_bid * 100.0) as u32);
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
            trade.actual_pnl = Some(pnl);
            trade.sell_price = Some(if won { 1.0 } else if is_invalid { trade.price } else { 0.0 });
            trade.exit_type = Some("resolution".to_string());
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

/// Determine take-profit threshold based on entry price and market type.
///
/// Bootstrapping rules (before decision table exists):
/// - Entry < 0.30: sell at 90ct+ (longshot that hit, take the money)
/// - Entry 0.30-0.50: sell at 85ct+ (medium confidence, good profit)
/// - Entry 0.50-0.70: sell at 88ct+ (higher confidence, still good R:R)
/// - Entry 0.70-0.83: sell at 93ct+ (high confidence, only sell near-certain)
/// - Any entry: ALWAYS sell at safety_threshold (default 95ct+)
///
/// Additional rule: sell if delta > 0.30 AND current_price > 0.80
/// (locked in 30ct+ profit and price is high enough)
fn should_take_profit(entry_price: f64, best_bid: f64, market_type: &str, safety_threshold: f64, min_delta: f64) -> bool {
    let delta = best_bid - entry_price;

    // Safety net: always sell at safety threshold
    if best_bid >= safety_threshold {
        return true;
    }

    // Must have minimum positive delta
    if delta < min_delta {
        return false;
    }

    // Big profit + high price = take it
    if delta > 0.30 && best_bid >= 0.80 {
        return true;
    }

    // Tiered thresholds based on entry
    let threshold = if entry_price < 0.30 {
        0.90
    } else if entry_price < 0.50 {
        0.85
    } else if entry_price < 0.70 {
        0.88
    } else {
        0.93
    };

    // Draw markets: slightly tighter thresholds (draws are more volatile)
    let threshold = if market_type == "draw" {
        (threshold - 0.03_f64).max(0.80)
    } else {
        threshold
    };

    best_bid >= threshold
}

/// Log take-profit decision to separate JSONL file for self-improvement loop.
fn log_tp_decision(
    condition_id: &str, title: &str, market_type: &str,
    entry_price: f64, best_bid: f64, delta: f64,
    decision: &str, sell_price: Option<f64>, profit_usdc: Option<f64>, roi_pct: Option<f64>,
) {
    use std::io::Write;
    let path = std::path::Path::new("/opt/bottie/data/take_profit_log.jsonl");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let entry = serde_json::json!({
        "timestamp": Utc::now().to_rfc3339(),
        "condition_id": condition_id,
        "market_title": title,
        "market_type": market_type,
        "entry_price": entry_price,
        "best_bid": best_bid,
        "delta": delta,
        "decision": decision,
        "sell_price": sell_price,
        "profit_usdc": profit_usdc,
        "roi_pct": roi_pct,
    });
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{}", entry);
    }
}

async fn take_profit_check(
    client: &ClobClient,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
    config: &crate::config::TakeProfitConfig,
    cooldown: &mut HashMap<String, DateTime<Utc>>,
) {
    if !config.enabled {
        return;
    }

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
        let condition_id = trades[idx].condition_id.clone();

        // Skip if we recently failed a sell on this token (5 min cooldown)
        if let Some(last_attempt) = cooldown.get(&token_id) {
            if Utc::now().signed_duration_since(*last_attempt) < chrono::Duration::minutes(5) {
                continue;
            }
        }

        // Detect market type from title
        let market_type = if title.to_lowercase().contains("draw") {
            "draw"
        } else {
            "moneyline"
        };

        // Check best bid
        let best_bid = match client.get_best_bid(&token_id).await {
            Ok(b) => b,
            Err(_) => continue,
        };

        let delta = best_bid - entry_price;

        if !should_take_profit(entry_price, best_bid, market_type, config.safety_threshold, config.min_delta) {
            continue;
        }

        // Place GTC sell at best_bid for maker rebate
        let sell_price = best_bid;

        info!(
            "TAKE PROFIT: {} | bid={:.0}ct (entry {:.0}ct, delta={:.0}ct) | selling {:.1} shares @ {:.1}ct",
            &title[..title.len().min(50)], best_bid * 100.0, entry_price * 100.0,
            delta * 100.0, shares, sell_price * 100.0
        );

        let fee_bps = client.get_fee_rate_bps(&token_id).await.unwrap_or(0);

        match client.create_and_post_order(
            &token_id,
            sell_price,
            shares,
            crate::clob::client::Side::Sell,
            crate::clob::client::OrderType::GTC,
            fee_bps,
        ).await {
            Ok(resp) => {
                if resp.is_filled() {
                    let actual_shares = { let fs = resp.filled_size(); if fs > 0.0 { fs } else { shares } };
                    let sell_usdc = actual_shares * sell_price;
                    let fee_cost = sell_usdc * (fee_bps as f64 / 10000.0);
                    let profit = sell_usdc - fee_cost - trades[idx].size_usdc;
                    let roi = profit / trades[idx].size_usdc * 100.0;
                    let wallet = trades[idx].copy_wallet.clone().unwrap_or_default();
                    let sport = trades[idx].sport.clone();
                    trades[idx].result = Some("take_profit".to_string());
                    trades[idx].pnl = Some(profit);
                    trades[idx].actual_pnl = Some(profit);
                    trades[idx].sell_price = Some(sell_price);
                    trades[idx].exit_type = Some("take_profit".to_string());
                    trades[idx].resolved_at = Some(Utc::now());
                    sold_count += 1;
                    total_profit += profit;
                    sold_meta.push((wallet, sport, profit));

                    info!(
                        "TAKE PROFIT FILLED: {} | profit=${:.2} ({:.0}%) | sell={:.0}ct",
                        &title[..title.len().min(40)], profit, roi, sell_price * 100.0
                    );

                    log_tp_decision(
                        &condition_id, &title, market_type,
                        entry_price, best_bid, delta,
                        "SELL", Some(sell_price), Some(profit), Some(roi),
                    );
                } else {
                    warn!("TAKE PROFIT NOT FILLED: {} (bid may have moved)", &title[..title.len().min(40)]);
                    cooldown.insert(token_id.clone(), Utc::now());
                    log_tp_decision(
                        &condition_id, &title, market_type,
                        entry_price, best_bid, delta,
                        "SELL_MISSED", None, None, None,
                    );
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

/// Sync trade log with CLOB order status. Uses the order status API
/// instead of positions API timing heuristics.
///
/// ORDER_STATUS_LIVE → order still in book, wait
/// ORDER_STATUS_MATCHED → truly filled, verify on PM positions
/// ORDER_STATUS_CANCELED / INVALID / CANCELED_MARKET_RESOLVED → phantom
async fn sync_phantoms(
    client: &ClobClient,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
) {
    let mut trades = logger.load_all();
    let mut phantom_count = 0u32;
    let mut sold_count = 0u32;
    let mut live_count = 0u32;

    for trade in trades.iter_mut() {
        if trade.result.is_some() || !trade.filled || trade.dry_run {
            continue;
        }

        let trade_age_mins = Utc::now()
            .signed_duration_since(trade.timestamp)
            .num_minutes();

        // Skip very fresh trades — give CLOB time to process
        if trade_age_mins < 2 {
            continue;
        }

        // Check order status via CLOB API
        let order_id = match &trade.order_id {
            Some(id) if !id.is_empty() => id.clone(),
            _ => continue, // no order_id, can't check
        };

        let (status, _size_matched) = match client.get_order_status(&order_id).await {
            Ok(s) => s,
            Err(_) => continue, // API error, skip this cycle
        };

        match status.as_str() {
            "ORDER_STATUS_LIVE" => {
                // Order still in the book, waiting to fill. Not a phantom.
                live_count += 1;

                // But if it's been >6h and still live, cancel it — market has moved on
                if trade_age_mins > 360 {
                    info!("STALE ORDER: {} | {}min old, still LIVE — marking phantom",
                        &trade.market_title[..trade.market_title.len().min(40)], trade_age_mins);
                    trade.result = Some("phantom".to_string());
                    trade.pnl = Some(0.0);
                    trade.actual_pnl = Some(0.0);
                    trade.exit_type = Some("phantom".to_string());
                    trade.resolved_at = Some(Utc::now());
                    phantom_count += 1;
                    // TODO: cancel the order via CLOB API
                }
            }
            "ORDER_STATUS_MATCHED" => {
                // Truly filled. But if not on PM positions after >2h, user manually sold.
                if trade_age_mins > 120 {
                    let funder = client.funder_address();
                    let our_positions = client.get_wallet_positions(&funder, 500).await.unwrap_or_default();
                    let key = format!("{}:{}", trade.condition_id, trade.outcome);
                    let held = our_positions.iter().any(|p| p.position_key() == key && p.size_f64() > 0.01);

                    if !held {
                        // Matched but not on PM → resolved or manually sold
                        let winner = client.get_market_info(&trade.condition_id).await
                            .ok()
                            .flatten()
                            .and_then(|m| m.winning_outcome());

                        let (result, pnl, sell_price, exit_type) = if let Some(w) = winner {
                            let won = trade.outcome.to_lowercase() == w.to_lowercase();
                            if won {
                                let p = trade.size_shares * (1.0 - trade.price);
                                ("win".to_string(), p, 1.0f64, "resolution".to_string())
                            } else {
                                ("loss".to_string(), -trade.size_usdc, 0.0f64, "resolution".to_string())
                            }
                        } else {
                            let (p, sp) = if !trade.token_id.is_empty() {
                                match client.get_best_bid(&trade.token_id).await {
                                    Ok(bid) if bid > 0.0 => {
                                        (bid * trade.size_shares - trade.size_usdc, bid)
                                    }
                                    _ => (0.0, 0.0),
                                }
                            } else {
                                (0.0, 0.0)
                            };
                            ("sold".to_string(), p, sp, "manual".to_string())
                        };

                        trade.sell_price = Some(sell_price);
                        trade.result = Some(result);
                        trade.pnl = Some(pnl);
                        trade.actual_pnl = Some(pnl);
                        trade.exit_type = Some(exit_type);
                        trade.resolved_at = Some(Utc::now());
                        sold_count += 1;
                    }
                }
            }
            _ => {
                // CANCELED, INVALID, CANCELED_MARKET_RESOLVED → phantom
                info!("PHANTOM (order {}): {} | status={}",
                    &order_id[..order_id.len().min(12)],
                    &trade.market_title[..trade.market_title.len().min(40)],
                    status);
                trade.result = Some("phantom".to_string());
                trade.pnl = Some(0.0);
                trade.actual_pnl = Some(0.0);
                trade.exit_type = Some("phantom".to_string());
                trade.resolved_at = Some(Utc::now());
                phantom_count += 1;
            }
        }

        // Rate limit: don't hammer the CLOB API
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    if phantom_count > 0 || sold_count > 0 {
        logger.rewrite_all(trades);
        let mut r = risk.write().await;
        for _ in 0..(phantom_count + sold_count) {
            r.record_trade_closed_with_context(0.0, None, "");
        }
        if phantom_count > 0 {
            info!("phantom-sync: marked {} orders as phantom (CANCELED/INVALID)", phantom_count);
        }
        if sold_count > 0 {
            info!("phantom-sync: marked {} orders as sold (MATCHED but not held)", sold_count);
        }
        if live_count > 0 {
            info!("phantom-sync: {} orders still LIVE in orderbook", live_count);
        }
    }
}

