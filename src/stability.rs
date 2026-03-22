use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Utc};
use tracing::info;

use crate::clob::types::WalletPosition;

/// Tracks Cannae's positions per event and detects when they stabilize
/// (GTC orders fully filled). Only emits a game for trading once positions
/// have been stable (< threshold% change) for the configured window.
pub struct StabilityTracker {
    /// Per event_slug: tracking state for pending games
    pending: HashMap<String, PendingGame>,
    /// Events that have been emitted or are already in our portfolio (never re-emit)
    emitted: HashSet<String>,
}

struct PendingGame {
    first_seen: DateTime<Utc>,
    /// Previous snapshot: position_key (conditionId:outcome) → shares
    prev_snapshot: HashMap<String, f64>,
    /// When all legs became stable (None = not yet stable)
    stable_since: Option<DateTime<Utc>>,
    /// Source wallet info
    source_wallet: String,
    source_name: String,
}

/// A game that has been confirmed stable and is ready for trading
pub struct StableGame {
    pub event_slug: String,
    pub positions: Vec<WalletPosition>,
    pub source_wallet: String,
    pub source_name: String,
}

impl StabilityTracker {
    pub fn new() -> Self {
        Self {
            pending: HashMap::new(),
            emitted: HashSet::new(),
        }
    }

    /// Seed events we already hold positions in (from PM positions at startup).
    /// These will never enter the pending queue.
    pub fn seed_emitted(&mut self, event_slugs: impl Iterator<Item = String>) {
        for slug in event_slugs {
            if !slug.is_empty() {
                self.emitted.insert(slug);
            }
        }
        info!("STABILITY: seeded {} already-traded events", self.emitted.len());
    }

    /// Update with Cannae's current positions. Call every poll cycle.
    /// Groups positions by event_slug and tracks stability per event.
    /// `allowed_leagues`: if non-empty, only track events whose slug prefix matches.
    pub fn update(
        &mut self,
        wallet_addr: &str,
        wallet_name: &str,
        positions: &[WalletPosition],
        our_event_slugs: &HashSet<String>,
        threshold_pct: f64,
        allowed_leagues: &[String],
    ) {
        // Group Cannae positions by event_slug (only open, unresolved)
        let mut by_event: HashMap<String, Vec<&WalletPosition>> = HashMap::new();
        for pos in positions {
            let cur = pos.cur_price_f64();
            if cur <= 0.01 || cur >= 0.99 {
                continue;
            }
            if pos.size_f64() <= 0.0 {
                continue;
            }

            let event_slug = pos
                .event_slug
                .clone()
                .or_else(|| pos.slug.clone())
                .unwrap_or_default()
                .trim_end_matches("-more-markets")
                .to_string();
            if event_slug.is_empty() {
                continue;
            }

            by_event.entry(event_slug).or_default().push(pos);
        }

        let now = Utc::now();

        for (event_slug, event_positions) in &by_event {
            // League filter: event_slug format is "{league}-{teams}-{date}"
            // Skip events whose league prefix is not in the allowed list.
            if !allowed_leagues.is_empty() {
                let league_prefix = event_slug.split('-').next().unwrap_or("");
                if !allowed_leagues.iter().any(|l| l == league_prefix) {
                    continue;
                }
            }

            // Skip events we already traded or hold
            if self.emitted.contains(event_slug) {
                continue;
            }
            if our_event_slugs.contains(event_slug) {
                continue;
            }

            // Build current snapshot: position_key → shares
            let mut current: HashMap<String, f64> = HashMap::new();
            for pos in event_positions {
                let key = pos.position_key();
                if key == ":" {
                    continue;
                }
                let shares = pos.size_f64();
                let entry = current.entry(key).or_insert(0.0);
                if shares > *entry {
                    *entry = shares;
                }
            }

            // Get or create pending entry
            let pending = self.pending.entry(event_slug.clone()).or_insert_with(|| {
                info!(
                    "STABILITY: new event {} from {} ({} legs)",
                    event_slug, wallet_name, current.len()
                );
                PendingGame {
                    first_seen: now,
                    prev_snapshot: HashMap::new(),
                    stable_since: None,
                    source_wallet: wallet_addr.to_string(),
                    source_name: wallet_name.to_string(),
                }
            });

            if pending.prev_snapshot.is_empty() {
                // First snapshot — store and wait for next cycle
                pending.prev_snapshot = current;
                continue;
            }

            // Check stability: compare all legs with previous snapshot
            let mut is_stable = true;
            let all_keys: HashSet<&String> =
                pending.prev_snapshot.keys().chain(current.keys()).collect();

            for key in &all_keys {
                let prev = pending.prev_snapshot.get(*key).copied().unwrap_or(0.0);
                let curr = current.get(*key).copied().unwrap_or(0.0);

                if prev == 0.0 && curr > 0.0 {
                    // New leg appeared
                    is_stable = false;
                    break;
                }
                if curr == 0.0 && prev > 0.0 {
                    // Leg disappeared
                    is_stable = false;
                    break;
                }
                if prev > 0.0 {
                    let change_pct = ((curr - prev) / prev).abs() * 100.0;
                    if change_pct > threshold_pct {
                        is_stable = false;
                        break;
                    }
                }
            }

            if is_stable {
                if pending.stable_since.is_none() {
                    pending.stable_since = Some(now);
                    info!(
                        "STABILITY: {} became stable ({} legs)",
                        event_slug,
                        current.len()
                    );
                }
            } else {
                if pending.stable_since.is_some() {
                    info!("STABILITY: {} destabilized (positions changed)", event_slug);
                }
                pending.stable_since = None;
            }

            // Update snapshot
            pending.prev_snapshot = current;
        }
    }

    /// Drain events that have been stable for the required window.
    /// Returns stable games with their current Cannae positions.
    pub fn drain_stable(
        &mut self,
        window_minutes: u32,
        all_positions: &[WalletPosition],
    ) -> Vec<StableGame> {
        let now = Utc::now();
        let window = chrono::Duration::minutes(window_minutes as i64);
        let mut ready = Vec::new();
        let mut to_remove = Vec::new();

        for (event_slug, pending) in &self.pending {
            // Prune stale entries (no stability after 4 hours)
            if now.signed_duration_since(pending.first_seen) > chrono::Duration::hours(4) {
                info!(
                    "STABILITY: abandoned {} after 4h without stabilizing",
                    event_slug
                );
                to_remove.push(event_slug.clone());
                continue;
            }

            if let Some(stable_since) = pending.stable_since {
                if now.signed_duration_since(stable_since) >= window {
                    // Game is stable! Collect current positions for this event
                    let positions: Vec<WalletPosition> = all_positions
                        .iter()
                        .filter(|p| {
                            let es = p
                                .event_slug
                                .clone()
                                .or_else(|| p.slug.clone())
                                .unwrap_or_default()
                                .trim_end_matches("-more-markets")
                                .to_string();
                            &es == event_slug
                                && p.cur_price_f64() > 0.01
                                && p.cur_price_f64() < 0.99
                                && p.size_f64() > 0.0
                        })
                        .cloned()
                        .collect();

                    if !positions.is_empty() {
                        let wait_mins = now
                            .signed_duration_since(pending.first_seen)
                            .num_minutes();
                        info!(
                            "STABILITY: {} READY after {}min ({} legs, stable for {}min)",
                            event_slug,
                            wait_mins,
                            positions.len(),
                            window_minutes
                        );
                        ready.push(StableGame {
                            event_slug: event_slug.clone(),
                            positions,
                            source_wallet: pending.source_wallet.clone(),
                            source_name: pending.source_name.clone(),
                        });
                    }
                    to_remove.push(event_slug.clone());
                }
            }
        }

        for slug in to_remove {
            self.pending.remove(&slug);
            self.emitted.insert(slug);
        }

        ready
    }

    /// Like drain_stable, but does NOT modify state.
    /// Call confirm_emitted() after external checks pass.
    pub fn get_ready_candidates(
        &self,
        window_minutes: u32,
        all_positions: &[WalletPosition],
    ) -> Vec<StableGame> {
        let now = Utc::now();
        let window = chrono::Duration::minutes(window_minutes as i64);
        let mut ready = Vec::new();

        for (event_slug, pending) in &self.pending {
            // Skip stale entries (>4h without stabilizing) — cleanup happens in prune_stale()
            if now.signed_duration_since(pending.first_seen) > chrono::Duration::hours(4) {
                continue;
            }

            if let Some(stable_since) = pending.stable_since {
                if now.signed_duration_since(stable_since) >= window {
                    let positions: Vec<WalletPosition> = all_positions
                        .iter()
                        .filter(|p| {
                            let es = p
                                .event_slug
                                .clone()
                                .or_else(|| p.slug.clone())
                                .unwrap_or_default()
                                .trim_end_matches("-more-markets")
                                .to_string();
                            &es == event_slug
                                && p.cur_price_f64() > 0.01
                                && p.cur_price_f64() < 0.99
                                && p.size_f64() > 0.0
                        })
                        .cloned()
                        .collect();

                    if !positions.is_empty() {
                        ready.push(StableGame {
                            event_slug: event_slug.clone(),
                            positions,
                            source_wallet: pending.source_wallet.clone(),
                            source_name: pending.source_name.clone(),
                        });
                    }
                }
            }
        }

        ready
    }

    /// Confirm that a candidate has been executed — move from pending to emitted.
    pub fn confirm_emitted(&mut self, slug: &str) {
        self.pending.remove(slug);
        self.emitted.insert(slug.to_string());
        info!("STABILITY: {} confirmed emitted", slug);
    }

    /// Prune stale pending entries (>4h without stabilizing).
    /// Call periodically from main loop.
    pub fn prune_stale(&mut self) {
        let now = Utc::now();
        let stale: Vec<String> = self.pending.iter()
            .filter(|(_, p)| now.signed_duration_since(p.first_seen) > chrono::Duration::hours(4))
            .map(|(slug, _)| slug.clone())
            .collect();
        for slug in stale {
            info!("STABILITY: abandoned {} after 4h without stabilizing", slug);
            self.pending.remove(&slug);
        }
    }

    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    pub fn pending_summary(&self) -> Vec<(String, i64, bool)> {
        let now = Utc::now();
        self.pending
            .iter()
            .map(|(slug, p)| {
                let wait_mins = now.signed_duration_since(p.first_seen).num_minutes();
                (slug.clone(), wait_mins, p.stable_since.is_some())
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_position(
        condition_id: &str,
        outcome: &str,
        size: f64,
        avg_price: f64,
        event_slug: &str,
        title: &str,
    ) -> WalletPosition {
        WalletPosition {
            asset: Some("token123".to_string()),
            condition_id: Some(condition_id.to_string()),
            title: Some(title.to_string()),
            slug: None,
            outcome: Some(outcome.to_string()),
            size: Some(serde_json::Value::Number(
                serde_json::Number::from_f64(size).unwrap(),
            )),
            avg_price: Some(serde_json::Value::Number(
                serde_json::Number::from_f64(avg_price).unwrap(),
            )),
            current_value: None,
            cash_pnl: None,
            proxy_wallet: None,
            event_slug: Some(event_slug.to_string()),
            cur_price: Some(serde_json::Value::Number(
                serde_json::Number::from_f64(0.5).unwrap(),
            )),
            initial_value: Some(serde_json::Value::Number(
                serde_json::Number::from_f64(size * avg_price).unwrap(),
            )),
        }
    }

    #[test]
    fn new_event_enters_pending() {
        let mut tracker = StabilityTracker::new();
        let positions = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
        ];
        let our_events = HashSet::new();

        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);
        assert_eq!(tracker.pending_count(), 1);

        // First update: just stores snapshot, no stability yet
        let stable = tracker.drain_stable(0, &positions);
        assert!(stable.is_empty(), "first snapshot should not be stable yet");
    }

    #[test]
    fn stable_after_no_change() {
        let mut tracker = StabilityTracker::new();
        let positions = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
            make_position("cid2", "No", 50.0, 0.30, "test-event", "Will Y win?"),
        ];
        let our_events = HashSet::new();

        // First update: stores snapshot
        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);

        // Second update: same positions → stable
        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);

        // With window=0, should be immediately ready
        let stable = tracker.drain_stable(0, &positions);
        assert_eq!(stable.len(), 1);
        assert_eq!(stable[0].event_slug, "test-event");
    }

    #[test]
    fn destabilizes_on_new_leg() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();

        let positions_v1 = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
        ];

        // First + second update: stable
        tracker.update("0xCannae", "Cannae", &positions_v1, &our_events, 5.0, &[]);
        tracker.update("0xCannae", "Cannae", &positions_v1, &our_events, 5.0, &[]);

        // Third update: new leg appears → destabilize
        let positions_v2 = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
            make_position("cid2", "No", 50.0, 0.30, "test-event", "Will Y win?"),
        ];
        tracker.update("0xCannae", "Cannae", &positions_v2, &our_events, 5.0, &[]);

        let stable = tracker.drain_stable(0, &positions_v2);
        assert!(stable.is_empty(), "should not be stable after new leg");
    }

    #[test]
    fn destabilizes_on_size_change() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();

        let positions_v1 = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
        ];

        tracker.update("0xCannae", "Cannae", &positions_v1, &our_events, 5.0, &[]);
        tracker.update("0xCannae", "Cannae", &positions_v1, &our_events, 5.0, &[]);

        // Size changed by 10% (> 5% threshold)
        let positions_v2 = vec![
            make_position("cid1", "Yes", 110.0, 0.50, "test-event", "Will X win?"),
        ];
        tracker.update("0xCannae", "Cannae", &positions_v2, &our_events, 5.0, &[]);

        let stable = tracker.drain_stable(0, &positions_v2);
        assert!(stable.is_empty(), "should not be stable after 10% size change");
    }

    #[test]
    fn skips_already_held_events() {
        let mut tracker = StabilityTracker::new();
        let mut our_events = HashSet::new();
        our_events.insert("test-event".to_string());

        let positions = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
        ];

        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);
        assert_eq!(tracker.pending_count(), 0, "should skip events we already hold");
    }

    #[test]
    fn never_re_emits_same_event() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();

        let positions = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "test-event", "Will X win?"),
        ];

        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);
        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);

        let stable = tracker.drain_stable(0, &positions);
        assert_eq!(stable.len(), 1);

        // Same event should not re-enter pending
        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &[]);
        assert_eq!(tracker.pending_count(), 0);
    }

    #[test]
    fn league_filter_blocks_non_allowed_events() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();
        let allowed = vec!["epl".to_string(), "bun".to_string()];

        // NBA event should be blocked
        let nba = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "nba-bos-lal-2026-03-22", "Will Boston Celtics win?"),
        ];
        tracker.update("0xCannae", "Cannae", &nba, &our_events, 5.0, &allowed);
        assert_eq!(tracker.pending_count(), 0, "NBA event should be filtered out");

        // NHL event should be blocked
        let nhl = vec![
            make_position("cid2", "Yes", 100.0, 0.50, "nhl-bos-tor-2026-03-22", "Will Boston Bruins win?"),
        ];
        tracker.update("0xCannae", "Cannae", &nhl, &our_events, 5.0, &allowed);
        assert_eq!(tracker.pending_count(), 0, "NHL event should be filtered out");

        // EPL event should be allowed
        let epl = vec![
            make_position("cid3", "Yes", 100.0, 0.50, "epl-ars-che-2026-03-22", "Will Arsenal win?"),
        ];
        tracker.update("0xCannae", "Cannae", &epl, &our_events, 5.0, &allowed);
        assert_eq!(tracker.pending_count(), 1, "EPL event should be allowed");
    }

    #[test]
    fn league_filter_allows_all_when_empty() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();
        let allowed: Vec<String> = vec![]; // empty = allow all

        let nba = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "nba-bos-lal-2026-03-22", "Will Boston Celtics win?"),
        ];
        tracker.update("0xCannae", "Cannae", &nba, &our_events, 5.0, &allowed);
        assert_eq!(tracker.pending_count(), 1, "empty allowed_leagues should allow all");
    }

    #[test]
    fn league_filter_allows_configured_football_leagues() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();
        let allowed = vec![
            "epl".to_string(), "bun".to_string(), "lal".to_string(),
            "fl1".to_string(), "uel".to_string(),
        ];

        // All configured leagues should work
        let positions = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "epl-ars-che-2026-03-22", "Will Arsenal win?"),
            make_position("cid2", "Yes", 100.0, 0.50, "bun-bay-dor-2026-03-22", "Will Bayern win?"),
            make_position("cid3", "Yes", 100.0, 0.50, "lal-bar-mad-2026-03-22", "Will Barcelona win?"),
        ];

        tracker.update("0xCannae", "Cannae", &positions, &our_events, 5.0, &allowed);
        assert_eq!(tracker.pending_count(), 3, "all configured football leagues should be allowed");
    }

    #[test]
    fn league_filter_stable_game_only_allowed_leagues() {
        let mut tracker = StabilityTracker::new();
        let our_events = HashSet::new();
        let allowed = vec!["epl".to_string()];

        let epl = vec![
            make_position("cid1", "Yes", 100.0, 0.50, "epl-ars-che-2026-03-22", "Will Arsenal win?"),
        ];
        let nba = vec![
            make_position("cid2", "Yes", 100.0, 0.50, "nba-bos-lal-2026-03-22", "Will Celtics win?"),
        ];

        // First + second update for EPL (becomes stable)
        tracker.update("0xCannae", "Cannae", &epl, &our_events, 5.0, &allowed);
        tracker.update("0xCannae", "Cannae", &epl, &our_events, 5.0, &allowed);

        // NBA tries to enter — should be blocked
        tracker.update("0xCannae", "Cannae", &nba, &our_events, 5.0, &allowed);

        let all_positions: Vec<_> = epl.iter().chain(nba.iter()).cloned().collect();
        let stable = tracker.drain_stable(0, &all_positions);
        assert_eq!(stable.len(), 1, "only EPL event should be stable");
        assert_eq!(stable[0].event_slug, "epl-ars-che-2026-03-22");
        assert_eq!(tracker.pending_count(), 0, "no NBA event in pending");
    }
}
