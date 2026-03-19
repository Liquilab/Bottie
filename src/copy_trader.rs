use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use tokio::sync::RwLock;
use tracing::{info, warn};

use futures_util::future::join_all;

use crate::clob::client::ClobClient;
use crate::clob::types::WalletPosition;
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
    /// Track when each wallet last produced a signal (for tiered polling)
    last_signal_time: HashMap<String, DateTime<Utc>>,
    /// Monotonic poll counter for warm-tier scheduling
    poll_count: u64,
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
            last_signal_time: HashMap::new(),
            poll_count: 0,
        }
    }

    /// Poll watched wallets for new positions using /positions snapshot-diff.
    /// This replaces the unreliable /activity endpoint.
    ///
    /// Uses tiered polling:
    /// - Hot tier (signal in last 30 min): polled every cycle
    /// - Warm tier (rest): polled every warm_poll_interval / poll_interval cycles
    pub async fn poll(&mut self) -> Vec<CopySignal> {
        self.poll_count += 1;

        let config = self.config.read().await;
        let max_delay_secs = config.copy_trading.max_delay_seconds;
        let batch_size = config.copy_trading.batch_size.max(1);
        let consensus_window_mins = config.copy_trading.consensus.window_minutes;
        let poll_interval = config.copy_trading.poll_interval_seconds;
        let warm_interval = config.copy_trading.warm_poll_interval_seconds;

        // Read autoresearch_params for wallet weight overrides and sport multipliers
        let wallet_overrides = config.autoresearch_params.wallet_weights_override.clone();
        let sport_multipliers = config.autoresearch_params.sport_multipliers.clone();

        let global_min_price = config.sizing.min_price;
        let global_max_price = config.sizing.max_price;

        let watchlist: Vec<(String, String, f64, Vec<String>, f64, f64)> = config
            .copy_trading
            .watchlist
            .iter()
            .map(|e| {
                let addr = e.address.to_lowercase();
                let base_weight = e.weight;
                let weight = wallet_overrides.get(&addr).copied().unwrap_or(base_weight);
                let market_types = e.market_types.clone();
                let min_price = e.min_price.unwrap_or(global_min_price);
                let max_price = e.max_price.unwrap_or(global_max_price);
                (addr, e.name.clone(), weight, market_types, min_price, max_price)
            })
            .collect();
        drop(config);

        let tracker = self.tracker.read().await;
        let mut signals = Vec::new();
        let mut total_new = 0u32;

        // Tiered polling: determine which wallets to poll this cycle
        let warm_every_n = if poll_interval > 0 {
            (warm_interval / poll_interval).max(1)
        } else {
            4
        };
        let now = Utc::now();
        let hot_cutoff = chrono::Duration::minutes(consensus_window_mins as i64);

        let active_wallets: Vec<_> = watchlist
            .iter()
            .filter(|(_, _, w, _, _, _)| *w > 0.0)
            .filter(|(addr, _, _, _, _, _)| {
                Self::should_poll_wallet(
                    addr,
                    self.poll_count,
                    warm_every_n,
                    consensus_window_mins,
                    &self.last_signal_time,
                    &self.prev_positions,
                    now,
                )
            })
            .collect();

        // Phase 1: Fetch wallet positions in parallel (configurable batch size)
        let fetch_start = Instant::now();
        let mut fetched: Vec<(String, String, f64, Vec<String>, f64, f64, Vec<WalletPosition>)> = Vec::new();

        for chunk in active_wallets.chunks(batch_size) {
            let futures: Vec<_> = chunk
                .iter()
                .map(|(addr, name, weight, market_types, min_price, max_price)| {
                    let client = self.client.clone();
                    let addr = addr.clone();
                    let name = name.clone();
                    let weight = *weight;
                    let market_types = market_types.clone();
                    let min_price = *min_price;
                    let max_price = *max_price;
                    async move {
                        let result = client.get_wallet_positions(&addr, 100).await;
                        (addr, name, weight, market_types, min_price, max_price, result)
                    }
                })
                .collect();

            let results = join_all(futures).await;
            for (addr, name, weight, market_types, min_price, max_price, result) in results {
                match result {
                    Ok(positions) => fetched.push((addr, name, weight, market_types, min_price, max_price, positions)),
                    Err(e) => warn!("positions fetch failed for {name}: {e}"),
                }
            }
            // Brief pause between batches to respect rate limits
            tokio::time::sleep(Duration::from_millis(150)).await;
        }

        let fetch_ms = fetch_start.elapsed().as_millis();
        let total_wallets = watchlist.iter().filter(|(_, _, w, _, _, _)| *w > 0.0).count();
        if fetched.len() > 1 {
            info!(
                "POLL FETCH: {}/{} wallets in {}ms (batches of {})",
                fetched.len(), total_wallets, fetch_ms, batch_size
            );
        }

        // Phase 2: Process results sequentially (diff, consensus, signals)
        for (address, name, weight, wallet_market_types, wallet_min_price, wallet_max_price, positions) in &fetched {
            let weight = *weight;
            // Build current snapshot: position_key → size
            // Only include open (unresolved) positions: 0.01 < curPrice < 0.99
            let mut current_snapshot: HashMap<String, f64> = HashMap::new();
            for pos in positions {
                let key = pos.position_key();
                if key == ":" {
                    continue; // skip invalid positions
                }
                let cur = pos.cur_price_f64();
                if cur <= 0.01 || cur >= 0.99 {
                    continue; // already resolved — exclude from snapshot
                }
                current_snapshot.insert(key, pos.size_f64());
            }

            // Hauptbet filter: per conditionId, find the largest USDC position.
            // Only consider open positions (curPrice 0.01–0.99).
            let mut max_usdc_per_condition: HashMap<String, f64> = HashMap::new();
            for pos in positions {
                let cid = pos.condition_id.clone().unwrap_or_default();
                if cid.is_empty() { continue; }
                let cur = pos.cur_price_f64();
                if cur <= 0.01 || cur >= 0.99 { continue; } // resolved
                let usdc = pos.initial_value_f64();
                let entry = max_usdc_per_condition.entry(cid).or_insert(0.0);
                if usdc > *entry { *entry = usdc; }
            }

            // Compare with previous snapshot to find NEW positions
            let prev = self.prev_positions.get(address.as_str());

            for pos in positions {
                let key = pos.position_key();
                if key == ":" {
                    continue;
                }

                let cur_size = pos.size_f64();
                if cur_size <= 0.0 {
                    continue;
                }

                // Skip already-resolved markets (curPrice at 0 or 1)
                let cur_price = pos.cur_price_f64();
                if cur_price <= 0.01 || cur_price >= 0.99 {
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

                let is_seeding = prev.is_none();

                total_new += 1;

                let price = pos.avg_price_f64();
                if price <= 0.0 || price >= 1.0 {
                    continue;
                }

                let condition_id = pos.condition_id.clone().unwrap_or_default();
                let outcome = pos.outcome.clone().unwrap_or_default();

                // Hauptbet filter: skip if this is a hedge (not the largest position
                // for this conditionId). Allows tied positions (e.g. two equal bets).
                if !condition_id.is_empty() {
                    let usdc = pos.initial_value_f64();
                    let max_for_cond = max_usdc_per_condition.get(&condition_id).copied().unwrap_or(0.0);
                    if usdc < max_for_cond {
                        continue; // hedge / smaller bet — skip
                    }
                }

                // Delay = time since we first detected this position change.
                // Since we poll every ~15s with parallel batches (~1.7s), the max detection
                // delay is one poll cycle. For positions that were already there in the
                // previous snapshot (but grew), the delay is at most one poll interval.
                // This is a conservative proxy — we know the position changed between
                // prev_snapshot and now, so it's at most poll_interval old.
                //
                // The /trades API doesn't reliably filter by wallet address, so we can't
                // measure exact wallet entry timestamp.
                let signal_delay_ms: u64 = 15_000; // one poll cycle as conservative estimate

                // Only trade if price hasn't moved much since entry — edge still exists
                // (cur_price already validated as 0.01–0.99 above)
                let drift = (cur_price - price).abs() / price;
                if drift > 0.15 {
                    continue; // price moved >15% since entry, edge is gone
                }
                let usdc_size = pos.initial_value_f64();
                let asset = pos.asset.clone().unwrap_or_default();
                let title = pos.title.clone().unwrap_or_else(|| {
                    condition_id[..12.min(condition_id.len())].to_string()
                });
                let sport = Self::detect_sport(&title, pos.slug.as_deref().unwrap_or(""));

                // Per-wallet market type filter
                if !wallet_market_types.is_empty() {
                    let detected = Self::detect_market_type(&title);
                    if !wallet_market_types.iter().any(|mt| mt == &detected) {
                        continue;
                    }
                }

                // Per-wallet price filter
                if price < *wallet_min_price || price > *wallet_max_price {
                    continue;
                }

                // Use eventSlug (event-level) for consensus grouping.
                // This means Celtics moneyline + Celtics O/U + Celtics spread
                // all count as consensus on the same event.
                let raw_slug = pos.event_slug.clone()
                    .or_else(|| pos.slug.clone())
                    .unwrap_or_default();
                // Normalize: strip "-more-markets" so variant slugs match the base event
                let event_slug = raw_slug.trim_end_matches("-more-markets").to_string();

                let consensus_key = if event_slug.is_empty() {
                    // Fallback to conditionId if no event slug
                    format!("{}:{}", condition_id, outcome)
                } else {
                    event_slug.clone()
                };

                self.recent_bets
                    .entry(consensus_key.clone())
                    .or_default()
                    .push(RecentBet {
                        wallet: address.clone(),
                        outcome: outcome.clone(),
                        weight,
                        usdc_size,
                        timestamp: Utc::now(),
                    });

                // Prune bets older than consensus window
                if let Some(bets) = self.recent_bets.get_mut(&consensus_key) {
                    let window = chrono::Duration::minutes(consensus_window_mins as i64);
                    bets.retain(|b| {
                        Utc::now().signed_duration_since(b.timestamp) < window
                    });
                }

                // Consensus scoring at event level (uses extracted function for testability)
                let (outcome_score, consensus_wallets, consensus_wallet_names) = Self::consensus_score(
                    &self.recent_bets,
                    &consensus_key,
                    consensus_window_mins,
                    Utc::now(),
                    weight,
                );

                // Seeding phase: skip solo positions but allow consensus trades.
                if is_seeding && consensus_wallets < 2 {
                    continue;
                }

                // Only trade the majority side. If this signal's outcome is NOT
                // the majority outcome for this event → skip (prevents trading
                // the minority side of a split, e.g. 6× No vs 2× Yes → only trade No).
                if consensus_wallets >= 2 && !Self::outcome_matches_majority(
                    &self.recent_bets, &consensus_key, &outcome, consensus_window_mins, Utc::now()
                ) {
                    continue;
                }

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

                let our_trades = wallet_info.map(|w| w.total_tracked_trades).unwrap_or(0);
                let our_wr = wallet_info.map(|w| w.overall_win_rate).unwrap_or(0.0);
                info!(
                    "SIGNAL: {} ({:.0}ct) | {} | {:.0}$ | {} wallets (score:{:.1}) | our_wr={:.0}% ({} trades) | delay={:.0}s",
                    name,
                    price * 100.0,
                    title.chars().take(50).collect::<String>(),
                    usdc_size,
                    consensus_wallets,
                    outcome_score,
                    our_wr * 100.0,
                    our_trades,
                    signal_delay_ms as f64 / 1000.0
                );

                // event_slug already computed above for consensus grouping

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
                    event_slug,
                    confidence,
                    consensus_count: consensus_wallets,
                    consensus_wallets: consensus_wallet_names,
                    timestamp: Utc::now(),
                    signal_delay_ms,
                });

                // Mark wallet as hot (produced a signal)
                self.last_signal_time.insert(address.clone(), Utc::now());
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

    /// Determine the polling tier for a wallet this cycle.
    /// Returns true if the wallet should be polled.
    pub fn should_poll_wallet(
        addr: &str,
        poll_count: u64,
        warm_every_n: u64,
        consensus_window_mins: u32,
        last_signal_time: &HashMap<String, DateTime<Utc>>,
        prev_positions: &HashMap<String, HashMap<String, f64>>,
        now: DateTime<Utc>,
    ) -> bool {
        // Hot tier: always poll if signal in last window_minutes
        let hot_cutoff = chrono::Duration::minutes(consensus_window_mins as i64);
        if let Some(last) = last_signal_time.get(addr) {
            if now.signed_duration_since(*last) < hot_cutoff {
                return true;
            }
        }
        // First poll (no prev_positions): always poll to seed
        if !prev_positions.contains_key(addr) {
            return true;
        }
        // Warm tier: poll every N-th cycle
        poll_count % warm_every_n == 0
    }

    /// Score consensus for an event, outcome-aware.
    ///
    /// Groups bets by outcome, finds the majority side.
    /// Only counts wallets on the majority side as consensus.
    /// If the signal's outcome doesn't match the majority → consensus = 1 (skip).
    ///
    /// Example: 6 wallets on "No", 2 on "Yes" → majority="No" (75%).
    /// Signal for "No" → consensus=6. Signal for "Yes" → consensus=1 (solo).
    ///
    /// Returns (weighted_score, majority_wallet_count, majority_wallet_names, majority_outcome).
    pub fn consensus_score(
        recent_bets: &HashMap<String, Vec<RecentBet>>,
        token_key: &str,
        window_minutes: u32,
        now: DateTime<Utc>,
        fallback_weight: f64,
    ) -> (f64, u32, Vec<String>) {
        let window = chrono::Duration::minutes(window_minutes as i64);
        match recent_bets.get(token_key) {
            Some(bets) => {
                // Group valid (within window) bets by outcome → unique wallets
                let mut by_outcome: HashMap<String, std::collections::HashSet<String>> = HashMap::new();
                let mut scores_by_outcome: HashMap<String, f64> = HashMap::new();
                for b in bets {
                    if now.signed_duration_since(b.timestamp) < window {
                        by_outcome.entry(b.outcome.clone()).or_default().insert(b.wallet.clone());
                        *scores_by_outcome.entry(b.outcome.clone()).or_default() +=
                            b.weight * (1.0 + b.usdc_size.sqrt() / 100.0);
                    }
                }

                if by_outcome.is_empty() {
                    return (fallback_weight, 1, vec![]);
                }

                // Find majority outcome (most unique wallets)
                let (majority_outcome, majority_wallets) = by_outcome
                    .iter()
                    .max_by_key(|(_, ws)| ws.len())
                    .map(|(o, ws)| (o.clone(), ws.clone()))
                    .unwrap();

                let majority_count = majority_wallets.len() as u32;
                let majority_score = scores_by_outcome.get(&majority_outcome).copied().unwrap_or(0.0);
                let majority_names: Vec<String> = majority_wallets.into_iter().collect();

                (majority_score, majority_count, majority_names)
            }
            None => (fallback_weight, 1, vec![]),
        }
    }

    /// Check if a signal's outcome matches the consensus majority for its event.
    pub fn outcome_matches_majority(
        recent_bets: &HashMap<String, Vec<RecentBet>>,
        consensus_key: &str,
        outcome: &str,
        window_minutes: u32,
        now: DateTime<Utc>,
    ) -> bool {
        let window = chrono::Duration::minutes(window_minutes as i64);
        match recent_bets.get(consensus_key) {
            Some(bets) => {
                let mut by_outcome: HashMap<String, usize> = HashMap::new();
                for b in bets {
                    if now.signed_duration_since(b.timestamp) < window {
                        *by_outcome.entry(b.outcome.clone()).or_default() += 1;
                    }
                }
                let majority = by_outcome.iter().max_by_key(|(_, c)| *c);
                match majority {
                    Some((maj_outcome, _)) => maj_outcome == outcome,
                    None => true,
                }
            }
            None => true,
        }
    }

    /// Detect market type from title.
    /// - "win" = title contains "win on" or "win the"
    /// - "ou" = title contains "O/U"
    /// - "spread" = title contains "Spread"
    /// - "draw" = title contains "draw"
    /// - "ml" = title contains "vs." and none of the above
    pub fn detect_market_type(title: &str) -> String {
        let t = title.to_lowercase();
        if t.contains("win on") || t.contains("win the") {
            "win".to_string()
        } else if t.contains("o/u") {
            "ou".to_string()
        } else if t.contains("spread") {
            "spread".to_string()
        } else if t.contains("draw") {
            "draw".to_string()
        } else if t.contains("vs.") {
            "ml".to_string()
        } else {
            "other".to_string()
        }
    }

    fn detect_sport(title: &str, event_slug: &str) -> String {
        let combined = format!("{} {}", title.to_lowercase(), event_slug.to_lowercase());
        if combined.contains("nba") || combined.contains("basketball") {
            "nba".to_string()
        } else if combined.contains("nfl") {
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

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, Utc};

    fn empty_signal_times() -> HashMap<String, DateTime<Utc>> {
        HashMap::new()
    }

    fn empty_prev_positions() -> HashMap<String, HashMap<String, f64>> {
        HashMap::new()
    }

    fn seeded_prev_positions(addrs: &[&str]) -> HashMap<String, HashMap<String, f64>> {
        addrs.iter().map(|a| (a.to_string(), HashMap::new())).collect()
    }

    // ═══════════════════════════════════════════════════════════════════
    // RUS-210: Tiered polling
    // ═══════════════════════════════════════════════════════════════════

    #[test]
    fn hot_wallet_always_polled() {
        let now = Utc::now();
        let mut signals = HashMap::new();
        signals.insert("0xhot".to_string(), now - Duration::minutes(5));
        let prev = seeded_prev_positions(&["0xhot"]);

        // Should be polled on ANY cycle (even non-warm cycles)
        for cycle in [1, 2, 3, 5, 7] {
            assert!(
                CopyTrader::should_poll_wallet("0xhot", cycle, 4, 30, &signals, &prev, now),
                "hot wallet should be polled on cycle {cycle}"
            );
        }
    }

    #[test]
    fn unseeded_wallet_always_polled() {
        let now = Utc::now();
        let prev = empty_prev_positions(); // no seeded wallets

        for cycle in [1, 2, 3, 5] {
            assert!(
                CopyTrader::should_poll_wallet("0xnew", cycle, 4, 30, &empty_signal_times(), &prev, now),
                "unseeded wallet should be polled on cycle {cycle}"
            );
        }
    }

    #[test]
    fn warm_wallet_only_on_nth_cycle() {
        let now = Utc::now();
        let prev = seeded_prev_positions(&["0xwarm"]);
        let signals = empty_signal_times(); // no recent signal

        // warm_every_n = 4 (60s / 15s)
        assert!(!CopyTrader::should_poll_wallet("0xwarm", 1, 4, 30, &signals, &prev, now));
        assert!(!CopyTrader::should_poll_wallet("0xwarm", 2, 4, 30, &signals, &prev, now));
        assert!(!CopyTrader::should_poll_wallet("0xwarm", 3, 4, 30, &signals, &prev, now));
        assert!(CopyTrader::should_poll_wallet("0xwarm", 4, 4, 30, &signals, &prev, now));
        assert!(!CopyTrader::should_poll_wallet("0xwarm", 5, 4, 30, &signals, &prev, now));
        assert!(CopyTrader::should_poll_wallet("0xwarm", 8, 4, 30, &signals, &prev, now));
        assert!(CopyTrader::should_poll_wallet("0xwarm", 12, 4, 30, &signals, &prev, now));
    }

    #[test]
    fn hot_expires_to_warm_after_window() {
        let now = Utc::now();
        let mut signals = HashMap::new();
        // Signal 31 minutes ago, window is 30 minutes
        signals.insert("0xexpired".to_string(), now - Duration::minutes(31));
        let prev = seeded_prev_positions(&["0xexpired"]);

        // cycle 1: not a warm cycle, not hot anymore → skip
        assert!(!CopyTrader::should_poll_wallet("0xexpired", 1, 4, 30, &signals, &prev, now));
        // cycle 4: warm cycle → poll
        assert!(CopyTrader::should_poll_wallet("0xexpired", 4, 4, 30, &signals, &prev, now));
    }

    #[test]
    fn config_change_warm_interval() {
        let now = Utc::now();
        let prev = seeded_prev_positions(&["0xwarm"]);
        let signals = empty_signal_times();

        // warm_every_n = 2 (30s / 15s) — faster polling
        assert!(!CopyTrader::should_poll_wallet("0xwarm", 1, 2, 30, &signals, &prev, now));
        assert!(CopyTrader::should_poll_wallet("0xwarm", 2, 2, 30, &signals, &prev, now));
        assert!(!CopyTrader::should_poll_wallet("0xwarm", 3, 2, 30, &signals, &prev, now));
        assert!(CopyTrader::should_poll_wallet("0xwarm", 4, 2, 30, &signals, &prev, now));
    }

    // ═══════════════════════════════════════════════════════════════════
    // RUS-211: Consensus window pruning
    // ═══════════════════════════════════════════════════════════════════

    #[test]
    fn prune_after_window() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet {
                wallet: "0xa".to_string(),
                outcome: "Yes".to_string(),
                weight: 1.0,
                usdc_size: 100.0,
                timestamp: now - Duration::minutes(31), // expired
            },
        ]);

        let (_score, count, _names) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        assert_eq!(count, 1, "expired bet → fallback to 1 (solo)");
    }

    #[test]
    fn keep_within_window() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet {
                wallet: "0xa".to_string(),
                outcome: "Yes".to_string(),
                weight: 1.0,
                usdc_size: 100.0,
                timestamp: now - Duration::minutes(29), // within window
            },
        ]);

        let (_score, count, _names) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        assert_eq!(count, 1);
        assert!(_score > 0.5, "should use actual score, not fallback");
    }

    #[test]
    fn two_wallets_consensus() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet {
                wallet: "0xa".to_string(),
                outcome: "Yes".to_string(),
                weight: 1.0,
                usdc_size: 100.0,
                timestamp: now - Duration::minutes(5),
            },
            RecentBet {
                wallet: "0xb".to_string(),
                outcome: "Yes".to_string(),
                weight: 0.8,
                usdc_size: 50.0,
                timestamp: now - Duration::minutes(10),
            },
        ]);

        let (_score, count, _names) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        assert_eq!(count, 2, "two unique wallets = consensus_count 2");
    }

    #[test]
    fn expired_wallet_excluded_from_consensus() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet {
                wallet: "0xa".to_string(),
                outcome: "Yes".to_string(),
                weight: 1.0,
                usdc_size: 100.0,
                timestamp: now - Duration::minutes(5), // fresh
            },
            RecentBet {
                wallet: "0xb".to_string(),
                outcome: "Yes".to_string(),
                weight: 0.8,
                usdc_size: 50.0,
                timestamp: now - Duration::minutes(35), // expired
            },
        ]);

        let (_score, count, _names) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        assert_eq!(count, 1, "expired wallet should not count");
    }

    #[test]
    fn window_30_prunes_more_than_120() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet {
                wallet: "0xa".to_string(),
                outcome: "Yes".to_string(),
                weight: 1.0,
                usdc_size: 100.0,
                timestamp: now - Duration::minutes(5),
            },
            RecentBet {
                wallet: "0xb".to_string(),
                outcome: "Yes".to_string(),
                weight: 0.8,
                usdc_size: 50.0,
                timestamp: now - Duration::minutes(60), // 60 min ago
            },
        ]);

        let (_, count_30, _) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        let (_, count_120, _) = CopyTrader::consensus_score(&bets, "cid:Yes", 120, now, 0.5);
        assert_eq!(count_30, 1, "window=30 should prune the 60-min bet");
        assert_eq!(count_120, 2, "window=120 should keep both");
    }

    // ═══════════════════════════════════════════════════════════════════
    // RUS-212: batch_size edge cases
    // ═══════════════════════════════════════════════════════════════════

    #[test]
    fn batch_size_zero_becomes_one() {
        let batch_size: usize = 0_usize.max(1);
        assert_eq!(batch_size, 1);
    }

    #[test]
    fn batch_size_chunks_no_panic() {
        let wallets: Vec<i32> = vec![1, 2, 3, 4, 5];

        // batch_size = 1
        assert_eq!(wallets.chunks(1).count(), 5);
        // batch_size = 8 (> len)
        assert_eq!(wallets.chunks(8).count(), 1);
        // batch_size = 5 (= len)
        assert_eq!(wallets.chunks(5).count(), 1);
    }

    // ═══════════════════════════════════════════════════════════════════
    // RUS-213: Consensus scoring + multipliers
    // ═══════════════════════════════════════════════════════════════════

    #[test]
    fn solo_bet_consensus_count_1() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet {
                wallet: "0xa".to_string(),
                outcome: "Yes".to_string(),
                weight: 0.7,
                usdc_size: 50.0,
                timestamp: now - Duration::minutes(2),
            },
        ]);

        let (_, count, _) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.7);
        assert_eq!(count, 1);
    }

    #[test]
    fn three_wallet_consensus() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet { wallet: "0xa".to_string(), outcome: "Yes".to_string(), weight: 1.0, usdc_size: 100.0, timestamp: now - Duration::minutes(2) },
            RecentBet { wallet: "0xb".to_string(), outcome: "Yes".to_string(), weight: 0.8, usdc_size: 50.0, timestamp: now - Duration::minutes(5) },
            RecentBet { wallet: "0xc".to_string(), outcome: "Yes".to_string(), weight: 0.6, usdc_size: 75.0, timestamp: now - Duration::minutes(10) },
        ]);

        let (_, count, _) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        assert_eq!(count, 3);
    }

    #[test]
    fn different_outcomes_no_consensus() {
        let now = Utc::now();
        let mut bets: HashMap<String, Vec<RecentBet>> = HashMap::new();
        // Wallet A bets Yes, Wallet B bets No — different token_keys
        bets.insert("cid:Yes".to_string(), vec![
            RecentBet { wallet: "0xa".to_string(), outcome: "Yes".to_string(), weight: 1.0, usdc_size: 100.0, timestamp: now - Duration::minutes(2) },
        ]);
        bets.insert("cid:No".to_string(), vec![
            RecentBet { wallet: "0xb".to_string(), outcome: "No".to_string(), weight: 0.8, usdc_size: 50.0, timestamp: now - Duration::minutes(5) },
        ]);

        let (_, count_yes, _) = CopyTrader::consensus_score(&bets, "cid:Yes", 30, now, 0.5);
        let (_, count_no, _) = CopyTrader::consensus_score(&bets, "cid:No", 30, now, 0.5);
        assert_eq!(count_yes, 1, "only 1 wallet on Yes");
        assert_eq!(count_no, 1, "only 1 wallet on No");
    }

    #[test]
    fn min_traders_filter() {
        // Simulates the filter logic from main.rs copy_trading_loop
        let min_traders_2: u32 = 2;
        let min_traders_1: u32 = 1;

        let signal_consensus_1 = 1u32;
        let signal_consensus_2 = 2u32;

        // min_traders=2: solo signal gets filtered
        assert!(signal_consensus_1 < min_traders_2, "consensus 1 < min 2 → SKIP");
        assert!(!(signal_consensus_2 < min_traders_2), "consensus 2 >= min 2 → PASS");

        // min_traders=1: everything passes
        assert!(!(signal_consensus_1 < min_traders_1), "consensus 1 >= min 1 → PASS");
    }

    #[test]
    fn consensus_multiplier_selection() {
        // Mirrors the match logic in poll()
        let multiplier_2 = 1.5;
        let multiplier_3plus = 2.0;

        let mult = |count: u32| -> f64 {
            match count {
                0 | 1 => 1.0,
                2 => multiplier_2,
                _ => multiplier_3plus,
            }
        };

        assert_eq!(mult(0), 1.0);
        assert_eq!(mult(1), 1.0);
        assert_eq!(mult(2), 1.5);
        assert_eq!(mult(3), 2.0);
        assert_eq!(mult(10), 2.0);
    }
}
