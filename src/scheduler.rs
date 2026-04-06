use std::collections::{HashMap, HashSet};
use std::path::Path;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::clob::types::{GammaSportsEvent, WalletPosition};
use crate::config::WatchlistEntry;

/// An upcoming game parsed from the Gamma API
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpcomingGame {
    pub event_slug: String,
    pub title: String,
    pub start_time: DateTime<Utc>,
    pub condition_ids: Vec<String>,
    /// Per-market tokens: (condition_id, question, [(outcome, token_id)])
    /// The question allows distinguishing draw markets from win markets.
    pub market_tokens: Vec<(String, String, Vec<(String, String)>)>,
    pub sport_tag: String,
}

/// Schedule of upcoming games, refreshed periodically
pub struct GameSchedule {
    pub games: Vec<UpcomingGame>,
    pub last_refresh: DateTime<Utc>,
}

impl GameSchedule {
    pub fn new() -> Self {
        Self {
            games: Vec::new(),
            last_refresh: DateTime::UNIX_EPOCH,
        }
    }

    /// Load schedule from disk (best-effort, returns empty on failure)
    pub fn load_from_disk(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(data) => match serde_json::from_str::<Vec<UpcomingGame>>(&data) {
                Ok(games) => {
                    info!("SCHED: loaded {} games from disk cache", games.len());
                    Self { games, last_refresh: DateTime::UNIX_EPOCH }
                }
                Err(e) => {
                    warn!("SCHED: failed to parse disk cache: {}", e);
                    Self::new()
                }
            }
            Err(_) => Self::new(),
        }
    }

    /// Save schedule to disk (best-effort, logs warning on failure)
    pub fn save_to_disk(&self, path: &Path) {
        match serde_json::to_string(&self.games) {
            Ok(data) => {
                if let Err(e) = std::fs::write(path, data) {
                    warn!("SCHED: failed to write disk cache: {}", e);
                }
            }
            Err(e) => warn!("SCHED: failed to serialize schedule: {}", e),
        }
    }

    /// Filter games starting between now and now + minutes
    pub fn games_starting_within(&self, minutes: i64) -> Vec<&UpcomingGame> {
        let now = Utc::now();
        let cutoff = now + chrono::Duration::minutes(minutes);
        // Include games that started up to 2 hours ago (still in-play, positions still tradeable)
        let lookback = now - chrono::Duration::hours(2);
        self.games
            .iter()
            .filter(|g| g.start_time > lookback && g.start_time <= cutoff)
            .collect()
    }

    /// Check if the schedule needs refreshing
    pub fn needs_refresh(&self, max_age_minutes: i64) -> bool {
        let age = Utc::now().signed_duration_since(self.last_refresh);
        age.num_minutes() >= max_age_minutes
    }

    /// Find the "No" token for the opponent's win market.
    ///
    /// Given:
    /// - `event_slug`: the game to look up
    /// - `hauptbet_cid`: the condition_id of Team A's win market (to exclude)
    /// - `draw_cids`: condition_ids that belong to draw markets (to exclude)
    ///
    /// Returns `(opponent_condition_id, no_token_id)` for the first remaining
    /// win-type condition (i.e. Team B's win market).
    pub fn find_opponent_no_token(
        &self,
        event_slug: &str,
        hauptbet_cid: &str,
        draw_cids: &[&str],
    ) -> Option<(String, String)> {
        let game = self.games.iter().find(|g| g.event_slug == event_slug)?;
        for (cid, question, tokens) in &game.market_tokens {
            if cid == hauptbet_cid { continue; }
            if draw_cids.contains(&cid.as_str()) { continue; }
            // Skip draw markets by question text (e.g. "will it be a draw?")
            if question.to_lowercase().contains("draw") { continue; }
            // This should be the opponent's win condition
            if let Some((_, no_tok)) = tokens.iter().find(|(o, _)| o.eq_ignore_ascii_case("No")) {
                return Some((cid.clone(), no_tok.clone()));
            }
        }
        None
    }

    /// Find the "Yes" token for a given condition_id.
    pub fn find_yes_token(&self, event_slug: &str, condition_id: &str) -> Option<String> {
        let game = self.games.iter().find(|g| g.event_slug == event_slug)?;
        for (cid, _question, tokens) in &game.market_tokens {
            if cid != condition_id { continue; }
            return tokens.iter()
                .find(|(o, _)| o.eq_ignore_ascii_case("Yes"))
                .map(|(_, tid)| tid.clone());
        }
        None
    }
}

/// A game we're watching -- discovered at T-30, waiting for T-5 confirm
#[derive(Debug, Clone)]
pub struct WatchedGame {
    pub event_slug: String,
    pub title: String,
    pub start_time: DateTime<Utc>,
    pub sport_tag: String,
    /// Snapshot from T-30: wallet_address -> Vec<WalletPosition>
    pub wallet_snapshots: HashMap<String, Vec<WalletPosition>>,
    pub discovered_at: DateTime<Utc>,
}

/// Confirmed match at T-5, ready for execution
pub struct T5Match {
    pub wallet_name: String,
    pub wallet_address: String,
    pub game_event_slug: String,
    pub positions: Vec<WalletPosition>,  // current positions (T-5 fetch)
    pub t30_position_count: usize,       // how many positions at T-30 (for logging)
}

/// Fetch upcoming sports events and build a schedule.
/// Derives sport tags from ALL unique league values across watchlist entries.
pub async fn refresh_schedule(
    client: &ClobClient,
    sport_tags: &[String],
    schedule: &mut GameSchedule,
) {
    let mut games = Vec::new();
    let mut seen_slugs: HashSet<String> = HashSet::new();

    for tag in sport_tags {
        match client.search_sports_events(tag).await {
            Ok(events) => {
                for event in events {
                    if let Some(game) = parse_event(&event, tag) {
                        // Dedup by event_slug (same event can appear under multiple tags)
                        if seen_slugs.insert(game.event_slug.clone()) {
                            games.push(game);
                        }
                    }
                }
            }
            Err(e) => {
                warn!("SCHED: failed to fetch events for tag '{}': {}", tag, e);
            }
        }
    }

    let count = games.len();
    schedule.games = games;
    schedule.last_refresh = Utc::now();
    schedule.save_to_disk(Path::new("data/schedule_cache.json"));
    info!("SCHED: schedule refreshed -- {} upcoming games from {} tags", count, sport_tags.len());
}

fn parse_event(event: &GammaSportsEvent, sport_tag: &str) -> Option<UpcomingGame> {
    let slug = event.slug.as_deref()?;
    let title = event.title.as_deref().unwrap_or("").to_string();

    // Prefer startTime (correct field from Gamma API), fall back to startDate
    let start_str = event.start_time.as_deref()
        .or(event.start_date.as_deref())?;

    let start_time = chrono::DateTime::parse_from_rfc3339(start_str)
        .or_else(|_| chrono::DateTime::parse_from_str(start_str, "%Y-%m-%dT%H:%M:%S%.fZ"))
        .ok()?
        .with_timezone(&Utc);

    let mut condition_ids = Vec::new();
    let mut market_tokens = Vec::new();

    if let Some(markets) = &event.markets {
        for market in markets {
            if let Some(cid) = &market.condition_id {
                condition_ids.push(cid.clone());
                if let Some(tokens) = &market.tokens {
                    let question = market.question.as_deref().unwrap_or("").to_lowercase();
                    let pairs: Vec<(String, String)> = tokens.iter()
                        .filter_map(|t| {
                            if let (Some(o), Some(tid)) = (&t.outcome, &t.token_id) {
                                Some((o.clone(), tid.clone()))
                            } else {
                                None
                            }
                        })
                        .collect();
                    market_tokens.push((cid.clone(), question, pairs));
                }
            }
        }
    }

    if condition_ids.is_empty() {
        return None;
    }

    Some(UpcomingGame {
        event_slug: slug.to_string(),
        title,
        start_time,
        condition_ids,
        market_tokens,
        sport_tag: sport_tag.to_string(),
    })
}

/// Derive unique sport tags from all watchlist entries' leagues.
pub fn sport_tags_from_watchlist(watchlist: &[WatchlistEntry]) -> Vec<String> {
    let tags: HashSet<String> = watchlist.iter()
        .flat_map(|w| w.leagues.iter().cloned())
        .collect();
    tags.into_iter().collect()
}

/// Continuous discovery: check ALL upcoming games (next 24h) for Cannae positions.
/// Runs every poll cycle. Only returns games not already in watched_games.
/// Hauptbet is NOT determined here — only at T-5.
///
/// Uses pre-fetched positions from the poll loop (no extra API calls).
pub fn discover_continuous_from_positions(
    schedule: &GameSchedule,
    watchlist: &[WatchlistEntry],
    already_watched: &HashSet<String>,
    raw_positions: &[(String, String, Vec<WalletPosition>)],  // (address, name, positions)
) -> Vec<WatchedGame> {
    // All games starting in the next 24 hours
    let upcoming = schedule.games_starting_within(24 * 60);

    if upcoming.is_empty() {
        return Vec::new();
    }

    // Filter out games already watched
    let new_games: Vec<&&UpcomingGame> = upcoming.iter()
        .filter(|g| !already_watched.contains(&g.event_slug))
        .collect();

    if new_games.is_empty() {
        return Vec::new();
    }

    // Build condition_id -> (event_slug, game_ref) mapping
    let mut cid_to_event: HashMap<String, String> = HashMap::new();
    let mut game_by_slug: HashMap<String, &UpcomingGame> = HashMap::new();
    for game in &new_games {
        for cid in &game.condition_ids {
            cid_to_event.insert(cid.clone(), game.event_slug.clone());
        }
        game_by_slug.insert(game.event_slug.clone(), game);
    }

    // Use pre-fetched positions (from poll loop, already paginated)
    let mut watched_games: HashMap<String, WatchedGame> = HashMap::new();

    for (wallet_addr, wallet_name, positions) in raw_positions {
        let wallet = match watchlist.iter().find(|w| w.address.eq_ignore_ascii_case(wallet_addr)) {
            Some(w) => w,
            None => continue,
        };

        // Group matching positions by event_slug
        // Match on condition_id first, fall back to position's own eventSlug
        let mut by_event: HashMap<String, Vec<WalletPosition>> = HashMap::new();
        for pos in positions {
            if pos.size_f64() <= 0.0 {
                continue;
            }
            let cur = pos.cur_price_f64();
            if cur <= 0.01 || cur >= 0.99 {
                continue;
            }
            // Try condition_id match first
            let matched_slug = pos.condition_id.as_ref()
                .and_then(|cid| cid_to_event.get(cid).cloned())
                // Fall back: use position's own eventSlug if it matches a scheduled game
                .or_else(|| {
                    pos.event_slug.as_ref()
                        .filter(|slug| game_by_slug.contains_key(slug.as_str()))
                        .cloned()
                });
            if let Some(event_slug) = matched_slug {
                by_event.entry(event_slug).or_default().push(pos.clone());
            }
        }

        for (event_slug, event_positions) in by_event {
            let game_ref = match game_by_slug.get(&event_slug) {
                Some(g) => g,
                None => continue,
            };

            // League filter: check if this wallet is allowed to trade this league
            if !wallet.leagues.is_empty() {
                let league_prefix = event_slug.split('-').next().unwrap_or("");
                if !wallet.leagues.iter().any(|l| l == league_prefix) {
                    continue;
                }
            }

            info!(
                "DISCOVER: {} has {} positions in {} (kickoff {} UTC, ~{}min)",
                wallet_name,
                event_positions.len(),
                event_slug,
                game_ref.start_time.format("%H:%M"),
                game_ref.start_time.signed_duration_since(Utc::now()).num_minutes(),
            );

            let watched = watched_games.entry(event_slug.clone()).or_insert_with(|| {
                WatchedGame {
                    event_slug: event_slug.clone(),
                    title: game_ref.title.clone(),
                    start_time: game_ref.start_time,
                    sport_tag: game_ref.sport_tag.clone(),
                    wallet_snapshots: HashMap::new(),
                    discovered_at: Utc::now(),
                }
            });

            watched.wallet_snapshots.insert(wallet_addr.to_lowercase(), event_positions);
        }
    }

    watched_games.into_values().collect()
}

/// T-5 Confirm + Execute: For watched games starting in ~5 min,
/// re-fetch wallet positions, compare with T-30 snapshot, return confirmed matches.
/// Uses pre-fetched positions from the poll loop (no extra API calls).
pub fn confirm_and_execute_t5(
    watched_games: &[WatchedGame],
    watchlist: &[WatchlistEntry],
    t5_minutes: u32,
    t5_executed: &HashSet<String>,
    raw_positions: &[(String, String, Vec<WalletPosition>)],
) -> Vec<T5Match> {
    let now = Utc::now();
    let mut matches = Vec::new();

    // Filter to games starting in 0..t5_minutes+2 window (with margin)
    let window_max = chrono::Duration::minutes((t5_minutes as i64) + 2);
    let due_games: Vec<&WatchedGame> = watched_games.iter()
        .filter(|g| {
            let until_start = g.start_time.signed_duration_since(now);
            until_start >= chrono::Duration::zero() && until_start <= window_max
        })
        .filter(|g| !t5_executed.contains(&g.event_slug))
        .collect();

    if due_games.is_empty() {
        return Vec::new();
    }

    // Build wallet positions map from pre-fetched data (already paginated)
    let mut wallet_positions: HashMap<String, Vec<WalletPosition>> = HashMap::new();
    for (addr, _name, positions) in raw_positions {
        wallet_positions.insert(addr.to_lowercase(), positions.clone());
    }

    // Strip "-more-markets" for slug matching
    let strip_more = |s: &str| s.trim_end_matches("-more-markets").to_string();

    for game in &due_games {
        // SSOT Pilaar 1, Regel 2 — forbidden slug suffixes (rules.yaml).
        // Skip "-more-markets" events — same game, different condition_ids.
        // Positions are already matched via strip_more() on the base event.
        if crate::rules::is_forbidden_slug(&game.event_slug) {
            continue;
        }
        let game_slug_base = strip_more(&game.event_slug);

        // Try ALL wallets that have positions in this game (not just the first).
        // This ensures that if wallet A's league filter rejects, wallet B gets a chance.
        for wallet_addr in game.wallet_snapshots.keys() {
            let wallet_cfg = match watchlist.iter()
                .find(|w| w.address.to_lowercase() == *wallet_addr)
            {
                Some(w) => w,
                None => continue,
            };

            let current_all = match wallet_positions.get(wallet_addr) {
                Some(p) => p,
                None => continue,
            };

            // Match positions by eventSlug (fresh data, no T-30 snapshot needed)
            let current_game_positions: Vec<WalletPosition> = current_all.iter()
                .filter(|p| {
                    if p.size_f64() <= 0.0 { return false; }
                    let cur = p.cur_price_f64();
                    if cur <= 0.01 || cur >= 0.99 { return false; }
                    p.event_slug.as_ref()
                        .map(|slug| strip_more(slug) == game_slug_base)
                        .unwrap_or(false)
                })
                .cloned()
                .collect();

            if current_game_positions.is_empty() {
                continue;
            }

            let mins_to_start = game.start_time.signed_duration_since(now).num_minutes();
            info!(
                "T5 CONFIRMED: {} has {} positions in {} (starts in {}min)",
                wallet_cfg.name,
                current_game_positions.len(),
                game.event_slug,
                mins_to_start,
            );

            matches.push(T5Match {
                wallet_name: wallet_cfg.name.clone(),
                wallet_address: wallet_addr.clone(),
                game_event_slug: game.event_slug.clone(),
                positions: current_game_positions,
                t30_position_count: 0, // no longer tracking T-30 count
            });
        }
    }

    matches
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn games_starting_within_filters_correctly() {
        let now = Utc::now();
        let schedule = GameSchedule {
            games: vec![
                UpcomingGame {
                    event_slug: "game-soon".to_string(),
                    title: "Soon Game".to_string(),
                    start_time: now + chrono::Duration::minutes(20),
                    condition_ids: vec!["cid1".to_string()],
                    market_tokens: vec![],
                    sport_tag: "nba".to_string(),
                },
                UpcomingGame {
                    event_slug: "game-later".to_string(),
                    title: "Later Game".to_string(),
                    start_time: now + chrono::Duration::minutes(60),
                    condition_ids: vec!["cid2".to_string()],
                    market_tokens: vec![],
                    sport_tag: "nba".to_string(),
                },
                UpcomingGame {
                    event_slug: "game-past".to_string(),
                    title: "Past Game".to_string(),
                    start_time: now - chrono::Duration::minutes(10),
                    condition_ids: vec!["cid3".to_string()],
                    market_tokens: vec![],
                    sport_tag: "nba".to_string(),
                },
            ],
            last_refresh: now,
        };

        let within_30 = schedule.games_starting_within(30);
        assert_eq!(within_30.len(), 1);
        assert_eq!(within_30[0].event_slug, "game-soon");
    }

    #[test]
    fn needs_refresh_works() {
        let schedule = GameSchedule {
            games: vec![],
            last_refresh: Utc::now() - chrono::Duration::minutes(61),
        };
        assert!(schedule.needs_refresh(60));

        let fresh = GameSchedule {
            games: vec![],
            last_refresh: Utc::now() - chrono::Duration::minutes(5),
        };
        assert!(!fresh.needs_refresh(60));
    }

    #[test]
    fn parse_event_extracts_fields() {
        use crate::clob::types::{GammaMarketResponse, GammaTokenResponse};

        let event = GammaSportsEvent {
            id: Some("1".to_string()),
            slug: Some("nba-lal-bos-2026-03-23".to_string()),
            title: Some("Lakers vs Celtics".to_string()),
            description: None,
            start_date: Some("2026-03-23T19:00:00Z".to_string()),
            start_time: None,
            end_date: None,
            markets: Some(vec![GammaMarketResponse {
                condition_id: Some("0xabc".to_string()),
                clob_token_ids: None,
                tokens: Some(vec![
                    GammaTokenResponse {
                        outcome: Some("Yes".to_string()),
                        token_id: Some("tok1".to_string()),
                    },
                    GammaTokenResponse {
                        outcome: Some("No".to_string()),
                        token_id: Some("tok2".to_string()),
                    },
                ]),
                outcome_prices: None,
                question: None,
            }]),
            tags: None,
        };

        let game = parse_event(&event, "nba").unwrap();
        assert_eq!(game.event_slug, "nba-lal-bos-2026-03-23");
        assert_eq!(game.condition_ids, vec!["0xabc"]);
        assert_eq!(game.market_tokens.len(), 1);
        assert_eq!(game.market_tokens[0].2.len(), 2);
        assert_eq!(game.sport_tag, "nba");
    }

    #[test]
    fn parse_event_prefers_start_time_over_start_date() {
        use crate::clob::types::{GammaMarketResponse, GammaTokenResponse};

        let event = GammaSportsEvent {
            id: Some("1".to_string()),
            slug: Some("nba-lal-bos-2026-03-23".to_string()),
            title: Some("Lakers vs Celtics".to_string()),
            description: None,
            start_date: Some("2026-03-23T18:00:00Z".to_string()),
            start_time: Some("2026-03-23T19:30:00Z".to_string()),
            end_date: None,
            markets: Some(vec![GammaMarketResponse {
                condition_id: Some("0xabc".to_string()),
                clob_token_ids: None,
                tokens: Some(vec![GammaTokenResponse {
                    outcome: Some("Yes".to_string()),
                    token_id: Some("tok1".to_string()),
                }]),
                outcome_prices: None,
                question: None,
            }]),
            tags: None,
        };

        let game = parse_event(&event, "nba").unwrap();
        // Should use startTime (19:30), not startDate (18:00)
        use chrono::Timelike;
        assert_eq!(game.start_time.hour(), 19);
        assert_eq!(game.start_time.minute(), 30);
    }

    #[test]
    fn sport_tags_from_watchlist_deduplicates() {
        let watchlist = vec![
            WatchlistEntry {
                address: "0xaaa".to_string(),
                name: "W1".to_string(),
                weight: 1.0,
                sports: vec!["all".to_string()],
                leagues: vec!["epl".to_string(), "nba".to_string()],
                market_types: vec![],
                max_legs_per_event: 1,
                min_price: None,
                max_price: None,
            },
            WatchlistEntry {
                address: "0xbbb".to_string(),
                name: "W2".to_string(),
                weight: 1.0,
                sports: vec!["all".to_string()],
                leagues: vec!["nba".to_string(), "nhl".to_string()],
                market_types: vec![],
                max_legs_per_event: 1,
                min_price: None,
                max_price: None,
            },
        ];

        let tags = sport_tags_from_watchlist(&watchlist);
        assert_eq!(tags.len(), 3); // epl, nba, nhl (deduplicated)
        assert!(tags.contains(&"epl".to_string()));
        assert!(tags.contains(&"nba".to_string()));
        assert!(tags.contains(&"nhl".to_string()));
    }
}
