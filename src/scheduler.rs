use std::collections::HashSet;

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

    /// Check if the schedule needs refreshing
    pub fn needs_refresh(&self, max_age_minutes: i64) -> bool {
        let age = Utc::now().signed_duration_since(self.last_refresh);
        age.num_minutes() >= max_age_minutes
    }
}

/// Fetch upcoming sports events and build a schedule
pub async fn refresh_schedule(
    client: &ClobClient,
    sport_tags: &[String],
    schedule: &mut GameSchedule,
) {
    let mut games = Vec::new();

    for tag in sport_tags {
        match client.search_sports_events(tag).await {
            Ok(events) => {
                for event in events {
                    if let Some(game) = parse_event(&event, tag) {
                        games.push(game);
                    }
                }
            }
            Err(e) => {
                warn!("T30: failed to fetch events for tag '{}': {}", tag, e);
            }
        }
    }

    let count = games.len();
    schedule.games = games;
    schedule.last_refresh = Utc::now();
    info!("T30: schedule refreshed — {} upcoming games from {} tags", count, sport_tags.len());
}

fn parse_event(event: &GammaSportsEvent, sport_tag: &str) -> Option<UpcomingGame> {
    let slug = event.slug.as_deref()?;
    let title = event.title.as_deref().unwrap_or("").to_string();
    let start_str = event.start_date.as_deref()?;

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

/// Match for T-30 check: a wallet position that matches an upcoming game
pub struct T30Match {
    pub wallet_name: String,
    pub wallet_address: String,
    pub game_event_slug: String,
    pub positions: Vec<WalletPosition>,
}

/// For games starting within t_minus minutes, check each wallet's positions
/// for matching condition_ids. Returns raw matches for execute_stable_game().
pub async fn check_t30_games(
    client: &ClobClient,
    schedule: &GameSchedule,
    watchlist: &[WatchlistEntry],
    t_minus_minutes: i64,
    attempted: &mut HashSet<String>,
) -> Vec<T30Match> {
    let upcoming = schedule.games_starting_within(t_minus_minutes);
    if upcoming.is_empty() {
        return Vec::new();
    }

    // Build set of all condition_ids from upcoming games, keyed by event_slug
    let mut cid_to_event: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    for game in &upcoming {
        for cid in &game.condition_ids {
            cid_to_event.insert(cid.clone(), game.event_slug.clone());
        }
    }

    let mut matches = Vec::new();

    for wallet in watchlist {
        // Dedup key: event_slug::wallet_address
        // Check all upcoming games against this wallet
        let mut wallet_has_upcoming = false;
        for game in &upcoming {
            let dedup_key = format!("{}::{}", game.event_slug, wallet.address.to_lowercase());
            if !attempted.contains(&dedup_key) {
                wallet_has_upcoming = true;
                break;
            }
        }
        if !wallet_has_upcoming {
            continue;
        }

        // Fetch wallet positions
        let positions = match client.get_wallet_positions(&wallet.address, 500).await {
            Ok(p) => p,
            Err(e) => {
                warn!("T30: failed to fetch positions for {}: {}", wallet.name, e);
                continue;
            }
        };

        // Group positions by event_slug matching upcoming games
        let mut by_event: std::collections::HashMap<String, Vec<WalletPosition>> =
            std::collections::HashMap::new();

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
                    by_event
                        .entry(event_slug.clone())
                        .or_default()
                        .push(pos.clone());
                }
            }
        }

        for (event_slug, event_positions) in by_event {
            let dedup_key = format!("{}::{}", event_slug, wallet.address.to_lowercase());
            if attempted.contains(&dedup_key) {
                continue;
            }
            attempted.insert(dedup_key);

            info!(
                "T30: {} has {} positions in upcoming game {} (wallet: {})",
                wallet.name,
                event_positions.len(),
                event_slug,
                wallet.address
            );

            matches.push(T30Match {
                wallet_name: wallet.name.clone(),
                wallet_address: wallet.address.clone(),
                game_event_slug: event_slug,
                positions: event_positions,
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
}
