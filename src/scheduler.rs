use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Utc};
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::clob::types::{GammaSportsEvent, WalletPosition};
use crate::config::WatchlistEntry;

/// An upcoming game parsed from the Gamma API
#[derive(Debug, Clone)]
pub struct UpcomingGame {
    pub event_slug: String,
    pub title: String,
    pub start_time: DateTime<Utc>,
    pub condition_ids: Vec<String>,
    pub token_ids: Vec<(String, String)>, // (outcome, token_id)
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

    /// Filter games starting between now and now + minutes
    pub fn games_starting_within(&self, minutes: i64) -> Vec<&UpcomingGame> {
        let now = Utc::now();
        let cutoff = now + chrono::Duration::minutes(minutes);
        self.games
            .iter()
            .filter(|g| g.start_time > now && g.start_time <= cutoff)
            .collect()
    }

    /// Filter games starting between now+from_minutes and now+to_minutes
    pub fn games_starting_between(&self, from_minutes: i64, to_minutes: i64) -> Vec<&UpcomingGame> {
        let now = Utc::now();
        let from = now + chrono::Duration::minutes(from_minutes);
        let to = now + chrono::Duration::minutes(to_minutes);
        self.games
            .iter()
            .filter(|g| g.start_time >= from && g.start_time <= to)
            .collect()
    }

    /// Check if the schedule needs refreshing
    pub fn needs_refresh(&self, max_age_minutes: i64) -> bool {
        let age = Utc::now().signed_duration_since(self.last_refresh);
        age.num_minutes() >= max_age_minutes
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
    let mut token_ids = Vec::new();

    if let Some(markets) = &event.markets {
        for market in markets {
            if let Some(cid) = &market.condition_id {
                condition_ids.push(cid.clone());
            }
            if let Some(tokens) = &market.tokens {
                for token in tokens {
                    if let (Some(outcome), Some(tid)) = (&token.outcome, &token.token_id) {
                        token_ids.push((outcome.clone(), tid.clone()));
                    }
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
        token_ids,
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

/// T-30 Discovery: For games starting in ~30 min window, check all wallet positions
/// and store snapshots. Returns newly discovered WatchedGames.
pub async fn discover_t30(
    client: &ClobClient,
    schedule: &GameSchedule,
    watchlist: &[WatchlistEntry],
    t_minus_minutes: u32,
    already_watched: &HashSet<String>,
) -> Vec<WatchedGame> {
    // Games starting between 25 and 35 min from now (centred on t_minus_minutes)
    let window_lo = (t_minus_minutes as i64) - 5;
    let window_hi = (t_minus_minutes as i64) + 5;
    let upcoming = schedule.games_starting_between(window_lo, window_hi);

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

    // For each wallet, fetch positions and check for matches
    let mut watched_games: HashMap<String, WatchedGame> = HashMap::new();

    for wallet in watchlist {
        let positions = match client.get_wallet_positions(&wallet.address, 500).await {
            Ok(p) => p,
            Err(e) => {
                warn!("T30 DISCOVER: failed to fetch positions for {}: {}", wallet.name, e);
                continue;
            }
        };

        // Group matching positions by event_slug
        let mut by_event: HashMap<String, Vec<WalletPosition>> = HashMap::new();
        for pos in &positions {
            if pos.size_f64() <= 0.0 {
                continue;
            }
            let cur = pos.cur_price_f64();
            if cur <= 0.01 || cur >= 0.99 {
                continue;
            }
            if let Some(cid) = &pos.condition_id {
                if let Some(event_slug) = cid_to_event.get(cid) {
                    by_event.entry(event_slug.clone()).or_default().push(pos.clone());
                }
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
                "T30 DISCOVER: {} has {} positions in {} (starts in ~{}min)",
                wallet.name,
                event_positions.len(),
                event_slug,
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

            watched.wallet_snapshots.insert(wallet.address.to_lowercase(), event_positions);
        }
    }

    watched_games.into_values().collect()
}

/// T-5 Confirm + Execute: For watched games starting in ~5 min,
/// re-fetch wallet positions, compare with T-30 snapshot, return confirmed matches.
pub async fn confirm_and_execute_t5(
    client: &ClobClient,
    watched_games: &[WatchedGame],
    watchlist: &[WatchlistEntry],
    t5_minutes: u32,
    t5_executed: &HashSet<String>,
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

    // Collect all wallets that have snapshots across due games
    let wallet_addrs: HashSet<String> = due_games.iter()
        .flat_map(|g| g.wallet_snapshots.keys().cloned())
        .collect();

    // Fetch current positions for each wallet (once)
    let mut wallet_positions: HashMap<String, Vec<WalletPosition>> = HashMap::new();
    for addr in &wallet_addrs {
        match client.get_wallet_positions(addr, 500).await {
            Ok(p) => { wallet_positions.insert(addr.clone(), p); }
            Err(e) => {
                warn!("T5 CONFIRM: failed to fetch positions for {}: {}", addr, e);
            }
        }
    }

    for game in &due_games {
        // Build condition_id set for this game from the schedule
        // We use the T-30 snapshot condition_ids as our reference
        let game_cids: HashSet<String> = game.wallet_snapshots.values()
            .flat_map(|positions| positions.iter())
            .filter_map(|p| p.condition_id.clone())
            .collect();

        for (wallet_addr, t30_positions) in &game.wallet_snapshots {
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

            // Filter current positions to those matching this game's condition_ids
            let current_game_positions: Vec<WalletPosition> = current_all.iter()
                .filter(|p| {
                    if p.size_f64() <= 0.0 { return false; }
                    let cur = p.cur_price_f64();
                    if cur <= 0.01 || cur >= 0.99 { return false; }
                    if let Some(cid) = &p.condition_id {
                        game_cids.contains(cid)
                    } else {
                        false
                    }
                })
                .cloned()
                .collect();

            if current_game_positions.is_empty() {
                info!(
                    "T5 SKIP: {} no longer has positions in {} (had {} at T-30)",
                    wallet_cfg.name, game.event_slug, t30_positions.len()
                );
                continue;
            }

            let mins_to_start = game.start_time.signed_duration_since(now).num_minutes();
            info!(
                "T5 CONFIRMED: {} still has {} positions in {} (T-30: {}, starts in {}min)",
                wallet_cfg.name,
                current_game_positions.len(),
                game.event_slug,
                t30_positions.len(),
                mins_to_start,
            );

            matches.push(T5Match {
                wallet_name: wallet_cfg.name.clone(),
                wallet_address: wallet_addr.clone(),
                game_event_slug: game.event_slug.clone(),
                positions: current_game_positions,
                t30_position_count: t30_positions.len(),
            });
        }
    }

    matches
}

// --- Legacy support: keep T30Match for backward compatibility ---

/// Match for T-30 check: a wallet position that matches an upcoming game
pub struct T30Match {
    pub wallet_name: String,
    pub wallet_address: String,
    pub game_event_slug: String,
    pub positions: Vec<WalletPosition>,
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
                    token_ids: vec![],
                    sport_tag: "nba".to_string(),
                },
                UpcomingGame {
                    event_slug: "game-later".to_string(),
                    title: "Later Game".to_string(),
                    start_time: now + chrono::Duration::minutes(60),
                    condition_ids: vec!["cid2".to_string()],
                    token_ids: vec![],
                    sport_tag: "nba".to_string(),
                },
                UpcomingGame {
                    event_slug: "game-past".to_string(),
                    title: "Past Game".to_string(),
                    start_time: now - chrono::Duration::minutes(10),
                    condition_ids: vec!["cid3".to_string()],
                    token_ids: vec![],
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
    fn games_starting_between_filters_correctly() {
        let now = Utc::now();
        let schedule = GameSchedule {
            games: vec![
                UpcomingGame {
                    event_slug: "game-5min".to_string(),
                    title: "5min Game".to_string(),
                    start_time: now + chrono::Duration::minutes(5),
                    condition_ids: vec!["cid1".to_string()],
                    token_ids: vec![],
                    sport_tag: "nba".to_string(),
                },
                UpcomingGame {
                    event_slug: "game-30min".to_string(),
                    title: "30min Game".to_string(),
                    start_time: now + chrono::Duration::minutes(30),
                    condition_ids: vec!["cid2".to_string()],
                    token_ids: vec![],
                    sport_tag: "nba".to_string(),
                },
                UpcomingGame {
                    event_slug: "game-60min".to_string(),
                    title: "60min Game".to_string(),
                    start_time: now + chrono::Duration::minutes(60),
                    condition_ids: vec!["cid3".to_string()],
                    token_ids: vec![],
                    sport_tag: "nba".to_string(),
                },
            ],
            last_refresh: now,
        };

        // Between 25 and 35 minutes
        let between = schedule.games_starting_between(25, 35);
        assert_eq!(between.len(), 1);
        assert_eq!(between[0].event_slug, "game-30min");

        // Between 0 and 7 minutes
        let between_short = schedule.games_starting_between(0, 7);
        assert_eq!(between_short.len(), 1);
        assert_eq!(between_short[0].event_slug, "game-5min");
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
            }]),
            tags: None,
        };

        let game = parse_event(&event, "nba").unwrap();
        assert_eq!(game.event_slug, "nba-lal-bos-2026-03-23");
        assert_eq!(game.condition_ids, vec!["0xabc"]);
        assert_eq!(game.token_ids.len(), 2);
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
