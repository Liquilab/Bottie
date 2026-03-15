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
    /// Prevents retrying failed/unfilled orders on every poll cycle.
    attempted: std::collections::HashMap<String, std::time::Instant>,
}

impl Executor {
    pub fn new(client: Arc<ClobClient>, config: SharedConfig) -> Self {
        Self {
            client,
            config,
            fee_cache: std::collections::HashMap::new(),
            attempted: std::collections::HashMap::new(),
        }
    }

    fn attempt_key(condition_id: &str, outcome: &str, side: &str) -> String {
        format!("{}:{}:{}", condition_id, outcome.to_lowercase(), side)
    }

    /// Execute a signal: size it, risk-check it, place the order
    pub async fn execute(
        &mut self,
        signal: &AggregatedSignal,
        risk: &mut RiskManager,
        logger: &TradeLogger,
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

        // Position deduplication: skip if we already have this exact position
        if !signal.outcome.is_empty()
            && logger.has_open_position(&signal.condition_id, &signal.outcome)
        {
            info!(
                "SKIP: already open position on {} {}",
                signal.market_title, signal.outcome
            );
            return Ok(false);
        }

        // Event deduplication: skip conflicting moneyline bets on same event.
        // Spread and O/U are allowed alongside moneyline on same event.
        if !signal.event_slug.is_empty()
            && logger.has_conflicting_event(&signal.event_slug, &signal.market_title)
        {
            info!(
                "SKIP: conflicting moneyline on event {} ({})",
                signal.event_slug, signal.market_title
            );
            return Ok(false);
        }

        // Skip if we already attempted this market+outcome this session (5 min cooldown)
        let attempt_key = Self::attempt_key(&signal.condition_id, &signal.outcome, &signal.side);
        if let Some(last) = self.attempted.get(&attempt_key) {
            if last.elapsed() < std::time::Duration::from_secs(300) {
                return Ok(false);
            }
        }
        self.attempted.insert(attempt_key, std::time::Instant::now());

        // Fetch current market price from orderbook.
        // Skip if price moved >25% against us since the wallet bought.
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
                    ask
                }
                _ => signal.price, // fall back to signal price if orderbook unavailable or invalid
            }
        } else {
            signal.price
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
                                info!(
                                    "SKIP: {} ends in {}min — too close to resolution",
                                    signal.market_title, until_end.num_minutes()
                                );
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
        let size = if is_copy {
            sizing::copy_trade_size(risk.bankroll(), &signal_at_exec, &sizing_config)
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
        let (signal_source_str, copy_wallet_val, consensus_count_val, signal_delay_ms_val) =
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

        // Get fee rate (cached)
        let fee_bps = match self.fee_cache.get(&signal.token_id) {
            Some(bps) => *bps,
            None => {
                let bps = self
                    .client
                    .get_fee_rate_bps(&signal.token_id)
                    .await
                    .unwrap_or(0);
                self.fee_cache.insert(signal.token_id.clone(), bps);
                bps
            }
        };

        // Use already-extracted signal metadata
        let signal_source = signal_source_str;
        let copy_wallet = copy_wallet_val;
        let consensus_count = consensus_count_val;
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
                edge_pct: exec_edge_pct,
                confidence: signal.combined_confidence,
                signal_delay_ms,
                order_id: None,
                filled: true,
                dry_run: true,
                result: None,
                pnl: None,
                resolved_at: None,
                strategy_version: strategy_version.clone(),
            });

            return Ok(true);
        }

        // Place FOK order (immediate fill or reject — no hanging orders)
        info!(
            "EXECUTE: {} {} {:.0} shares @ {:.3} = ${:.2} | edge={:.1}% | {}",
            side, signal.market_title, size, exec_price, size_usdc, signal.edge_pct, signal_source
        );

        let mut actual_fee = fee_bps;
        let resp = match self
            .client
            .create_and_post_order(&signal.token_id, exec_price, size, side, OrderType::FOK, fee_bps)
            .await
        {
            Ok(r) => r,
            Err(e) => {
                let err_str = e.to_string();
                // Retry with correct fee if market rejects our fee rate
                if let Some(idx) = err_str.find("current market's taker fee: ") {
                    let fee_str = &err_str[idx + 28..];
                    if let Some(end) = fee_str.find(|c: char| !c.is_ascii_digit()) {
                        if let Ok(correct_fee) = fee_str[..end].parse::<u32>() {
                            info!("retrying with fee={} (was {})", correct_fee, fee_bps);
                            actual_fee = correct_fee;
                            self.fee_cache.insert(signal.token_id.clone(), correct_fee);
                            self.client
                                .create_and_post_order(&signal.token_id, exec_price, size, side, OrderType::FOK, correct_fee)
                                .await?
                        } else {
                            return Err(e);
                        }
                    } else if let Ok(correct_fee) = fee_str.trim_end_matches(|c: char| !c.is_ascii_digit()).parse::<u32>() {
                        info!("retrying with fee={} (was {})", correct_fee, fee_bps);
                        actual_fee = correct_fee;
                        self.fee_cache.insert(signal.token_id.clone(), correct_fee);
                        self.client
                            .create_and_post_order(&signal.token_id, exec_price, size, side, OrderType::FOK, correct_fee)
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
        let filled = resp.is_filled();
        // Use actual filled size from exchange, not requested size
        let actual_shares = if filled { let fs = resp.filled_size(); if fs > 0.0 { fs } else { size } } else { 0.0 };
        let actual_usdc = if filled { actual_shares * exec_price } else { 0.0 };

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
                edge_pct: exec_edge_pct,
                confidence: signal.combined_confidence,
                signal_delay_ms,
                order_id,
                filled: true,
                dry_run: false,
                result: None,
                pnl: None,
                resolved_at: None,
                strategy_version,
            });
        }

        Ok(filled)
    }
}

fn extract_signal_meta(sources: &[SignalSource]) -> (String, Option<String>, Option<u32>, u64) {
    for source in sources {
        if let SignalSource::Copy(copy) = source {
            return (
                "copy".to_string(),
                Some(copy.source_wallet.clone()),
                Some(copy.consensus_count),
                copy.signal_delay_ms,
            );
        }
    }
    for source in sources {
        if let SignalSource::OddsArb(arb) = source {
            return (format!("odds_arb:{}", arb.bookmaker), None, None, 0);
        }
    }
    ("unknown".to_string(), None, None, 0)
}
