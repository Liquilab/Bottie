//! Blind fade strategy: WIN NO on favorite + DRAW YES on all football games.
//!
//! Uses the GameSchedule (same as Cannae T-5 scheduler) for real kickoff times.
//! Entirely config-driven. When `blind_fade.enabled` is false, zero code paths touched.

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use chrono::Utc;
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::config::{BlindFadeConfig, SharedConfig};
use crate::execution::Executor;
use crate::logger::TradeLogger;
use crate::risk::RiskManager;
use crate::scheduler::{self, GameSchedule, UpcomingGame};
use crate::signal;

/// Main blind fade loop. Uses GameSchedule for kickoff times (same as T-5 scheduler).
pub async fn blind_fade_loop(
    client: Arc<ClobClient>,
    config: SharedConfig,
    logger: Arc<TradeLogger>,
    risk: Arc<RwLock<RiskManager>>,
) {
    let mut executor = Executor::new(client.clone(), config.clone());
    let mut schedule = GameSchedule::load_from_disk(std::path::Path::new("data/schedule_cache.json"));

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

        // Refresh schedule if stale (every 60 min, same as Cannae)
        if schedule.needs_refresh(60) {
            scheduler::refresh_schedule(&client, &fade_config.sport_tags, &mut schedule).await;
        }

        match run_blind_fade_cycle(&client, &mut executor, &logger, &risk, &fade_config, &schedule).await {
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
    logger: &TradeLogger,
    risk: &Arc<RwLock<RiskManager>>,
    fade_config: &BlindFadeConfig,
    schedule: &GameSchedule,
) -> Result<u32> {
    let now = Utc::now();

    // Filter: games starting within 75 minutes (real kickoff from GameSchedule)
    let upcoming: Vec<&UpcomingGame> = schedule.games.iter()
        .filter(|g| {
            let mins = g.start_time.signed_duration_since(now).num_minutes();
            mins > -10 && mins <= 75  // started < 10min ago OR starts within 75min
        })
        .collect();

    if upcoming.is_empty() {
        info!("BLIND_FADE: 0 upcoming games (within 75min) out of {} total", schedule.games.len());
        return Ok(0);
    }

    info!("BLIND_FADE: {} upcoming games (within 75min) out of {} total", upcoming.len(), schedule.games.len());

    let mut placed = 0u32;

    for game in &upcoming {
        // Find win markets and draw market from market_tokens
        // market_tokens: Vec<(condition_id, question, Vec<(outcome, token_id)>)>
        let mut win_markets: Vec<(&str, &str, &[(String, String)])> = Vec::new(); // (cid, question, tokens)
        let mut draw_market: Option<(&str, &[(String, String)])> = None;

        for (cid, question, tokens) in &game.market_tokens {
            if question.contains("win") && !question.contains("draw") {
                win_markets.push((cid, question, tokens));
            } else if question.contains("draw") {
                draw_market = Some((cid, tokens));
            }
        }

        // Need at least one win market AND a draw market
        if win_markets.is_empty() || draw_market.is_none() {
            continue;
        }
        let (draw_cid, draw_tokens) = draw_market.unwrap();

        // Find draw YES token
        let draw_yes_token = draw_tokens.iter()
            .find(|(o, _)| o == "Yes")
            .map(|(_, tid)| tid.as_str());
        let draw_yes_token = match draw_yes_token {
            Some(t) => t,
            None => continue,
        };

        // Get draw YES price from orderbook
        let draw_yes_price = match client.get_best_ask(draw_yes_token).await {
            Ok(p) if p > 0.0 && p < 1.0 => p,
            _ => continue,
        };

        // For each win market, get YES price to find the favorite
        let mut best_fav: Option<(&str, &str, f64)> = None; // (cid, no_token, yes_price)

        for (cid, _question, tokens) in &win_markets {
            let yes_token = tokens.iter().find(|(o, _)| o == "Yes").map(|(_, t)| t.as_str());
            let no_token = tokens.iter().find(|(o, _)| o == "No").map(|(_, t)| t.as_str());

            if let (Some(yes_tok), Some(no_tok)) = (yes_token, no_token) {
                // Get YES price (= how likely this team wins)
                let yes_price = match client.get_best_bid(yes_tok).await {
                    Ok(p) if p > 0.0 => p,
                    _ => continue,
                };

                if best_fav.is_none() || yes_price > best_fav.unwrap().2 {
                    best_fav = Some((cid, no_tok, yes_price));
                }
            }
        }

        let (fav_cid, fav_no_token, fav_yes_price) = match best_fav {
            Some(f) => f,
            None => continue,
        };

        // WIN NO price = ask on the NO token of the favorite
        let win_no_price = match client.get_best_ask(fav_no_token).await {
            Ok(p) if p > 0.0 && p < 1.0 => p,
            _ => continue,
        };

        // Price filter: WIN NO must be within configured range
        if win_no_price < fade_config.win_no_min_price || win_no_price > fade_config.win_no_max_price {
            continue;
        }

        // Pre-flight: need enough cash for BOTH legs
        let required = fade_config.flat_size_usdc * 2.0;
        let bankroll = risk.read().await.bankroll();
        if bankroll < required {
            info!("BLIND_FADE: SKIP {} — bankroll ${:.2} < ${:.2} required",
                game.title, bankroll, required);
            continue;
        }

        let mins_to_kick = game.start_time.signed_duration_since(now).num_minutes();
        info!(
            "BLIND_FADE: {} | T-{}min | fav YES {:.0}ct | WIN NO {:.0}ct | DRAW YES {:.0}ct",
            game.title, mins_to_kick, fav_yes_price * 100.0,
            win_no_price * 100.0, draw_yes_price * 100.0,
        );

        // Leg 1: WIN NO on favorite
        let win_no_signal = build_signal(
            fav_no_token, fav_cid, win_no_price, "No",
            &game.title, "soccer", &game.event_slug,
        );

        let size_pct = fade_config.flat_size_usdc / bankroll * 100.0;
        let leg1_ok = {
            let mut risk_guard = risk.write().await;
            match executor.execute_flat(&win_no_signal, &mut risk_guard, logger, true, size_pct).await {
                Ok(true) => {
                    placed += 1;
                    info!("BLIND_FADE: FILLED WIN NO {} @ {:.0}ct", game.title, win_no_price * 100.0);
                    true
                }
                Ok(false) => false,
                Err(e) => { warn!("BLIND_FADE: WIN NO error: {e}"); false }
            }
        };

        // Leg 2: DRAW YES — ONLY if leg 1 succeeded (keep legs paired)
        if !leg1_ok {
            continue;
        }

        {
            let draw_signal = build_signal(
                draw_yes_token, draw_cid, draw_yes_price, "Yes",
                &game.title, "soccer", &game.event_slug,
            );

            let draw_size_pct = fade_config.flat_size_usdc / risk.read().await.bankroll() * 100.0;
            if draw_size_pct > 0.0 {
                let mut risk_guard = risk.write().await;
                match executor.execute_flat(&draw_signal, &mut risk_guard, logger, true, draw_size_pct).await {
                    Ok(true) => {
                        placed += 1;
                        info!("BLIND_FADE: FILLED DRAW YES {} @ {:.0}ct", game.title, draw_yes_price * 100.0);
                    }
                    Ok(false) => {}
                    Err(e) => warn!("BLIND_FADE: DRAW YES error: {e}"),
                }
            }
        }

        // Brief pause between games to avoid rate limits
        tokio::time::sleep(Duration::from_secs(2)).await;
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
    event_slug: &str,
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
        event_slug: event_slug.to_string(),
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
