use anyhow::{Context, Result};
use reqwest::Client;
use serde::Deserialize;
use tracing::{debug, info};

use crate::signal::ArbSignal;

pub struct OddsClient {
    http: Client,
    api_key: String,
    base_url: String,
}

#[derive(Debug, Deserialize)]
pub struct OddsEvent {
    pub id: Option<String>,
    pub sport_key: Option<String>,
    pub sport_title: Option<String>,
    pub commence_time: Option<String>,
    pub home_team: Option<String>,
    pub away_team: Option<String>,
    pub bookmakers: Option<Vec<Bookmaker>>,
}

#[derive(Debug, Deserialize)]
pub struct Bookmaker {
    pub key: Option<String>,
    pub title: Option<String>,
    pub markets: Option<Vec<BookmakerMarket>>,
}

#[derive(Debug, Deserialize)]
pub struct BookmakerMarket {
    pub key: Option<String>,
    pub outcomes: Option<Vec<BookmakerOutcome>>,
}

#[derive(Debug, Deserialize)]
pub struct BookmakerOutcome {
    pub name: Option<String>,
    pub price: Option<f64>,
}

impl OddsClient {
    pub fn new(api_key: &str, base_url: &str) -> Self {
        Self {
            http: Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .expect("failed to build HTTP client"),
            api_key: api_key.to_string(),
            base_url: base_url.to_string(),
        }
    }

    /// Fetch odds for a specific sport
    pub async fn get_odds(&self, sport: &str) -> Result<Vec<OddsEvent>> {
        let url = format!(
            "{}/sports/{}/odds/?apiKey={}&regions=us,eu&markets=h2h&oddsFormat=decimal",
            self.base_url, sport, self.api_key
        );

        debug!("fetching odds: {sport}");

        let events: Vec<OddsEvent> = self
            .http
            .get(&url)
            .send()
            .await?
            .error_for_status()
            .with_context(|| format!("failed to fetch odds for {sport}"))?
            .json()
            .await?;

        Ok(events)
    }

    /// Calculate sharp probability from bookmaker odds.
    /// Prioritizes Pinnacle (sharpest bookmaker), falls back to average of all.
    pub fn consensus_probability(event: &OddsEvent, team: &str) -> Option<f64> {
        let bookmakers = event.bookmakers.as_ref()?;
        let mut pinnacle_prob: Option<f64> = None;
        let mut all_probs = Vec::new();

        for bm in bookmakers {
            let bm_key = bm.key.as_deref().unwrap_or("");
            let markets = bm.markets.as_ref()?;
            for market in markets {
                if market.key.as_deref() != Some("h2h") {
                    continue;
                }
                let outcomes = market.outcomes.as_ref()?;
                for outcome in outcomes {
                    if outcome.name.as_deref() == Some(team) {
                        if let Some(decimal_odds) = outcome.price {
                            if decimal_odds > 1.0 {
                                let prob = 1.0 / decimal_odds;
                                all_probs.push(prob);
                                if bm_key == "pinnacle" {
                                    pinnacle_prob = Some(prob);
                                }
                            }
                        }
                    }
                }
            }
        }

        // Prefer Pinnacle (sharpest line), fall back to average
        if let Some(p) = pinnacle_prob {
            return Some(p);
        }

        if all_probs.is_empty() {
            return None;
        }

        let avg = all_probs.iter().sum::<f64>() / all_probs.len() as f64;
        Some(avg)
    }
}

/// Compare bookmaker odds with Polymarket prices to find arb opportunities
pub fn find_arb_opportunities(
    odds_events: &[OddsEvent],
    pm_markets: &[PolymarketSportsMatch],
    min_edge_pct: f64,
) -> Vec<ArbSignal> {
    let mut signals = Vec::new();

    for pm in pm_markets {
        // Try to find matching odds event
        let matching_event = odds_events.iter().find(|e| {
            let home = e.home_team.as_deref().unwrap_or("");
            let away = e.away_team.as_deref().unwrap_or("");
            fuzzy_match_teams(&pm.team_a, &pm.team_b, home, away)
        });

        let event = match matching_event {
            Some(e) => e,
            None => continue,
        };

        // Check team A (Yes outcome)
        if let Some(bm_prob) = OddsClient::consensus_probability(event, &pm.team_a) {
            let pm_price = pm.yes_price;
            let edge = (bm_prob - pm_price) / pm_price * 100.0;

            if edge > min_edge_pct {
                info!(
                    "ARB: {} - {} YES @ {:.2} vs bookmaker {:.2} = {:.1}% edge",
                    pm.title, pm.team_a, pm_price, bm_prob, edge
                );
                signals.push(ArbSignal {
                    token_id: pm.yes_token_id.clone(),
                    condition_id: pm.condition_id.clone(),
                    side: "BUY".to_string(),
                    pm_price,
                    implied_prob: pm_price,
                    bookmaker_prob: bm_prob,
                    edge_pct: edge,
                    market_title: pm.title.clone(),
                    sport: pm.sport.clone(),
                    bookmaker: "consensus".to_string(),
                });
            }

            // Check the opposite side too
            let no_bm_prob = 1.0 - bm_prob;
            let no_pm_price = pm.no_price;
            let no_edge = (no_bm_prob - no_pm_price) / no_pm_price * 100.0;

            if no_edge > min_edge_pct {
                info!(
                    "ARB: {} - {} NO @ {:.2} vs bookmaker {:.2} = {:.1}% edge",
                    pm.title, pm.team_b, no_pm_price, no_bm_prob, no_edge
                );
                signals.push(ArbSignal {
                    token_id: pm.no_token_id.clone(),
                    condition_id: pm.condition_id.clone(),
                    side: "BUY".to_string(),
                    pm_price: no_pm_price,
                    implied_prob: no_pm_price,
                    bookmaker_prob: no_bm_prob,
                    edge_pct: no_edge,
                    market_title: pm.title.clone(),
                    sport: pm.sport.clone(),
                    bookmaker: "consensus".to_string(),
                });
            }
        }
    }

    signals
}

/// Polymarket sports market with matched teams
#[derive(Debug, Clone)]
pub struct PolymarketSportsMatch {
    pub title: String,
    pub condition_id: String,
    pub yes_token_id: String,
    pub no_token_id: String,
    pub yes_price: f64,
    pub no_price: f64,
    pub team_a: String,
    pub team_b: String,
    pub sport: String,
    pub end_date: Option<String>,
}

/// Grouped event: all main markets (win A, win B, draw) for one event
#[derive(Debug, Clone)]
pub struct GroupedEvent {
    pub event_title: String,
    pub team_a: String,
    pub team_b: String,
    /// "Will Team A win?" market
    pub win_a: Option<PolymarketSportsMatch>,
    /// "Will Team B win?" market
    pub win_b: Option<PolymarketSportsMatch>,
    /// "Will X vs Y end in a draw?" market
    pub draw: Option<PolymarketSportsMatch>,
    /// Bookmaker consensus probabilities
    pub bm_prob_a: f64,
    pub bm_prob_b: f64,
    pub bm_prob_draw: f64,
    /// Competitiveness = abs(fav_prob - dog_prob) as percentage
    pub competitiveness_pct: f64,
}

/// Find close game signals: events where competitiveness < threshold
/// Returns signals for Win NO legs on both teams + Draw YES
pub fn find_close_game_signals(
    odds_events: &[OddsEvent],
    pm_markets: &[PolymarketSportsMatch],
    max_competitiveness_pct: f64,
) -> Vec<ArbSignal> {
    let mut signals = Vec::new();
    let grouped = group_event_markets(odds_events, pm_markets);

    for event in &grouped {
        if event.competitiveness_pct > max_competitiveness_pct {
            debug!(
                "SKIP close_games: {} (comp={:.1}% > {:.1}%)",
                event.event_title, event.competitiveness_pct, max_competitiveness_pct
            );
            continue;
        }

        info!(
            "CLOSE GAME: {} | comp={:.1}% | bm: A={:.0}% B={:.0}% D={:.0}%",
            event.event_title,
            event.competitiveness_pct,
            event.bm_prob_a * 100.0,
            event.bm_prob_b * 100.0,
            event.bm_prob_draw * 100.0,
        );

        // Generate Win NO signals for both teams (NO wins when team doesn't win)
        if let Some(ref win_a) = event.win_a {
            signals.push(ArbSignal {
                token_id: win_a.no_token_id.clone(),
                condition_id: win_a.condition_id.clone(),
                side: "BUY".to_string(),
                pm_price: win_a.no_price,
                implied_prob: win_a.no_price,
                bookmaker_prob: 1.0 - event.bm_prob_a,
                edge_pct: event.competitiveness_pct,
                market_title: format!("{} NO", win_a.title),
                sport: win_a.sport.clone(),
                bookmaker: "close_games".to_string(),
            });
        }

        if let Some(ref win_b) = event.win_b {
            signals.push(ArbSignal {
                token_id: win_b.no_token_id.clone(),
                condition_id: win_b.condition_id.clone(),
                side: "BUY".to_string(),
                pm_price: win_b.no_price,
                implied_prob: win_b.no_price,
                bookmaker_prob: 1.0 - event.bm_prob_b,
                edge_pct: event.competitiveness_pct,
                market_title: format!("{} NO", win_b.title),
                sport: win_b.sport.clone(),
                bookmaker: "close_games".to_string(),
            });
        }

        // Generate Draw YES signal
        if let Some(ref draw) = event.draw {
            signals.push(ArbSignal {
                token_id: draw.yes_token_id.clone(),
                condition_id: draw.condition_id.clone(),
                side: "BUY".to_string(),
                pm_price: draw.yes_price,
                implied_prob: draw.yes_price,
                bookmaker_prob: event.bm_prob_draw,
                edge_pct: event.competitiveness_pct,
                market_title: format!("{} YES", draw.title),
                sport: draw.sport.clone(),
                bookmaker: "close_games".to_string(),
            });
        }
    }

    signals
}

/// Group PM markets by event and match with bookmaker odds
fn group_event_markets(
    odds_events: &[OddsEvent],
    pm_markets: &[PolymarketSportsMatch],
) -> Vec<GroupedEvent> {
    // Group PM markets by event: markets with same teams belong to same event
    let mut event_groups: Vec<GroupedEvent> = Vec::new();

    for pm in pm_markets {
        let title_lower = pm.title.to_lowercase();

        // Skip non-main-market (O/U, BTTS, spread)
        let is_win = title_lower.contains("will") && title_lower.contains("win");
        let is_draw = title_lower.contains("end in a draw");
        if !is_win && !is_draw {
            continue;
        }

        // Find or create event group
        let group = event_groups.iter_mut().find(|g| {
            fuzzy_match_teams(&g.team_a, &g.team_b, &pm.team_a, &pm.team_b)
                || fuzzy_match_teams(&g.team_a, &g.team_b, &pm.team_b, &pm.team_a)
        });

        let group = match group {
            Some(g) => g,
            None => {
                event_groups.push(GroupedEvent {
                    event_title: format!("{} vs {}", pm.team_a, pm.team_b),
                    team_a: pm.team_a.clone(),
                    team_b: pm.team_b.clone(),
                    win_a: None,
                    win_b: None,
                    draw: None,
                    bm_prob_a: 0.0,
                    bm_prob_b: 0.0,
                    bm_prob_draw: 0.0,
                    competitiveness_pct: 100.0,
                });
                event_groups.last_mut().unwrap()
            }
        };

        if is_draw {
            group.draw = Some(pm.clone());
        } else if is_win {
            // Determine which team this "Will X win?" belongs to
            // Extract team name from title: "Will {Team} win on {date}?"
            let team_in_title = extract_team_from_win_title(&pm.title);
            let norm = |s: &str| s.to_lowercase().replace(['-', '_', '.'], " ");
            let team_norm = norm(&team_in_title);
            let a_norm = norm(&group.team_a);
            let b_norm = norm(&group.team_b);

            if team_norm.contains(&a_norm) || a_norm.contains(&team_norm) {
                group.win_a = Some(pm.clone());
            } else if team_norm.contains(&b_norm) || b_norm.contains(&team_norm) {
                group.win_b = Some(pm.clone());
            } else {
                // Can't determine — assign to first empty slot
                if group.win_a.is_none() {
                    group.win_a = Some(pm.clone());
                } else if group.win_b.is_none() {
                    group.win_b = Some(pm.clone());
                }
            }
        }
    }

    // Match with bookmaker odds and calculate competitiveness
    for group in &mut event_groups {
        let matching_event = odds_events.iter().find(|e| {
            let home = e.home_team.as_deref().unwrap_or("");
            let away = e.away_team.as_deref().unwrap_or("");
            fuzzy_match_teams(&group.team_a, &group.team_b, home, away)
        });

        if let Some(event) = matching_event {
            let home = event.home_team.as_deref().unwrap_or("");
            let away = event.away_team.as_deref().unwrap_or("");

            // Get consensus probs with vig removal
            let prob_home = OddsClient::consensus_probability(event, home).unwrap_or(0.33);
            let prob_away = OddsClient::consensus_probability(event, away).unwrap_or(0.33);
            let prob_draw = OddsClient::consensus_probability(event, "Draw").unwrap_or(0.28);

            // Remove vig (normalize to 100%)
            let total = prob_home + prob_away + prob_draw;
            if total > 0.0 {
                group.bm_prob_a = prob_home / total;
                group.bm_prob_b = prob_away / total;
                group.bm_prob_draw = prob_draw / total;
            }

            let fav = group.bm_prob_a.max(group.bm_prob_b);
            let dog = group.bm_prob_a.min(group.bm_prob_b);
            group.competitiveness_pct = (fav - dog) * 100.0;
        }
        // If no bookmaker match found, competitiveness stays at 100% = filtered out
    }

    event_groups
}

/// Extract team name from "Will {Team} win on {date}?" title
fn extract_team_from_win_title(title: &str) -> String {
    let t = title.trim();
    // Remove "Will " prefix
    let rest = if let Some(r) = t.strip_prefix("Will ") { r } else { t };
    // Find " win" and take everything before it
    if let Some(idx) = rest.to_lowercase().find(" win") {
        rest[..idx].trim().to_string()
    } else {
        rest.to_string()
    }
}

fn fuzzy_match_teams(pm_a: &str, pm_b: &str, odds_home: &str, odds_away: &str) -> bool {
    let normalize = |s: &str| s.to_lowercase().replace(['-', '_', '.'], " ");
    let pm_a = normalize(pm_a);
    let pm_b = normalize(pm_b);
    let home = normalize(odds_home);
    let away = normalize(odds_away);

    (pm_a.contains(&home) || home.contains(&pm_a))
        && (pm_b.contains(&away) || away.contains(&pm_b))
        || (pm_a.contains(&away) || away.contains(&pm_a))
            && (pm_b.contains(&home) || home.contains(&pm_b))
}
