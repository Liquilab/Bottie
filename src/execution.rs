use std::sync::Arc;

use anyhow::Result;
use tracing::{info, warn};

use crate::clob::client::{ClobClient, OrderType, Side};
use crate::config::SharedConfig;
use crate::logger::{TradeLog, TradeLogger};
use crate::risk::{RiskDecision, RiskManager};
use crate::signal::{AggregatedSignal, SignalSource};
use crate::sizing;

pub struct Executor {
    client: Arc<ClobClient>,
    config: SharedConfig,
    fee_cache: std::collections::HashMap<String, u32>,
    /// Tracks condition_id:outcome pairs we've already attempted this session.
    /// Once a conditionId+outcome+side is attempted, it is NEVER retried.
    /// This prevents duplicate orders from repeated signals (Cannae bijkopen,
    /// bot restarts, etc).
    attempted: std::collections::HashSet<String>,
}

impl Executor {
    pub fn new(client: Arc<ClobClient>, config: SharedConfig) -> Self {
        Self {
            client,
            config,
            fee_cache: std::collections::HashMap::new(),
            attempted: std::collections::HashSet::new(),
        }
    }

    /// Seed attempted map from live PM positions (source of truth).
    /// Only positions we actually HOLD block new orders.
    /// trades.jsonl is NOT used — sold/resolved positions must not block new entries.
    pub fn seed_from_positions(&mut self, positions: &[crate::clob::types::WalletPosition]) {
        let mut count = 0;
        for pos in positions {
            if pos.size_f64() < 0.01 {
                continue;
            }
            let cid = pos.condition_id.as_deref().unwrap_or("");
            let outcome = pos.outcome.as_deref().unwrap_or("");
            if cid.is_empty() {
                continue;
            }
            let key = Self::attempt_key(cid, outcome, "BUY");
            if self.attempted.insert(key) {
                count += 1;
            }
        }
        if count > 0 {
            tracing::info!("seeded {} PM positions into attempted map (total: {})", count, self.attempted.len());
        }
    }

    fn attempt_key(condition_id: &str, outcome: &str, side: &str) -> String {
        format!("{}:{}:{}", condition_id, outcome.to_lowercase(), side)
    }

    /// Seed attempted map from trades.jsonl so historical trades survive restarts.
    /// The PM Data API inconsistently returns positions, so this is the belt to
    /// PM-seeding's suspenders.
    pub fn seed_from_trade_log(&mut self, path: &std::path::Path) {
        use std::io::BufRead;

        let file = match std::fs::File::open(path) {
            Ok(f) => f,
            Err(e) => {
                warn!("could not open {}: {} — skipping trade log seed", path.display(), e);
                return;
            }
        };

        let reader = std::io::BufReader::new(file);
        let mut added = 0u32;

        for line in reader.lines() {
            let line = match line {
                Ok(l) if !l.trim().is_empty() => l,
                _ => continue,
            };
            let trade: serde_json::Value = match serde_json::from_str(&line) {
                Ok(v) => v,
                Err(_) => continue,
            };

            // Skip phantom fills
            if trade.get("result").and_then(|v| v.as_str()) == Some("phantom") {
                continue;
            }

            let condition_id = trade.get("condition_id").and_then(|v| v.as_str()).unwrap_or("");
            let outcome = trade.get("outcome").and_then(|v| v.as_str()).unwrap_or("");
            let side = trade.get("side").and_then(|v| v.as_str()).unwrap_or("BUY");

            if !condition_id.is_empty() && !outcome.is_empty() {
                let key = Self::attempt_key(condition_id, outcome, side);
                if self.attempted.insert(key) {
                    added += 1;
                }
            }
        }

        info!("seeded {} entries from trades.jsonl into attempted map (total: {})", added, self.attempted.len());
    }

    /// Execute with game context: proportional shares sizing using Cannae's game total
    pub async fn execute_with_game_context(
        &mut self,
        signal: &AggregatedSignal,
        risk: &mut RiskManager,
        logger: &TradeLogger,
        cannae_game_total_usdc: f64,
    ) -> Result<bool> {
        self.execute_inner(signal, risk, logger, cannae_game_total_usdc, false).await
    }

    /// Execute with game context and taker mode (FOK on ask price)
    pub async fn execute_with_game_context_taker(
        &mut self,
        signal: &AggregatedSignal,
        risk: &mut RiskManager,
        logger: &TradeLogger,
        cannae_game_total_usdc: f64,
        taker_mode: bool,
    ) -> Result<bool> {
        self.execute_inner(signal, risk, logger, cannae_game_total_usdc, taker_mode).await
    }

    /// Execute a signal: size it, risk-check it, place the order
    pub async fn execute(
        &mut self,
        signal: &AggregatedSignal,
        risk: &mut RiskManager,
        logger: &TradeLogger,
    ) -> Result<bool> {
        self.execute_inner(signal, risk, logger, 0.0, false).await
    }

    async fn execute_inner(
        &mut self,
        signal: &AggregatedSignal,
        risk: &mut RiskManager,
        logger: &TradeLogger,
        cannae_game_total_usdc: f64,
        taker_mode: bool,
    ) -> Result<bool> {
        // Read config values we need, then drop the lock immediately (N6: avoid holding
        // RwLock across HTTP await points which blocks config hot-reload)
        let (sizing_config, is_dry_run, strategy_version, max_resolution_days) = {
            let config = self.config.read().await;
            (
                config.sizing.clone(),
                self.client.is_dry_run(),
                config.autoresearch_params.current_strategy_version.clone(),
                config.copy_trading.max_resolution_days,
            )
        };

        // Determine side
        let side = if signal.side == "BUY" {
            Side::Buy
        } else {
            Side::Sell
        };

        // Block crypto 5m/15m markets — pure noise, not tradeable at our size
        {
            let title = signal.market_title.to_lowercase();
            if title.contains("up or down") {
                return Ok(false);
            }
        }

        // Price boundary filter — Cannae's sweet spot is 25-65ct
        {
            let sizing_config = &self.config.read().await.sizing;
            if signal.price > sizing_config.max_price {
                info!(
                    "SKIP: price {:.0}ct > max {:.0}ct for {}",
                    signal.price * 100.0, sizing_config.max_price * 100.0, signal.market_title
                );
                return Ok(false);
            }
        }

        // Dedup against LIVE PM positions (source of truth, not trade log).
        // Two checks in one API call:
        // 1. condition_id: blocks exact duplicates + contradictions
        // 2. event_slug + market_type: blocks same-type bets on same event
        //    (e.g. ORL -13.5 spread + ORL -12.5 spread = blocked)
        //    But spread + ou on same event = allowed (different market_type)
        {
            let funder = self.client.funder_address();
            let signal_market_type = crate::copy_trader::CopyTrader::detect_market_type(&signal.market_title);
            match self.client.get_wallet_positions(&funder, 500).await {
                Ok(positions) => {
                    for pos in &positions {
                        if pos.size_f64() < 0.01 {
                            continue;
                        }
                        let pos_cid = pos.condition_id.as_deref().unwrap_or("");

                        // Check 1: exact condition_id match
                        if pos_cid == signal.condition_id {
                            let pos_outcome = pos.outcome.as_deref().unwrap_or("");
                            if pos_outcome.to_lowercase() == signal.outcome.to_lowercase() {
                                info!(
                                    "SKIP: already open position on {} {}",
                                    signal.market_title, signal.outcome
                                );
                            } else {
                                info!(
                                    "SKIP: contradictory bet on {} (have {}, signal {})",
                                    signal.market_title, pos_outcome, signal.outcome
                                );
                            }
                            return Ok(false);
                        }

                        // Check 2 (RUS-283): event_slug + market_type dedup
                        let pos_event = pos.event_slug.as_deref()
                            .or(pos.slug.as_deref())
                            .unwrap_or("")
                            .trim_end_matches("-more-markets");
                        if !signal.event_slug.is_empty()
                            && !pos_event.is_empty()
                            && pos_event == signal.event_slug
                        {
                            let pos_title = pos.title.as_deref().unwrap_or("");
                            let pos_mt = crate::copy_trader::CopyTrader::detect_market_type(pos_title);
                            if pos_mt == signal_market_type {
                                info!(
                                    "SKIP DEDUP: already have {} bet on {} (have: {}, signal: {})",
                                    signal_market_type, signal.event_slug,
                                    pos_title, signal.market_title
                                );
                                return Ok(false);
                            }
                        }
                    }
                }
                Err(e) => {
                    warn!("PM position check failed ({}), skipping trade for safety", e);
                    return Ok(false);
                }
            }
        }

        // Skip if we already attempted this market+outcome this session (permanent, never retries)
        let attempt_key = Self::attempt_key(&signal.condition_id, &signal.outcome, &signal.side);
        if !self.attempted.insert(attempt_key) {
            // Already in the set → we've attempted this before → skip
            return Ok(false);
        }

        // Fetch current ASK from orderbook. NEVER use stale signal.price.
        let exec_price = if !signal.token_id.is_empty() {
            match self.client.get_best_ask(&signal.token_id).await {
                Ok(ask) if ask > 0.0 && ask < 1.0 => {
                    if ask > signal.price * 1.25 {
                        info!(
                            "SKIP: price moved too much for {} (was {:.0}ct, now {:.0}ct)",
                            signal.market_title,
                            signal.price * 100.0,
                            ask * 100.0
                        );
                        return Ok(false);
                    }
                    // Buy 1ct above best ask to sweep deeper into the book.
                    // FOK on exact ask fails when top-of-book is thin.
                    (ask + 0.01_f64).min(0.99)
                }
                Ok(ask) => {
                    warn!(
                        "SKIP: invalid ASK {:.2} for {} — orderbook empty or resolved",
                        ask, signal.market_title
                    );
                    return Ok(false);
                }
                Err(e) => {
                    warn!(
                        "SKIP: orderbook fetch failed for {}: {} — refusing stale price",
                        signal.market_title, e
                    );
                    return Ok(false);
                }
            }
        } else {
            warn!("SKIP: no token_id for {} — cannot fetch ASK", signal.market_title);
            return Ok(false);
        };

        // --- Check market resolution time (capital efficiency) ---
        if max_resolution_days > 0 && !signal.condition_id.is_empty() {
            match self.client.get_market_info(&signal.condition_id).await {
                Ok(Some(market)) => {
                    if let Some(end_date_str) = &market.end_date {
                        if let Ok(end_date) = chrono::DateTime::parse_from_rfc3339(end_date_str)
                            .or_else(|_| chrono::DateTime::parse_from_str(end_date_str, "%Y-%m-%dT%H:%M:%S%.fZ"))
                        {
                            let until_end = end_date.signed_duration_since(chrono::Utc::now());
                            let days_until = until_end.num_days();
                            if days_until > max_resolution_days as i64 {
                                info!(
                                    "SKIP: {} resolves in {} days (max {})",
                                    signal.market_title, days_until, max_resolution_days
                                );
                                return Ok(false);
                            }
                            // Don't enter markets ending within 30 minutes — edge is gone,
                            // risk of last-minute reversal is highest
                            if until_end.num_minutes() < 30 {
                                let mins = until_end.num_minutes();
                                if mins < 0 {
                                    info!(
                                        "SKIP: {} already ended {}min ago — market resolved",
                                        signal.market_title, mins.abs()
                                    );
                                } else {
                                    info!(
                                        "SKIP: {} ends in {}min — too close to resolution",
                                        signal.market_title, mins
                                    );
                                }
                                return Ok(false);
                            }
                        }
                    }
                }
                _ => {} // If we can't fetch market info, proceed anyway
            }
        }

        // Size the trade (using exec_price for accurate Kelly and edge)
        let exec_edge_pct = if signal.combined_confidence > 0.5 && exec_price > 0.0 {
            (signal.combined_confidence - exec_price) / exec_price * 100.0
        } else {
            0.0
        };
        let signal_at_exec = AggregatedSignal {
            price: exec_price,
            edge_pct: exec_edge_pct,
            ..signal.clone()
        };

        let is_copy = signal.sources.iter().any(|s| matches!(s, SignalSource::Copy(_)));
        let size = if is_copy && cannae_game_total_usdc > 0.0 {
            sizing::copy_trade_size(risk.bankroll(), &signal_at_exec, &sizing_config, cannae_game_total_usdc)
        } else if is_copy {
            // Fallback: no game context, use old kelly sizing
            sizing::kelly_size(risk.bankroll(), &signal_at_exec, &sizing_config)
        } else {
            sizing::kelly_size(risk.bankroll(), &signal_at_exec, &sizing_config)
        };

        if size <= 0.0 {
            info!(
                "SKIP: size=0 for {} (bankroll=${:.2}, price={:.2})",
                signal.market_title, risk.bankroll(), exec_price
            );
            return Ok(false);
        }

        let size_usdc = size * exec_price;

        // Risk check (with wallet/sport concentration limits)
        let (signal_source_str, copy_wallet_val, consensus_count_val, consensus_wallets_val, signal_delay_ms_val) =
            extract_signal_meta(&signal.sources);
        match risk.check_trade_with_context(
            size_usdc,
            copy_wallet_val.as_deref(),
            &signal.sport,
        ) {
            RiskDecision::Allowed => {}
            RiskDecision::Rejected(reason) => {
                warn!("RISK REJECTED: {} - {}", signal.market_title, reason);
                return Ok(false);
            }
        }

        // Get fee rate (cached). Don't cache failures — 0 from error would
        // permanently block markets that require non-zero fees.
        let fee_bps = match self.fee_cache.get(&signal.token_id) {
            Some(bps) => *bps,
            None => {
                match self.client.get_fee_rate_bps(&signal.token_id).await {
                    Ok(bps) => {
                        self.fee_cache.insert(signal.token_id.clone(), bps);
                        bps
                    }
                    Err(e) => {
                        warn!("fee-rate lookup failed for {}: {} — using 0, will retry on error", signal.market_title, e);
                        0 // Don't cache — next attempt will retry the API
                    }
                }
            }
        };

        // Use already-extracted signal metadata
        let signal_source = signal_source_str;
        let copy_wallet = copy_wallet_val;
        let consensus_count = consensus_count_val;
        let consensus_wallets = consensus_wallets_val;
        let signal_delay_ms = signal_delay_ms_val;

        // Dry run check
        if is_dry_run {
            info!(
                "DRY RUN: {} {} {:.0} shares @ {:.3} = ${:.2} | edge={:.1}% conf={:.2} | {}",
                side,
                signal.market_title,
                size,
                exec_price,
                size_usdc,
                signal.edge_pct,
                signal.combined_confidence,
                signal_source
            );

            logger.log(TradeLog {
                timestamp: chrono::Utc::now(),
                token_id: signal.token_id.clone(),
                condition_id: signal.condition_id.clone(),
                market_title: signal.market_title.clone(),
                sport: signal.sport.clone(),
                side: signal.side.clone(),
                outcome: signal.outcome.clone(),
                event_slug: Some(signal.event_slug.clone()).filter(|s| !s.is_empty()),
                price: exec_price,
                size_usdc,
                size_shares: size,
                signal_source: signal_source.clone(),
                copy_wallet,
                consensus_count,
                consensus_wallets: consensus_wallets.clone(),
                edge_pct: exec_edge_pct,
                confidence: signal.combined_confidence,
                signal_delay_ms,
                order_id: None,
                filled: true,
                dry_run: true,
                result: None,
                pnl: None,
                resolved_at: None,
                sell_price: None,
                actual_pnl: None,
                exit_type: None,
                strategy_version: strategy_version.clone(),
            });

            return Ok(true);
        }

        // Place order: FOK for taker mode (immediate fill), GTC for maker mode (sits in book)
        let order_type = if taker_mode { OrderType::FOK } else { OrderType::GTC };
        let mode_label = if taker_mode { "TAKER" } else { "MAKER" };
        info!(
            "EXECUTE [{}]: {} {} {:.0} shares @ {:.3} = ${:.2} | edge={:.1}% | {}",
            mode_label, side, signal.market_title, size, exec_price, size_usdc, signal.edge_pct, signal_source
        );

        let mut actual_fee = fee_bps;
        let resp = match self
            .client
            .create_and_post_order(&signal.token_id, exec_price, size, side, order_type, fee_bps)
            .await
        {
            Ok(r) => r,
            Err(e) => {
                let err_str = e.to_string();
                // Retry with correct fee if market rejects our fee rate
                // Use short pattern to avoid ASCII vs Unicode apostrophe mismatch
                if let Some(idx) = err_str.find("taker fee: ")
                    .or_else(|| err_str.find("maker fee: "))
                {
                    let fee_str = &err_str[idx + 11..];
                    if let Some(end) = fee_str.find(|c: char| !c.is_ascii_digit()) {
                        if let Ok(correct_fee) = fee_str[..end].parse::<u32>() {
                            info!("retrying with fee={} (was {})", correct_fee, fee_bps);
                            actual_fee = correct_fee;
                            self.fee_cache.insert(signal.token_id.clone(), correct_fee);
                            self.client
                                .create_and_post_order(&signal.token_id, exec_price, size, side, order_type, correct_fee)
                                .await?
                        } else {
                            return Err(e);
                        }
                    } else if let Ok(correct_fee) = fee_str.trim_end_matches(|c: char| !c.is_ascii_digit()).parse::<u32>() {
                        info!("retrying with fee={} (was {})", correct_fee, fee_bps);
                        actual_fee = correct_fee;
                        self.fee_cache.insert(signal.token_id.clone(), correct_fee);
                        self.client
                            .create_and_post_order(&signal.token_id, exec_price, size, side, order_type, correct_fee)
                            .await?
                    } else {
                        return Err(e);
                    }
                } else {
                    return Err(e);
                }
            }
        };
        let _ = actual_fee; // used for cache update above

        let order_id = resp.effective_id().map(|s| s.to_string());
        let mut filled = resp.is_filled();
        // Use actual filled size from exchange, not requested size
        let actual_shares = if filled { let fs = resp.filled_size(); if fs > 0.0 { fs } else { size } } else { 0.0 };
        let actual_usdc = if filled { actual_shares * exec_price } else { 0.0 };

        // GTC orders sit in the orderbook and may not fill instantly.
        // Trust the CLOB response (is_filled checks size_matched > 0).
        // The resolver phantom-sync (every 5 min) catches false positives
        // by checking actual PM positions.

        if filled {
            risk.record_trade_opened_with_context(
                actual_usdc,
                copy_wallet.as_deref(),
                &signal.sport,
            );
            info!(
                "FILLED: {} | order_id={} | {:.1} shares @ {:.2}ct = ${:.2}",
                signal.market_title,
                order_id.as_deref().unwrap_or("?"),
                actual_shares,
                exec_price * 100.0,
                actual_usdc,
            );
        } else {
            let reason = resp.skipped.as_deref().unwrap_or(
                resp.error_msg.as_deref().unwrap_or("unknown")
            );
            warn!("NOT FILLED: {} | reason={}", signal.market_title, reason);
        }

        // Only log filled trades (don't pollute log with unfilled attempts)
        if filled {
            logger.log(TradeLog {
                timestamp: chrono::Utc::now(),
                token_id: signal.token_id.clone(),
                condition_id: signal.condition_id.clone(),
                market_title: signal.market_title.clone(),
                sport: signal.sport.clone(),
                side: signal.side.clone(),
                outcome: signal.outcome.clone(),
                event_slug: Some(signal.event_slug.clone()).filter(|s| !s.is_empty()),
                price: exec_price,
                size_usdc: actual_usdc,
                size_shares: actual_shares,
                signal_source,
                copy_wallet,
                consensus_count,
                consensus_wallets: consensus_wallets.clone(),
                edge_pct: exec_edge_pct,
                confidence: signal.combined_confidence,
                signal_delay_ms,
                order_id,
                filled: true,
                dry_run: false,
                result: None,
                pnl: None,
                resolved_at: None,
                sell_price: None,
                actual_pnl: None,
                exit_type: None,
                strategy_version,
            });
        }

        Ok(filled)
    }
}

fn extract_signal_meta(sources: &[SignalSource]) -> (String, Option<String>, Option<u32>, Option<Vec<String>>, u64) {
    for source in sources {
        if let SignalSource::Copy(copy) = source {
            let wallets = if copy.consensus_wallets.is_empty() {
                None
            } else {
                Some(copy.consensus_wallets.clone())
            };
            return (
                "copy".to_string(),
                Some(copy.source_wallet.clone()),
                Some(copy.consensus_count),
                wallets,
                copy.signal_delay_ms,
            );
        }
    }
    for source in sources {
        if let SignalSource::OddsArb(arb) = source {
            return (format!("odds_arb:{}", arb.bookmaker), None, None, None, 0);
        }
    }
    ("unknown".to_string(), None, None, None, 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seed_from_trade_log_blocks_duplicates() {
        let tmp = std::env::temp_dir().join("test_trades.jsonl");
        std::fs::write(&tmp, r#"{"condition_id":"0xabc123","outcome":"No","side":"BUY","result":"win"}
{"condition_id":"0xdef456","outcome":"Yes","side":"BUY","result":"loss"}
{"condition_id":"0xphantom","outcome":"No","side":"BUY","result":"phantom"}
"#).unwrap();

        let mut attempted: std::collections::HashSet<String> = std::collections::HashSet::new();

        // Simulate seed_from_trade_log inline (can't construct Executor without ClobClient)
        use std::io::BufRead;
        let file = std::fs::File::open(&tmp).unwrap();
        let reader = std::io::BufReader::new(file);
        let mut added = 0u32;
        for line in reader.lines() {
            let line = match line { Ok(l) if !l.trim().is_empty() => l, _ => continue };
            let trade: serde_json::Value = match serde_json::from_str(&line) { Ok(v) => v, Err(_) => continue };
            if trade.get("result").and_then(|v| v.as_str()) == Some("phantom") { continue; }
            let cid = trade.get("condition_id").and_then(|v| v.as_str()).unwrap_or("");
            let outcome = trade.get("outcome").and_then(|v| v.as_str()).unwrap_or("");
            let side = trade.get("side").and_then(|v| v.as_str()).unwrap_or("BUY");
            if !cid.is_empty() && !outcome.is_empty() {
                let key = Executor::attempt_key(cid, outcome, side);
                if attempted.insert(key) { added += 1; }
            }
        }

        assert_eq!(added, 2, "should seed 2 non-phantom trades");
        assert!(attempted.contains("0xabc123:no:BUY"), "should contain abc123");
        assert!(attempted.contains("0xdef456:yes:BUY"), "should contain def456");
        assert!(!attempted.contains("0xphantom:no:BUY"), "should NOT contain phantom");

        std::fs::remove_file(&tmp).ok();
    }
}
