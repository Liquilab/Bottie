use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

use crate::clob::client::{ClobClient, OrderType, Side};
use crate::config::SharedConfig;
use crate::logger::TradeLogger;
use crate::risk::RiskManager;
use crate::scheduler::{self, GameSchedule};

// ── Pair state ──────────────────────────────────────────────────────

/// Two-phase spread pair inspired by RN1's market-making approach:
/// Phase 1 (pre-game): buy the cheap side (underdog YES) via limit order
/// Phase 2 (in-game):  monitor the other side, buy when it dips → combined < $1
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpreadPair {
    pub condition_id: String,
    pub event_slug: String,
    pub title: String,
    /// The cheap token we buy first (underdog YES).
    pub leg1_token_id: String,
    pub leg1_outcome: String,
    pub leg1_price: f64,
    pub leg1_order_id: Option<String>,
    pub leg1_filled: f64,
    /// The expensive token we buy second when it dips.
    pub leg2_token_id: String,
    pub leg2_outcome: String,
    pub leg2_price: f64,      // 0 until leg2 is placed
    pub leg2_order_id: Option<String>,
    pub leg2_filled: f64,
    pub target_shares: f64,
    /// Max price we'd pay for leg2 to keep combined < max_combined.
    pub leg2_max_price: f64,
    pub status: PairStatus,
    pub created_at: DateTime<Utc>,
    pub sport_tag: String,
    pub start_time: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum PairStatus {
    /// Leg1 order placed, waiting for fill.
    Leg1Pending,
    /// Leg1 filled, monitoring leg2 price during game.
    WaitingForDip,
    /// Leg2 order placed, waiting for fill.
    Leg2Pending,
    /// Both legs filled — profit locked.
    Complete,
    /// Cancelled (timeout, market resolved, etc.).
    Cancelled,
}

impl SpreadPair {
    pub fn combined_price(&self) -> f64 {
        self.leg1_price + self.leg2_price
    }

    pub fn expected_profit_per_share(&self) -> f64 {
        if self.leg2_price > 0.0 {
            1.0 - self.combined_price()
        } else {
            0.0
        }
    }

    pub fn matched_shares(&self) -> f64 {
        self.leg1_filled.min(self.leg2_filled)
    }

    pub fn realized_profit(&self) -> f64 {
        self.matched_shares() * self.expected_profit_per_share()
    }
}

// ── Trader ──────────────────────────────────────────────────────────

pub struct SpreadTrader {
    client: Arc<ClobClient>,
    config: SharedConfig,
    pairs: HashMap<String, SpreadPair>,
    log_path: PathBuf,
}

impl SpreadTrader {
    pub fn new(client: Arc<ClobClient>, config: SharedConfig) -> Self {
        let log_path = PathBuf::from("data/spread_pairs.jsonl");
        let pairs = Self::load_pairs(&log_path);
        Self { client, config, pairs, log_path }
    }

    fn load_pairs(path: &PathBuf) -> HashMap<String, SpreadPair> {
        let mut map = HashMap::new();
        if let Ok(content) = std::fs::read_to_string(path) {
            for line in content.lines() {
                if let Ok(pair) = serde_json::from_str::<SpreadPair>(line) {
                    if !matches!(pair.status, PairStatus::Complete | PairStatus::Cancelled) {
                        map.insert(pair.condition_id.clone(), pair);
                    }
                }
            }
        }
        map
    }

    fn append_pair(&self, pair: &SpreadPair) {
        if let Ok(line) = serde_json::to_string(pair) {
            use std::io::Write;
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&self.log_path)
            {
                let _ = writeln!(f, "{}", line);
            }
        }
    }

    // ── Phase 1: discover games and buy cheap side ──────────────────

    pub async fn discover_and_buy_leg1(
        &mut self,
        schedule: &GameSchedule,
        risk: &Arc<RwLock<RiskManager>>,
        logger: &Arc<TradeLogger>,
    ) {
        let cfg = self.config.read().await;
        let spread_cfg = &cfg.spread;
        if !spread_cfg.enabled { return; }

        let max_pairs = spread_cfg.max_concurrent_pairs;
        let active = self.pairs.values()
            .filter(|p| !matches!(p.status, PairStatus::Complete | PairStatus::Cancelled))
            .count();
        if active >= max_pairs {
            debug!("spread: at capacity ({}/{})", active, max_pairs);
            return;
        }

        let now = Utc::now();
        let window_end = now + chrono::Duration::minutes(spread_cfg.window_minutes);
        let max_combined = spread_cfg.max_combined_price;
        let size_per_side = spread_cfg.size_per_side_usdc;
        let min_price = spread_cfg.min_price;
        let max_price = spread_cfg.max_price;
        let leg1_max_price = spread_cfg.leg1_max_price;
        drop(cfg);

        let mut slots_left = max_pairs - active;

        for game in &schedule.games {
            if slots_left == 0 { break; }
            // Only games starting in window (not already started)
            if game.start_time < now || game.start_time > window_end { continue; }

            for (condition_id, question, tokens) in &game.market_tokens {
                if slots_left == 0 { break; }
                if self.pairs.contains_key(condition_id) { continue; }

                // Only "Will X win?" markets
                let q_lower = question.to_lowercase();
                if !q_lower.contains("win") && !q_lower.contains("beat") { continue; }
                if q_lower.contains("spread") || q_lower.contains("over") || q_lower.contains("under") { continue; }
                if tokens.len() != 2 { continue; }

                // Identify YES/NO tokens
                let (yes_outcome, yes_token) = &tokens[0];
                let (no_outcome, no_token) = &tokens[1];
                let (yes_tid, no_tid) = if yes_outcome.eq_ignore_ascii_case("Yes") {
                    (yes_token.clone(), no_token.clone())
                } else if no_outcome.eq_ignore_ascii_case("Yes") {
                    (no_token.clone(), yes_token.clone())
                } else {
                    continue;
                };

                // Fetch orderbooks
                let (yes_book, no_book) = tokio::join!(
                    self.client.get_orderbook(&yes_tid),
                    self.client.get_orderbook(&no_tid),
                );
                let yes_book = match yes_book { Ok(b) => b, Err(_) => continue };
                let no_book = match no_book { Ok(b) => b, Err(_) => continue };

                let yes_ask = match yes_book.best_ask() { Some(p) => p, None => continue };
                let no_ask = match no_book.best_ask() { Some(p) => p, None => continue };

                // The cheap side = lower ask price. That's our leg1 (underdog).
                let (leg1_tid, leg1_out, leg1_ask, leg2_tid, leg2_out, leg2_ask) = if yes_ask <= no_ask {
                    (yes_tid, "Yes".to_string(), yes_ask, no_tid, "No".to_string(), no_ask)
                } else {
                    (no_tid, "No".to_string(), no_ask, yes_tid, "Yes".to_string(), yes_ask)
                };

                // Leg1 must be cheap enough (underdog territory)
                if leg1_ask > leg1_max_price {
                    debug!("spread: {} leg1 too expensive ({:.0}¢ > {:.0}¢)", game.title, leg1_ask * 100.0, leg1_max_price * 100.0);
                    continue;
                }
                if leg1_ask < min_price {
                    continue;
                }

                // Check that leg2 is reachable: if leg2 dipped to max_price, combined still < max_combined?
                let leg2_ceiling = max_combined - leg1_ask;
                if leg2_ceiling < min_price || leg2_ceiling > max_price {
                    debug!("spread: {} leg2 ceiling {:.0}¢ out of bounds", game.title, leg2_ceiling * 100.0);
                    continue;
                }

                // Current combined (at asks) is likely > $1. That's fine — we only buy leg1 now.
                let shares = (size_per_side / leg1_ask).floor();
                if shares < 5.0 { continue; }

                // Risk check
                let leg1_cost = shares * leg1_ask;
                {
                    let r = risk.read().await;
                    if r.bankroll() < leg1_cost + 10.0 {
                        debug!("spread: insufficient bankroll for leg1");
                        continue;
                    }
                }

                // Place leg1 GTC limit buy at ask-1¢ (maker order, 0% fee)
                let leg1_price = (leg1_ask - 0.01).max(0.01);
                let leg1_price = (leg1_price * 100.0).round() / 100.0;

                if self.client.is_dry_run() {
                    info!(
                        "SPREAD DRY LEG1: {} | {} @{:.0}¢ | {:.0}sh ${:.2} | leg2 ceiling={:.0}¢",
                        game.title, leg1_out, leg1_price * 100.0, shares, leg1_cost,
                        leg2_ceiling * 100.0
                    );
                    continue;
                }

                info!(
                    "SPREAD LEG1: {} | {} @{:.0}¢ | {:.0}sh ${:.2} | leg2 ceiling={:.0}¢ (current leg2 ask={:.0}¢)",
                    game.title, leg1_out, leg1_price * 100.0, shares, leg1_cost,
                    leg2_ceiling * 100.0, leg2_ask * 100.0
                );

                let fee = self.client.get_fee_rate_bps(&leg1_tid).await.unwrap_or(0);
                let resp = self.client.create_and_post_order(
                    &leg1_tid, leg1_price, shares, Side::Buy, OrderType::GTC, fee,
                ).await;

                let (order_id, filled) = match &resp {
                    Ok(r) if !r.is_rejected() => {
                        info!("spread leg1 placed: {:?} filled={:.0}", r.effective_id(), r.filled_size());
                        (r.effective_id().map(|s| s.to_string()), r.filled_size())
                    }
                    Ok(r) => {
                        warn!("spread leg1 rejected: {:?}", r.skipped);
                        (None, 0.0)
                    }
                    Err(e) => {
                        warn!("spread leg1 failed: {}", e);
                        (None, 0.0)
                    }
                };

                if order_id.is_none() && filled < 1.0 {
                    continue; // Failed to place
                }

                let status = if filled >= shares * 0.95 {
                    PairStatus::WaitingForDip
                } else {
                    PairStatus::Leg1Pending
                };

                let pair = SpreadPair {
                    condition_id: condition_id.clone(),
                    event_slug: game.event_slug.clone(),
                    title: game.title.clone(),
                    leg1_token_id: leg1_tid,
                    leg1_outcome: leg1_out.clone(),
                    leg1_price,
                    leg1_order_id: order_id.clone(),
                    leg1_filled: filled,
                    leg2_token_id: leg2_tid,
                    leg2_outcome: leg2_out.clone(),
                    leg2_price: 0.0,
                    leg2_order_id: None,
                    leg2_filled: 0.0,
                    target_shares: shares,
                    leg2_max_price: leg2_ceiling,
                    status: status.clone(),
                    created_at: Utc::now(),
                    sport_tag: game.sport_tag.clone(),
                    start_time: game.start_time,
                };

                // Log leg1 trade
                logger.log(crate::logger::TradeLog {
                    timestamp: Utc::now(),
                    token_id: pair.leg1_token_id.clone(),
                    condition_id: condition_id.clone(),
                    market_title: game.title.clone(),
                    sport: game.sport_tag.clone(),
                    side: "BUY".to_string(),
                    outcome: leg1_out,
                    price: leg1_price,
                    size_usdc: shares * leg1_price,
                    size_shares: shares,
                    signal_source: "spread_leg1".to_string(),
                    copy_wallet: None,
                    consensus_count: None,
                    consensus_wallets: None,
                    edge_pct: 0.0,
                    confidence: 1.0,
                    signal_delay_ms: 0,
                    event_slug: Some(game.event_slug.clone()),
                    order_id,
                    filled: filled > 0.0,
                    dry_run: false,
                    result: None,
                    pnl: None,
                    resolved_at: None,
                    sell_price: None,
                    actual_pnl: None,
                    exit_type: None,
                    closing_price: None,
                    taker_ask: Some(leg1_ask),
                    strategy_version: None,
                });

                self.append_pair(&pair);
                self.pairs.insert(condition_id.clone(), pair);
                slots_left -= 1;
            }
        }
    }

    // ── Phase 2: monitor dips and buy leg2 ──────────────────────────

    pub async fn monitor_and_buy_leg2(
        &mut self,
        logger: &Arc<TradeLogger>,
    ) {
        // Collect pairs ready for leg2 monitoring
        let candidates: Vec<(String, String, f64, f64, DateTime<Utc>)> = self.pairs.iter()
            .filter(|(_, p)| p.status == PairStatus::WaitingForDip)
            .map(|(cid, p)| (
                cid.clone(),
                p.leg2_token_id.clone(),
                p.leg2_max_price,
                p.target_shares,
                p.start_time,
            ))
            .collect();

        for (cid, leg2_tid, leg2_max, target_shares, start_time) in candidates {
            let now = Utc::now();

            // Only monitor during game (after start_time)
            let minutes_since_start = (now - start_time).num_minutes();
            if minutes_since_start < -2 {
                // Game hasn't started yet, skip
                continue;
            }

            // Fetch leg2 orderbook
            let book = match self.client.get_orderbook(&leg2_tid).await {
                Ok(b) => b,
                Err(_) => continue,
            };

            let ask = match book.best_ask() {
                Some(p) => p,
                None => continue,
            };

            let ask = (ask * 100.0).round() / 100.0;

            let pair = self.pairs.get(&cid).unwrap();
            let combined = pair.leg1_price + ask;

            // Has leg2 dipped below our ceiling?
            if ask > leg2_max {
                debug!(
                    "spread leg2: {} | {} ask={:.0}¢ > max {:.0}¢ (combined={:.0}¢)",
                    pair.title, pair.leg2_outcome, ask * 100.0,
                    leg2_max * 100.0, combined * 100.0
                );
                continue;
            }

            // DIP DETECTED — buy leg2 at ask-1¢ (GTC maker)
            let leg2_price = (ask - 0.01).max(0.01);
            let leg2_price = (leg2_price * 100.0).round() / 100.0;
            let combined_at_limit = pair.leg1_price + leg2_price;
            let profit_per_share = 1.0 - combined_at_limit;

            if profit_per_share <= 0.0 {
                debug!("spread leg2: ask-1 would make combined >= $1, skip");
                continue;
            }

            info!(
                "SPREAD LEG2 DIP: {} | {} @{:.0}¢ (ask={:.0}¢) | combined={:.0}¢ | profit={:.1}¢/sh ${:.2}",
                pair.title, pair.leg2_outcome, leg2_price * 100.0, ask * 100.0,
                combined_at_limit * 100.0, profit_per_share * 100.0,
                target_shares * profit_per_share
            );

            if self.client.is_dry_run() {
                info!("SPREAD DRY LEG2: would place GTC {:.0}sh @{:.0}¢", target_shares, leg2_price * 100.0);
                continue;
            }

            let fee = self.client.get_fee_rate_bps(&leg2_tid).await.unwrap_or(0);
            let resp = self.client.create_and_post_order(
                &leg2_tid, leg2_price, target_shares, Side::Buy, OrderType::GTC, fee,
            ).await;

            let (order_id, filled) = match &resp {
                Ok(r) if !r.is_rejected() => {
                    info!("spread leg2 placed: {:?} filled={:.0}", r.effective_id(), r.filled_size());
                    (r.effective_id().map(|s| s.to_string()), r.filled_size())
                }
                Ok(r) => {
                    warn!("spread leg2 rejected: {:?}", r.skipped);
                    (None, 0.0)
                }
                Err(e) => {
                    warn!("spread leg2 failed: {}", e);
                    (None, 0.0)
                }
            };

            // Update pair
            let pair = self.pairs.get_mut(&cid).unwrap();
            pair.leg2_price = leg2_price;
            pair.leg2_order_id = order_id.clone();
            pair.leg2_filled = filled;

            if filled >= target_shares * 0.95 {
                pair.status = PairStatus::Complete;
                info!(
                    "SPREAD COMPLETE: {} | {:.0}sh × {:.1}¢ = ${:.2} profit",
                    pair.title, pair.matched_shares(),
                    pair.expected_profit_per_share() * 100.0,
                    pair.realized_profit()
                );
            } else if filled > 0.0 {
                pair.status = PairStatus::Leg2Pending;
            }
            // If filled == 0, stay in WaitingForDip to retry next poll

            let pair_snap = pair.clone();
            self.append_pair(&pair_snap);

            // Log leg2 trade
            if filled > 0.0 {
                logger.log(crate::logger::TradeLog {
                    timestamp: Utc::now(),
                    token_id: pair_snap.leg2_token_id.clone(),
                    condition_id: cid.clone(),
                    market_title: pair_snap.title.clone(),
                    sport: pair_snap.sport_tag.clone(),
                    side: "BUY".to_string(),
                    outcome: pair_snap.leg2_outcome.clone(),
                    price: leg2_price,
                    size_usdc: filled * leg2_price,
                    size_shares: filled,
                    signal_source: "spread_leg2".to_string(),
                    copy_wallet: None,
                    consensus_count: None,
                    consensus_wallets: None,
                    edge_pct: profit_per_share * 100.0,
                    confidence: 1.0,
                    signal_delay_ms: 0,
                    event_slug: Some(pair_snap.event_slug.clone()),
                    order_id,
                    filled: true,
                    dry_run: false,
                    result: None,
                    pnl: None,
                    resolved_at: None,
                    sell_price: None,
                    actual_pnl: None,
                    exit_type: None,
                    closing_price: None,
                    taker_ask: Some(ask),
                    strategy_version: None,
                });
            }
        }
    }

    // ── Fill checks for pending orders ──────────────────────────────

    pub async fn check_pending_fills(&mut self) {
        // Collect pending leg1 orders to check
        let leg1_checks: Vec<(String, String)> = self.pairs.iter()
            .filter(|(_, p)| p.status == PairStatus::Leg1Pending)
            .filter_map(|(cid, p)| {
                p.leg1_order_id.as_ref().map(|oid| (cid.clone(), oid.clone()))
            })
            .collect();

        for (cid, oid) in &leg1_checks {
            match self.client.get_order_status(oid).await {
                Ok((_status, matched)) => {
                    let pair = self.pairs.get_mut(cid).unwrap();
                    if matched > pair.leg1_filled {
                        pair.leg1_filled = matched;
                        info!("spread leg1 fill: {} → {:.0}sh", pair.title, matched);
                    }
                    if matched >= pair.target_shares * 0.95 {
                        pair.status = PairStatus::WaitingForDip;
                        info!("spread leg1 filled → monitoring for leg2 dip: {}", pair.title);
                        let snap = pair.clone();
                        self.append_pair(&snap);
                    }
                }
                Err(e) => debug!("spread: leg1 check failed: {}", e),
            }
        }

        // Collect pending leg2 orders to check
        let leg2_checks: Vec<(String, String)> = self.pairs.iter()
            .filter(|(_, p)| p.status == PairStatus::Leg2Pending)
            .filter_map(|(cid, p)| {
                p.leg2_order_id.as_ref().map(|oid| (cid.clone(), oid.clone()))
            })
            .collect();

        for (cid, oid) in &leg2_checks {
            match self.client.get_order_status(oid).await {
                Ok((_status, matched)) => {
                    let pair = self.pairs.get_mut(cid).unwrap();
                    if matched > pair.leg2_filled {
                        pair.leg2_filled = matched;
                        info!("spread leg2 fill: {} → {:.0}sh", pair.title, matched);
                    }
                    if matched >= pair.target_shares * 0.95 {
                        pair.status = PairStatus::Complete;
                        info!(
                            "SPREAD COMPLETE: {} | {:.0}sh × {:.1}¢ = ${:.2}",
                            pair.title, pair.matched_shares(),
                            pair.expected_profit_per_share() * 100.0,
                            pair.realized_profit()
                        );
                        let snap = pair.clone();
                        self.append_pair(&snap);
                    }
                }
                Err(e) => debug!("spread: leg2 check failed: {}", e),
            }
        }
    }

    // ── Timeouts ────────────────────────────────────────────────────

    pub async fn cancel_stale(&mut self) {
        let now = Utc::now();
        let mut to_cancel: Vec<(String, Option<String>, Option<String>)> = Vec::new();

        for (cid, pair) in &self.pairs {
            if matches!(pair.status, PairStatus::Complete | PairStatus::Cancelled) {
                continue;
            }
            let age_hours = (now - pair.created_at).num_hours();
            // Leg1 pending > 1h: cancel (game probably started without fill)
            if pair.status == PairStatus::Leg1Pending && age_hours > 1 {
                to_cancel.push((cid.clone(), pair.leg1_order_id.clone(), None));
            }
            // WaitingForDip > 4h: game likely over, leg2 never came
            if pair.status == PairStatus::WaitingForDip && age_hours > 4 {
                to_cancel.push((cid.clone(), None, None));
            }
            // Leg2 pending > 1h: cancel
            if pair.status == PairStatus::Leg2Pending && age_hours > 4 {
                to_cancel.push((cid.clone(), None, pair.leg2_order_id.clone()));
            }
        }

        for (cid, leg1_oid, leg2_oid) in &to_cancel {
            if let Some(oid) = leg1_oid { let _ = self.client.cancel_order(oid).await; }
            if let Some(oid) = leg2_oid { let _ = self.client.cancel_order(oid).await; }
            if let Some(pair) = self.pairs.get_mut(cid) {
                warn!("spread TIMEOUT: {} | status={:?}", pair.title, pair.status);
                pair.status = PairStatus::Cancelled;
                let snap = pair.clone();
                self.append_pair(&snap);
            }
        }

        // Cleanup old entries from memory
        let cutoff = now - chrono::Duration::hours(24);
        self.pairs.retain(|_, p| {
            !(matches!(p.status, PairStatus::Complete | PairStatus::Cancelled) && p.created_at < cutoff)
        });
    }

    pub fn status_summary(&self) -> String {
        let leg1 = self.pairs.values().filter(|p| p.status == PairStatus::Leg1Pending).count();
        let waiting = self.pairs.values().filter(|p| p.status == PairStatus::WaitingForDip).count();
        let leg2 = self.pairs.values().filter(|p| p.status == PairStatus::Leg2Pending).count();
        let complete = self.pairs.values().filter(|p| p.status == PairStatus::Complete).count();
        let profit: f64 = self.pairs.values()
            .filter(|p| p.status == PairStatus::Complete)
            .map(|p| p.realized_profit())
            .sum();
        format!("spread: leg1={leg1} wait={waiting} leg2={leg2} done={complete} profit=${profit:.2}")
    }
}

// ── Loop ────────────────────────────────────────────────────────────

pub async fn spread_loop(
    client: Arc<ClobClient>,
    config: SharedConfig,
    logger: Arc<TradeLogger>,
    risk: Arc<RwLock<RiskManager>>,
) {
    let mut trader = SpreadTrader::new(client.clone(), config.clone());
    let active = trader.pairs.len();
    if active > 0 {
        info!("spread: restored {} active pairs from disk", active);
    }

    let mut schedule = GameSchedule::load_from_disk(std::path::Path::new("data/schedule_cache.json"));

    loop {
        let cfg = config.read().await;
        let enabled = cfg.spread.enabled;
        let poll_secs = cfg.spread.poll_interval_seconds;
        let sport_tags = cfg.spread.sport_tags.clone();
        drop(cfg);

        if !enabled {
            tokio::time::sleep(tokio::time::Duration::from_secs(60)).await;
            continue;
        }

        // Refresh schedule every 60 min
        if schedule.needs_refresh(60) && !sport_tags.is_empty() {
            scheduler::refresh_schedule(&client, &sport_tags, &mut schedule).await;
        }

        // Phase 1: discover new games, buy cheap side
        trader.discover_and_buy_leg1(&schedule, &risk, &logger).await;

        // Check pending order fills
        trader.check_pending_fills().await;

        // Phase 2: monitor active pairs for leg2 dip opportunities
        trader.monitor_and_buy_leg2(&logger).await;

        // Cancel stale pairs
        trader.cancel_stale().await;

        debug!("spread: {}", trader.status_summary());

        tokio::time::sleep(tokio::time::Duration::from_secs(poll_secs)).await;
    }
}
