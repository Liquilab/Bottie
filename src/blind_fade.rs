//! Blind fade strategy: WIN NO on favorite + DRAW YES on all football games.
//!
//! Entirely config-driven. When `blind_fade.enabled` is false, this module
//! does nothing. Runs as an independent loop alongside copy_trading and odds_arb.

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::config::{BlindFadeConfig, SharedConfig};
use crate::execution::Executor;
use crate::logger::TradeLogger;
use crate::odds::PolymarketSportsMatch;
use crate::risk::RiskManager;
use crate::signal;
use crate::sports;

/// A football event with win markets for both teams and optionally a draw market.
struct FootballEvent {
    event_title: String,
    team_a: String,
    team_b: String,
    win_a: Option<PolymarketSportsMatch>,
    win_b: Option<PolymarketSportsMatch>,
    draw: Option<PolymarketSportsMatch>,
}

/// Main blind fade loop. Scans football markets and places WIN NO + DRAW YES.
pub async fn blind_fade_loop(
    client: Arc<ClobClient>,
    config: SharedConfig,
    logger: Arc<TradeLogger>,
    risk: Arc<RwLock<RiskManager>>,
) {
    let mut executor = Executor::new(client.clone(), config.clone());

    // Seed attempted map from live positions (prevent double-buying)
    {
        let funder = client.funder_address();
        match client.get_wallet_positions(&funder, 500).await {
            Ok(positions) => executor.seed_from_positions(&positions),
            Err(e) => warn!("blind_fade: could not seed from PM positions: {e}"),
        }
    }

    loop {
        let (enabled, poll_interval, fade_config) = {
            let c = config.read().await;
            (
                c.blind_fade.enabled,
                c.blind_fade.poll_interval_seconds,
                c.blind_fade.clone(),
            )
        };

        if !enabled {
            tokio::time::sleep(Duration::from_secs(60)).await;
            continue;
        }

        match run_blind_fade_cycle(&client, &mut executor, &config, &logger, &risk, &fade_config).await {
            Ok(placed) => {
                if placed > 0 {
                    info!("BLIND_FADE: placed {} bets this cycle", placed);
                }
            }
            Err(e) => warn!("BLIND_FADE: cycle error: {e}"),
        }

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
    }
}

async fn run_blind_fade_cycle(
    client: &Arc<ClobClient>,
    executor: &mut Executor,
    config: &SharedConfig,
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
    fade_config: &BlindFadeConfig,
) -> Result<u32> {
    // Fetch all football markets
    let pm_markets = sports::fetch_sports_markets(client, &fade_config.sport_tags).await?;

    if pm_markets.is_empty() {
        return Ok(0);
    }

    // Group by event
    let events = group_football_events(&pm_markets);
    info!("BLIND_FADE: {} football events found ({} markets)", events.len(), pm_markets.len());

    let mut placed = 0u32;

    for event in &events {
        // Find the favorite: team with highest YES price
        let (fav_market, _underdog_market) = match (&event.win_a, &event.win_b) {
            (Some(a), Some(b)) => {
                if a.yes_price >= b.yes_price {
                    (a, b)
                } else {
                    (b, a)
                }
            }
            (Some(a), None) => (a, a), // only one team market
            (None, Some(b)) => (b, b),
            (None, None) => continue,
        };

        // WIN NO price = price of buying NO on the favorite
        let win_no_price = fav_market.no_price;

        // Price filter: WIN NO must be 30-50ct (configurable)
        if win_no_price < fade_config.win_no_min_price || win_no_price > fade_config.win_no_max_price {
            continue;
        }

        info!(
            "BLIND_FADE: {} | fav={} (YES {:.0}ct) | WIN NO {:.0}ct | draw {}",
            event.event_title,
            extract_team_from_title(&fav_market.title),
            fav_market.yes_price * 100.0,
            win_no_price * 100.0,
            event.draw.as_ref().map(|d| format!("YES {:.0}ct", d.yes_price * 100.0)).unwrap_or_else(|| "N/A".to_string()),
        );

        // Leg 1: WIN NO on favorite
        let win_no_signal = build_signal(
            &fav_market.no_token_id,
            &fav_market.condition_id,
            win_no_price,
            "No",
            &fav_market.title,
            &fav_market.sport,
        );

        let size_pct = fade_config.flat_size_usdc / risk.read().await.bankroll() * 100.0;
        if size_pct > 0.0 {
            let mut risk_guard = risk.write().await;
            match executor.execute_flat(&win_no_signal, &mut risk_guard, logger, true, size_pct).await {
                Ok(true) => {
                    placed += 1;
                    info!("BLIND_FADE: FILLED WIN NO {} @ {:.0}ct", fav_market.title, win_no_price * 100.0);
                }
                Ok(false) => {} // skipped (dedup, risk, etc)
                Err(e) => warn!("BLIND_FADE: WIN NO error: {e}"),
            }
        }

        // Leg 2: DRAW YES (no price filter on draw)
        if let Some(ref draw) = event.draw {
            let draw_signal = build_signal(
                &draw.yes_token_id,
                &draw.condition_id,
                draw.yes_price,
                "Yes",
                &draw.title,
                &draw.sport,
            );

            let draw_size_pct = fade_config.flat_size_usdc / risk.read().await.bankroll() * 100.0;
            if draw_size_pct > 0.0 {
                let mut risk_guard = risk.write().await;
                match executor.execute_flat(&draw_signal, &mut risk_guard, logger, true, draw_size_pct).await {
                    Ok(true) => {
                        placed += 1;
                        info!("BLIND_FADE: FILLED DRAW YES {} @ {:.0}ct", draw.title, draw.yes_price * 100.0);
                    }
                    Ok(false) => {}
                    Err(e) => warn!("BLIND_FADE: DRAW YES error: {e}"),
                }
            }
        }

        // Brief pause between games to avoid rate limits
        if placed > 0 {
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    }

    Ok(placed)
}

fn build_signal(
    token_id: &str,
    condition_id: &str,
    price: f64,
    outcome: &str,
    title: &str,
    sport: &str,
) -> signal::AggregatedSignal {
    signal::AggregatedSignal {
        token_id: token_id.to_string(),
        condition_id: condition_id.to_string(),
        side: "BUY".to_string(),
        price,
        market_title: title.to_string(),
        market_type: if outcome == "No" { "win".to_string() } else { "draw".to_string() },
        sport: sport.to_string(),
        outcome: outcome.to_string(),
        event_slug: String::new(),
        sources: vec![signal::SignalSource::OddsArb(signal::ArbSignal {
            token_id: token_id.to_string(),
            condition_id: condition_id.to_string(),
            side: "BUY".to_string(),
            pm_price: price,
            implied_prob: price,
            bookmaker_prob: 0.0,
            edge_pct: 0.0,
            market_title: title.to_string(),
            sport: sport.to_string(),
            bookmaker: "blind_fade".to_string(),
        })],
        combined_confidence: 0.0,
        edge_pct: 0.0,
        source_size_usdc: 0.0,
        source_shares: 0.0,
    }
}

/// Group PM markets into football events (win A, win B, draw).
/// Similar to odds.rs group_event_markets but without bookmaker data.
fn group_football_events(pm_markets: &[PolymarketSportsMatch]) -> Vec<FootballEvent> {
    let mut events: Vec<FootballEvent> = Vec::new();

    for pm in pm_markets {
        let title_lower = pm.title.to_lowercase();

        let is_win = title_lower.contains("will") && title_lower.contains("win");
        let is_draw = title_lower.contains("end in a draw");
        if !is_win && !is_draw {
            continue;
        }

        // Find or create event group by matching teams
        let group = events.iter_mut().find(|g| {
            fuzzy_team_match(&g.team_a, &g.team_b, &pm.team_a, &pm.team_b)
        });

        let group = match group {
            Some(g) => g,
            None => {
                events.push(FootballEvent {
                    event_title: format!("{} vs {}", pm.team_a, pm.team_b),
                    team_a: pm.team_a.clone(),
                    team_b: pm.team_b.clone(),
                    win_a: None,
                    win_b: None,
                    draw: None,
                });
                events.last_mut().unwrap()
            }
        };

        if is_draw {
            group.draw = Some(pm.clone());
        } else if is_win {
            let team_in_title = extract_team_from_title(&pm.title);
            let norm = |s: &str| s.to_lowercase().replace(['-', '_', '.'], " ");
            let team_norm = norm(&team_in_title);
            let a_norm = norm(&group.team_a);
            let b_norm = norm(&group.team_b);

            if team_norm.contains(&a_norm) || a_norm.contains(&team_norm) {
                group.win_a = Some(pm.clone());
            } else if team_norm.contains(&b_norm) || b_norm.contains(&team_norm) {
                group.win_b = Some(pm.clone());
            } else {
                if group.win_a.is_none() {
                    group.win_a = Some(pm.clone());
                } else if group.win_b.is_none() {
                    group.win_b = Some(pm.clone());
                }
            }
        }
    }

    // Only keep events with at least one win market
    events.retain(|e| e.win_a.is_some() || e.win_b.is_some());
    events
}

fn extract_team_from_title(title: &str) -> String {
    let t = title.trim();
    let rest = if let Some(r) = t.strip_prefix("Will ") { r } else { t };
    if let Some(idx) = rest.to_lowercase().find(" win") {
        rest[..idx].trim().to_string()
    } else {
        rest.to_string()
    }
}

fn fuzzy_team_match(a1: &str, b1: &str, a2: &str, b2: &str) -> bool {
    let norm = |s: &str| s.to_lowercase().replace(['-', '_', '.'], " ");
    let (a1, b1, a2, b2) = (norm(a1), norm(b1), norm(a2), norm(b2));

    (a1.contains(&a2) || a2.contains(&a1))
        && (b1.contains(&b2) || b2.contains(&b1))
        || (a1.contains(&b2) || b2.contains(&a1))
            && (b1.contains(&a2) || a2.contains(&b1))
}
