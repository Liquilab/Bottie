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

    /// Calculate consensus probability from bookmaker odds
    pub fn consensus_probability(event: &OddsEvent, team: &str) -> Option<f64> {
        let bookmakers = event.bookmakers.as_ref()?;
        let mut probs = Vec::new();

        for bm in bookmakers {
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
                                probs.push(1.0 / decimal_odds);
                            }
                        }
                    }
                }
            }
        }

        if probs.is_empty() {
            return None;
        }

        // Average implied probability (removing vig would be more accurate but this is a start)
        let avg = probs.iter().sum::<f64>() / probs.len() as f64;
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
