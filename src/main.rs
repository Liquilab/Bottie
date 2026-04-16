mod budget;
mod clob;
mod config;
mod consensus;
mod copy_trader;
mod execution;
mod logger;
mod odds;
mod portfolio;
mod resolver;
mod risk;
mod rules;
mod scheduler;
mod signal;
mod signing;
mod sizing;
mod sports;
mod spread;
mod stability;
mod subgraph;
mod sync;
mod wallet_tracker;


use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use alloy_signer_local::PrivateKeySigner;
use anyhow::{Context, Result};
use clap::Parser;
use tokio::sync::RwLock;
use std::collections::HashSet;
use tracing::{error, info, warn};
use crate::logger::TradeLog;

use crate::clob::client::ClobClient;
use crate::config::{AppConfig, BotConfig, SharedConfig};
use crate::copy_trader::CopyTrader;
use crate::execution::Executor;
use crate::logger::TradeLogger;
use crate::odds::OddsClient;
use crate::portfolio::Portfolio;
use crate::risk::RiskManager;
use crate::signal::SignalAggregator;
use crate::wallet_tracker::WalletTracker;

#[derive(Parser)]
#[command(name = "bottie", about = "Polymarket autonomous trading bot")]
struct Cli {
    /// Run in dry-run mode (log trades without placing them)
    #[arg(long, default_value_t = false)]
    dry_run: bool,

    /// Path to config file
    #[arg(long, default_value = "config.yaml")]
    config: String,

    /// Initial bankroll in USDC
    #[arg(long, default_value_t = 200.0)]
    bankroll: f64,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize logging
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "bottie=info".into()),
        )
        .with_target(false)
        .init();

    let cli = Cli::parse();

    // Load env config
    let mut bot_config = BotConfig::from_env()?;
    bot_config.dry_run = cli.dry_run;

    // Load YAML config
    let config_path = PathBuf::from(&cli.config);
    let app_config = AppConfig::load(&config_path)?;
    let shared_config: SharedConfig = Arc::new(RwLock::new(app_config));

    // Load SSOT Pilaar 1 — declarative trading rules. Panics if missing/invalid.
    rules::load("data/ssot/rules.yaml");

    // Create signer
    let signer: PrivateKeySigner = bot_config
        .private_key
        .parse()
        .context("invalid PRIVATE_KEY")?;

    info!("signer: {}", signer.address());
    info!("funder: {}", bot_config.funder);
    info!("dry_run: {}", bot_config.dry_run);

    // Derive API key if not set
    let clob_client = if bot_config.api_key.is_empty() {
        info!("deriving API key...");
        let temp_client = ClobClient::new(signer.clone(), bot_config.clone());
        let creds = temp_client.derive_api_key().await?;
        bot_config.api_key = creds.api_key;
        bot_config.api_secret = creds.secret;
        bot_config.api_passphrase = creds.passphrase;
        info!("API key derived successfully");
        ClobClient::new(signer.clone(), bot_config.clone())
    } else {
        ClobClient::new(signer.clone(), bot_config.clone())
    };

    let client = Arc::new(clob_client);

    // Initialize components
    let config_snapshot = shared_config.read().await;
    let tracker = Arc::new(RwLock::new(WalletTracker::from_config(
        &config_snapshot.copy_trading.watchlist,
    )));
    drop(config_snapshot);

    let logger = Arc::new(TradeLogger::new(PathBuf::from("data/trades.jsonl")));

    // Sync manual trades from own wallet into trade log
    let own_address = format!("{}", bot_config.funder).to_lowercase();
    match sync::sync_own_trades(&client, &logger, &own_address).await {
        Ok(n) if n > 0 => info!("synced {} manual trades from wallet", n),
        Ok(_) => {}
        Err(e) => warn!("trade sync failed (continuing): {e}"),
    }

    // Sync bankroll with on-chain USDC balance
    let bankroll = match sync::sync_bankroll(&client).await {
        Ok(balance) if balance > 0.0 => {
            info!("using on-chain bankroll: ${:.2} (CLI: ${:.2})", balance, cli.bankroll);
            balance
        }
        Ok(_) => {
            warn!("on-chain balance is 0, using CLI bankroll: ${:.2}", cli.bankroll);
            cli.bankroll
        }
        Err(e) => {
            warn!("bankroll sync failed, using CLI value ${:.2}: {e}", cli.bankroll);
            cli.bankroll
        }
    };

    let risk = Arc::new(RwLock::new(RiskManager::new(
        shared_config.read().await.risk.clone(),
        bankroll,
    )));

    // Restore open_bets (incl. per-wallet/per-sport) from trade log after restart
    {
        let existing = logger.load_all();
        let open_trades: Vec<_> = existing
            .iter()
            .filter(|t| t.filled && t.result.is_none() && !t.dry_run)
            .collect();
        let open_count = open_trades.len();
        if open_count > 0 {
            let mut r = risk.write().await;
            for t in &open_trades {
                r.record_trade_opened_with_context(
                    0.0,
                    t.copy_wallet.as_deref(),
                    &t.sport,
                );
            }
            info!("restored {} open bets from trade log", open_count);
        }
    }

    info!(
        "initialized | bankroll=${:.2} | wallets={}",
        bankroll,
        tracker.read().await.wallet_count()
    );

    // Start config hot-reload
    let config_path_clone = config_path.clone();
    let shared_config_clone = shared_config.clone();
    tokio::spawn(async move {
        config::watch_config(config_path_clone, shared_config_clone).await;
    });

    // Watchlist refresh disabled — consensus strategy uses config.yaml as source of truth.
    // Discovery is handled by prepare.py + score.py (Fase 2/3), not runtime hot-wallet scanning.
    // Old code added random wallets at weight 0.05 which polluted the polling budget.

    // Spawn resolution tracker (checks open positions every 5 min)
    let _resolver_handle = {
        let client = client.clone();
        let logger = logger.clone();
        let risk = risk.clone();
        let tracker = tracker.clone();
        let config = shared_config.clone();
        tokio::spawn(async move {
            // Wait 60s for the bot to place initial trades before first check
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            resolver::resolver_loop(client, logger, risk, tracker, config).await;
        })
    };

    // Set up graceful shutdown
    let shutdown = tokio::signal::ctrl_c();

    // Spawn main loops
    let copy_handle = {
        let client = client.clone();
        let tracker = tracker.clone();
        let config = shared_config.clone();
        let logger = logger.clone();
        let risk = risk.clone();

        tokio::spawn(async move {
            copy_trading_loop(client, tracker, config, logger, risk).await;
        })
    };

    let odds_handle = {
        let client = client.clone();
        let config = shared_config.clone();
        let logger = logger.clone();
        let risk = risk.clone();

        tokio::spawn(async move {
            odds_arb_loop(client, config, logger, risk).await;
        })
    };

    let spread_handle = {
        let client = client.clone();
        let config = shared_config.clone();
        let logger = logger.clone();
        let risk = risk.clone();

        tokio::spawn(async move {
            spread::spread_loop(client, config, logger, risk).await;
        })
    };

    // Status report loop
    let status_handle = {
        let logger = logger.clone();
        let risk = risk.clone();

        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(300)).await;
                let portfolio = Portfolio::from_logs(&logger);
                let r = risk.read().await;
                info!(
                    "STATUS: {} | bankroll=${:.2} | daily_pnl=${:.2} | open={}",
                    portfolio.summary(),
                    r.bankroll(),
                    r.daily_pnl(),
                    r.open_bets()
                );
            }
        })
    };

    // Daily reset loop
    let daily_handle = {
        let risk = risk.clone();
        tokio::spawn(async move {
            loop {
                // Wait until midnight UTC
                let now = chrono::Utc::now();
                let tomorrow = (now + chrono::Duration::days(1))
                    .date_naive()
                    .and_hms_opt(0, 0, 0)
                    .unwrap();
                let until_midnight = tomorrow
                    .and_utc()
                    .signed_duration_since(now);
                if let Ok(dur) = until_midnight.to_std() {
                    tokio::time::sleep(dur).await;
                }
                info!("resetting daily PnL counters");
                risk.write().await.reset_daily();
            }
        })
    };

    info!("bot started - press Ctrl+C to stop");

    // Wait for shutdown signal
    tokio::select! {
        _ = shutdown => {
            info!("shutdown signal received");
        }
        _ = copy_handle => {
            error!("copy trading loop exited unexpectedly");
        }
        _ = odds_handle => {
            error!("odds arb loop exited unexpectedly");
        }
        _ = spread_handle => {
            error!("spread loop exited unexpectedly");
        }
    }

    info!("shutting down...");
    let portfolio = Portfolio::from_logs(&logger);
    info!("FINAL: {}", portfolio.summary());

    Ok(())
}

async fn copy_trading_loop(
    client: Arc<ClobClient>,
    tracker: Arc<RwLock<WalletTracker>>,
    config: SharedConfig,
    logger: Arc<TradeLogger>,
    risk: Arc<RwLock<RiskManager>>,
) {
    let mut copy_trader = CopyTrader::new(client.clone(), tracker.clone(), config.clone());
    let mut executor = Executor::new(client.clone(), config.clone());
    let mut wave_budget = budget::WaveBudget::new(config.read().await.sport_sizing.clone());

    // Seed attempted map from live PM positions (source of truth).
    {
        let funder = client.funder_address();
        match client.get_wallet_positions(&funder, 500).await {
            Ok(positions) => {
                executor.seed_from_positions(&positions);
                // PM positions = source of truth. Trade log NOT used for seeding:
                // sold positions still have result=null in trades.jsonl, blocking re-buys.
            }
            Err(e) => tracing::warn!("could not seed from PM positions: {e}"),
        }
    }
    let mut polls_since_sync: u32 = 0;

    // Two-phase schedule state
    let mut game_schedule = scheduler::GameSchedule::load_from_disk(std::path::Path::new("data/schedule_cache.json"));
    let mut watched_games: Vec<scheduler::WatchedGame> = Vec::new(); // T-30 discovered, waiting for T-1
    let mut t1_bought: HashSet<String> = HashSet::new();   // event_slugs bought at T-1
    let mut t1_rechecked: HashSet<String> = HashSet::new(); // event_slugs rechecked after T-1

    // Cannae position summary: log every ~30 min (120 polls × 15s = 30 min)
    let cannae_summary_every_n: u32 = 120;
    let mut polls_since_cannae_summary: u32 = cannae_summary_every_n; // Force immediate on first cycle

    // Consensus: subgraph client + dedup set
    let consensus_cfg = config.read().await.whale_consensus.clone();
    let subgraph_client = subgraph::SubgraphClient::new(
        Some(&consensus_cfg.subgraph_url),
        consensus_cfg.query_delay_ms,
    );
    let mut consensus_bought: HashSet<String> = HashSet::new();
    let mut ob_bought: HashSet<String> = HashSet::new();

    loop {
        let poll_interval = {
            let c = config.read().await;
            let copy_enabled = c.copy_trading.enabled;
            let consensus_enabled = c.whale_consensus.enabled;
            let ob_enabled = c.orderbook_imbalance.enabled;
            if !copy_enabled && !consensus_enabled && !ob_enabled {
                tokio::time::sleep(Duration::from_secs(60)).await;
                continue;
            }
            c.copy_trading.poll_interval_seconds
        };

        // Re-sync bankroll with on-chain USDC every 20 polls (~5 min)
        polls_since_sync += 1;
        if polls_since_sync >= 20 {
            polls_since_sync = 0;
            if let Ok(balance) = sync::sync_bankroll(&client).await {
                if balance > 0.0 {
                    let mut r = risk.write().await;
                    r.update_bankroll(balance);
                }
            }
            // Full sync from trade log: fixes drift in global + per-wallet + per-sport counters
            let open_context: Vec<(Option<String>, String)> = logger.load_all()
                .into_iter()
                .filter(|t| t.filled && t.result.is_none() && !t.dry_run)
                .map(|t| (t.copy_wallet, t.sport))
                .collect();
            {
                let mut r = risk.write().await;
                r.sync_full(&open_context);
            }
        }

        // Poll for new copy signals
        let (_copy_signals, raw_positions) = copy_trader.poll().await;

        // Ensure schedule is fresh before CANNAE GAMES log (needs kickoff times)
        {
            let schedule_cfg = config.read().await.schedule.clone();
            if schedule_cfg.enabled && game_schedule.needs_refresh(schedule_cfg.refresh_interval_minutes as i64) {
                let watchlist = config.read().await.copy_trading.watchlist.clone();
                let sport_tags = if schedule_cfg.sport_tags.is_empty() {
                    scheduler::sport_tags_from_watchlist(&watchlist)
                } else {
                    schedule_cfg.sport_tags.clone()
                };
                if !sport_tags.is_empty() {
                    scheduler::refresh_schedule(&client, &sport_tags, &mut game_schedule).await;
                }
            }
        }

        // Periodic Cannae position summary + budget refresh: every ~30 min
        polls_since_cannae_summary += 1;
        if polls_since_cannae_summary >= cannae_summary_every_n {
            polls_since_cannae_summary = 0;
            // Refresh budget config in case of hot-reload
            wave_budget.update_config(config.read().await.sport_sizing.clone());

            // Collect positions from the primary wallet (first in watchlist)
            let cannae_positions: Vec<_> = raw_positions
                .iter()
                .next()
                .map(|(_, _, positions)| positions.clone())
                .unwrap_or_default();

            wave_budget.refresh_from_positions(&cannae_positions);

            // Fetch own positions for recycling estimate
            {
                let cash = risk.read().await.bankroll();
                wave_budget.refresh_budget(cash, &game_schedule);
                wave_budget.log_flight_board(&game_schedule, cash);
            }
        }

        // --- Continuous discovery + T-5 confirm+buy ---
        {
            let (schedule_cfg, watchlist) = {
                let c = config.read().await;
                (c.schedule.clone(), c.copy_trading.watchlist.clone())
            };
            if schedule_cfg.enabled {
                // Derive sport tags from watchlist leagues (union of all wallets)
                let sport_tags = if schedule_cfg.sport_tags.is_empty() {
                    scheduler::sport_tags_from_watchlist(&watchlist)
                } else {
                    schedule_cfg.sport_tags.clone()
                };

                if !sport_tags.is_empty() {
                    // Schedule already refreshed above (before CANNAE GAMES log)

                    // 2. Continuous discovery: find ALL upcoming games where Cannae has positions
                    let already_watched: HashSet<String> = watched_games.iter()
                        .map(|g| g.event_slug.clone())
                        .chain(t1_bought.iter().cloned())
                        .collect();

                    let new_watched = scheduler::discover_continuous_from_positions(
                        &game_schedule,
                        &watchlist,
                        &already_watched,
                        &raw_positions,
                    );

                    if !new_watched.is_empty() {
                        info!(
                            "DISCOVERED: {} new games to watch",
                            new_watched.len()
                        );
                        watched_games.extend(new_watched);
                    }

                    // 3. T-1 Buy: confirm Cannae positions and buy all legs.
                    //    Window = 0..t10_minutes (set to 1 min = T-1).
                    let t1_matches_primary = scheduler::confirm_and_execute_t5(
                        &watched_games,
                        &watchlist,
                        schedule_cfg.t10_minutes,
                        &t1_bought,
                        &raw_positions,
                    );

                    // Always use latest config for sizing (hot-reload may have changed %)
                    wave_budget.update_config(config.read().await.sport_sizing.clone());

                    // Per-poll dedup: primary and recheck passes use the same T-1 window,
                    // so track evaluated games this iteration to avoid double processing.
                    let mut tried_this_poll: HashSet<String> = HashSet::new();

                    // Sort T-1 matches by Cannae game total DESC (biggest games first)
                    let mut t1_sorted_primary = t1_matches_primary;
                    t1_sorted_primary.sort_by(|a, b| {
                        let a_total = wave_budget.cannae_games
                            .get(&a.game_event_slug)
                            .map(|g| g.total_usdc)
                            .unwrap_or(0.0);
                        let b_total = wave_budget.cannae_games
                            .get(&b.game_event_slug)
                            .map(|g| g.total_usdc)
                            .unwrap_or(0.0);
                        b_total.partial_cmp(&a_total).unwrap_or(std::cmp::Ordering::Equal)
                    });

                    // T-1 buy pass: all legs (win, draw, opponent NO) in one shot.
                    for t1_match in &t1_sorted_primary {
                        if t1_bought.contains(&t1_match.game_event_slug) {
                            continue;
                        }
                        let game = stability::StableGame {
                            event_slug: t1_match.game_event_slug.clone(),
                            positions: t1_match.positions.clone(),
                            source_wallet: t1_match.wallet_address.clone(),
                            source_name: t1_match.wallet_name.clone(),
                        };
                        info!(
                            "T1 BUY: {} from {} ({} positions, discovery had {})",
                            game.event_slug, game.source_name,
                            t1_match.positions.len(), t1_match.t30_position_count,
                        );
                        let bought = execute_stable_game(
                            &game, &mut executor, &risk, &logger, &config, true,
                            &client, &game_schedule,
                        ).await;
                        if !bought.is_empty() {
                            t1_bought.insert(t1_match.game_event_slug.clone());
                            spawn_t5_verify(
                                bought,
                                t1_match.game_event_slug.clone(),
                                t1_match.wallet_address.clone(),
                                client.clone(),
                                logger.clone(),
                            );
                            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        }
                    }

                    // 4. T-1 recheck: catch games that arrived after primary T-1 pass.
                    //    Same window. Condition-level dedup prevents double-buying.
                    let t1_matches = scheduler::confirm_and_execute_t5(
                        &watched_games,
                        &watchlist,
                        schedule_cfg.t1_minutes,
                        &t1_rechecked,
                        &raw_positions,
                    );

                    // Biggest Cannae games first for capital priority.
                    let mut t1_sorted = t1_matches;
                    t1_sorted.sort_by(|a, b| {
                        let a_total = wave_budget.cannae_games
                            .get(&a.game_event_slug)
                            .map(|g| g.total_usdc)
                            .unwrap_or(0.0);
                        let b_total = wave_budget.cannae_games
                            .get(&b.game_event_slug)
                            .map(|g| g.total_usdc)
                            .unwrap_or(0.0);
                        b_total.partial_cmp(&a_total).unwrap_or(std::cmp::Ordering::Equal)
                    });

                    for t1_match in &t1_sorted {
                        // Same-poll dedup: skip if primary T-1 already handled this game.
                        if tried_this_poll.contains(&t1_match.game_event_slug) {
                            continue;
                        }
                        if t1_rechecked.contains(&t1_match.game_event_slug) {
                            continue;
                        }
                        let game = stability::StableGame {
                            event_slug: t1_match.game_event_slug.clone(),
                            positions: t1_match.positions.clone(),
                            source_wallet: t1_match.wallet_address.clone(),
                            source_name: t1_match.wallet_name.clone(),
                        };
                        info!(
                            "T1 RECHECK: {} from {} ({} positions)",
                            game.event_slug, game.source_name,
                            t1_match.positions.len(),
                        );
                        let bought = execute_stable_game(
                            &game, &mut executor, &risk, &logger, &config, true,
                            &client, &game_schedule,
                        ).await;
                        if !bought.is_empty() {
                            t1_rechecked.insert(t1_match.game_event_slug.clone());
                            // Spawn T+5 verification task
                            spawn_t5_verify(
                                bought,
                                t1_match.game_event_slug.clone(),
                                t1_match.wallet_address.clone(),
                                client.clone(),
                                logger.clone(),
                            );
                            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        }
                    }

                    // Cleanup: remove watched games that started >2h ago (keep recent for T-1 window)
                    let now = chrono::Utc::now();
                    let cleanup_cutoff = now - chrono::Duration::hours(2);
                    watched_games.retain(|g| g.start_time > cleanup_cutoff);
                }
            }
        }

        // ══════════════════════════════════════════════════════════════
        // CONSENSUS STRATEGY: whale consensus on football win markets
        // ══════════════════════════════════════════════════════════════
        {
            let cons_cfg = config.read().await.whale_consensus.clone();
            if cons_cfg.enabled {
                let now = chrono::Utc::now();
                let t1_window = chrono::Duration::minutes(
                    config.read().await.schedule.t10_minutes as i64 + 2
                );

                // Find football games in T-1 window
                let football_games: Vec<&scheduler::UpcomingGame> = game_schedule.games.iter()
                    .filter(|g| {
                        let until = g.start_time.signed_duration_since(now);
                        until >= chrono::Duration::zero() && until <= t1_window
                    })
                    .filter(|g| {
                        // Football only (not US sports)
                        let sport = g.event_slug.split('-').next().unwrap_or("");
                        !matches!(sport, "nba" | "nhl" | "mlb" | "nfl" | "cbb" | "ncaa")
                    })
                    .filter(|g| {
                        // Need 2+ win markets (2 teams)
                        let win_markets = g.market_tokens.iter()
                            .filter(|(_, q, _)| {
                                let ql = q.to_lowercase();
                                ql.contains("win") && !ql.contains("draw") && !ql.contains("o/u")
                                    && !ql.contains("both") && !ql.contains("spread")
                            })
                            .count();
                        win_markets >= 2
                    })
                    .filter(|g| !consensus_bought.contains(&g.event_slug))
                    .collect();

                for game in &football_games {
                    let slug = &game.event_slug;

                    // Get win-market condition IDs
                    let win_cids: Vec<String> = game.market_tokens.iter()
                        .filter(|(_, q, _)| {
                            let ql = q.to_lowercase();
                            ql.contains("win") && !ql.contains("draw") && !ql.contains("o/u")
                                && !ql.contains("both") && !ql.contains("spread")
                        })
                        .map(|(cid, _, _)| cid.clone())
                        .collect();

                    if win_cids.len() < 2 { continue; }

                    // Query subgraph for top holders
                    let holders = subgraph_client.top_holders_batch(
                        &win_cids, cons_cfg.top_n_holders
                    ).await;

                    if holders.is_empty() { continue; }

                    // Calculate consensus
                    let result = match consensus::calculate_consensus(
                        &holders,
                        &game_schedule,
                        slug,
                        cons_cfg.min_consensus_pct,
                        cons_cfg.min_traders,
                    ) {
                        Some(r) => r,
                        None => continue,
                    };

                    // Build signal and execute
                    let buy_price = match client.get_best_ask(&result.buy_token_id).await {
                        Ok(p) if p > 0.0 && p < 0.95 => (p * 100.0).ceil() / 100.0,
                        Ok(p) => {
                            info!("CONSENSUS SKIP: {} — ask {:.2} out of range", slug, p);
                            continue;
                        }
                        Err(e) => {
                            info!("CONSENSUS SKIP: {} — orderbook error: {}", slug, e);
                            continue;
                        }
                    };

                    let sport = slug.split('-').next().unwrap_or("").to_string();
                    let agg_signal = signal::AggregatedSignal {
                        token_id: result.buy_token_id.clone(),
                        condition_id: result.buy_cid.clone(),
                        side: "BUY".to_string(),
                        price: buy_price,
                        market_title: result.buy_question.clone(),
                        market_type: "win".to_string(),
                        sport: sport.clone(),
                        outcome: "No".to_string(),
                        event_slug: slug.clone(),
                        sources: vec![signal::SignalSource::Copy(signal::CopySignal {
                            source_wallet: "consensus".to_string(),
                            source_name: "WhaleConsensus".to_string(),
                            token_id: result.buy_token_id.clone(),
                            condition_id: result.buy_cid.clone(),
                            side: "BUY".to_string(),
                            price: buy_price,
                            size: result.consensus_shares,
                            market_title: result.buy_question.clone(),
                            sport: sport.clone(),
                            outcome: "No".to_string(),
                            event_slug: slug.clone(),
                            confidence: result.consensus_pct / 100.0,
                            consensus_count: result.n_total_traders as u32,
                            consensus_wallets: vec!["WhaleConsensus".to_string()],
                            timestamp: chrono::Utc::now(),
                            signal_delay_ms: 0,
                        })],
                        combined_confidence: (buy_price * 1.10).min(0.95),
                        edge_pct: 0.0,
                        source_size_usdc: result.consensus_shares,
                        source_shares: result.consensus_shares,
                    };

                    info!(
                        "T1 BUY CONSENSUS: {} — No @ {:.0}ct | {:.0}% consensus ({} traders, {:.0} shares)",
                        slug, buy_price * 100.0, result.consensus_pct,
                        result.n_total_traders, result.consensus_shares
                    );

                    let mut risk_guard = risk.write().await;
                    let ok = executor.execute_flat(
                        &agg_signal, &mut risk_guard, &logger, true, cons_cfg.sizing_pct
                    ).await;
                    drop(risk_guard);

                    match ok {
                        Ok(true) => {
                            consensus_bought.insert(slug.clone());
                        }
                        Ok(false) => {
                            info!("CONSENSUS SKIP: {} — executor returned false", slug);
                        }
                        Err(e) => {
                            tracing::warn!("CONSENSUS ERROR: {} — {}", slug, e);
                        }
                    }

                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                }
            }
        }

        // ══════════════════════════════════════════════════════════════
        // ORDERBOOK IMBALANCE: buy opponent No when bid depth is lopsided
        // Also: spread imbalance when confirmed by win imbalance.
        // ══════════════════════════════════════════════════════════════
        {
            let ob_cfg = config.read().await.orderbook_imbalance.clone();
            if ob_cfg.enabled {
                let now = chrono::Utc::now();
                let window = chrono::Duration::minutes(ob_cfg.window_minutes);

                // Football games in window, not yet bought
                let ob_games: Vec<&scheduler::UpcomingGame> = game_schedule.games.iter()
                    .filter(|g| {
                        let until = g.start_time.signed_duration_since(now);
                        until >= chrono::Duration::zero() && until <= window
                    })
                    .filter(|g| {
                        let sport = g.event_slug.split('-').next().unwrap_or("");
                        !matches!(sport, "nba" | "nhl" | "mlb" | "nfl" | "cbb" | "ncaa")
                    })
                    .filter(|g| !ob_bought.contains(&g.event_slug))
                    .collect();

                for game in &ob_games {
                    let slug = &game.event_slug;

                    // Helper: extract markets by type filter
                    let extract_markets = |filter: &dyn Fn(&str) -> bool| -> Vec<(&str, &str, &str, &str)> {
                        game.market_tokens.iter()
                            .filter(|(_, q, _)| filter(&q.to_lowercase()))
                            .filter_map(|(cid, q, tokens)| {
                                let yes = tokens.iter().find(|(o, _)| o == "Yes").map(|(_, t)| t.as_str());
                                let no = tokens.iter().find(|(o, _)| o == "No").map(|(_, t)| t.as_str());
                                match (yes, no) {
                                    (Some(y), Some(n)) => Some((cid.as_str(), q.as_str(), y, n)),
                                    _ => None,
                                }
                            })
                            .collect()
                    };

                    let win_markets = extract_markets(&|ql: &str| {
                        ql.contains("win") && !ql.contains("draw") && !ql.contains("o/u")
                            && !ql.contains("both") && !ql.contains("spread")
                    });

                    if win_markets.len() < 2 { continue; }

                    // Fetch full orderbooks for all win market Yes tokens
                    // market_data: (cid, question, yes_token, no_token, bid_depth, best_bid_price, concentration)
                    struct ObMarket<'a> {
                        cid: &'a str,
                        question: &'a str,
                        yes_token: &'a str,
                        no_token: &'a str,
                        bid_depth: f64,
                        ask_depth: f64,
                        best_bid: f64,      // Yes best bid = implied win probability
                        concentration: f64,  // fraction of depth at best bid
                    }

                    let mut market_data: Vec<ObMarket> = Vec::new();
                    let mut total_depth = 0.0;
                    let mut fetch_ok = true;

                    for (cid, q, yes_tok, no_tok) in &win_markets {
                        match client.get_orderbook(yes_tok).await {
                            Ok(book) => {
                                let bd = book.bid_depth_usdc();
                                let ad = book.ask_depth_usdc();
                                let bb = book.best_bid().unwrap_or(0.0);
                                let conc = book.bid_concentration();
                                market_data.push(ObMarket {
                                    cid, question: q, yes_token: yes_tok, no_token: no_tok,
                                    bid_depth: bd, ask_depth: ad, best_bid: bb, concentration: conc,
                                });
                                total_depth += bd;
                            }
                            Err(e) => {
                                info!("OB SKIP: {} — orderbook error for {}: {}", slug, q, e);
                                fetch_ok = false;
                                break;
                            }
                        }
                        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                    }

                    if !fetch_ok { continue; }

                    // ── Compute SURPRISE per team ──
                    // surprise = actual_depth_share - expected_depth_share (from price)
                    let price_sum: f64 = market_data.iter().map(|m| m.best_bid).sum();
                    let mut best_surprise_idx = 0usize;
                    let mut best_surprise = f64::NEG_INFINITY;

                    for (i, m) in market_data.iter().enumerate() {
                        let expected_share = if price_sum > 0.0 { m.best_bid / price_sum } else { 0.5 };
                        let actual_share = if total_depth > 0.0 { m.bid_depth / total_depth } else { 0.5 };
                        let surprise = actual_share - expected_share;
                        if surprise > best_surprise {
                            best_surprise = surprise;
                            best_surprise_idx = i;
                        }
                    }

                    // Raw ratio for logging
                    let mut sorted_depths: Vec<f64> = market_data.iter().map(|m| m.bid_depth).collect();
                    sorted_depths.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
                    let raw_ratio = if sorted_depths.len() >= 2 && sorted_depths[1] > 0.01 {
                        sorted_depths[0] / sorted_depths[1]
                    } else { 0.0 };

                    // ── Snapshot logging (every game, regardless of trade) ──
                    {
                        let snap = serde_json::json!({
                            "ts": now.to_rfc3339(),
                            "slug": slug,
                            "kickoff": game.start_time.to_rfc3339(),
                            "markets": market_data.iter().enumerate().map(|(i, m)| {
                                let exp = if price_sum > 0.0 { m.best_bid / price_sum } else { 0.5 };
                                let act = if total_depth > 0.0 { m.bid_depth / total_depth } else { 0.5 };
                                serde_json::json!({
                                    "cid": m.cid, "q": m.question,
                                    "bid_depth": format!("{:.1}", m.bid_depth),
                                    "ask_depth": format!("{:.1}", m.ask_depth),
                                    "best_bid": format!("{:.3}", m.best_bid),
                                    "concentration": format!("{:.2}", m.concentration),
                                    "expected_share": format!("{:.3}", exp),
                                    "actual_share": format!("{:.3}", act),
                                    "surprise": format!("{:.3}", act - exp),
                                })
                            }).collect::<Vec<_>>(),
                            "total_bid_depth": format!("{:.1}", total_depth),
                            "raw_ratio": format!("{:.2}", raw_ratio),
                            "best_surprise": format!("{:.3}", best_surprise),
                        });
                        if let Ok(mut f) = std::fs::OpenOptions::new()
                            .create(true).append(true)
                            .open("data/ob_snapshots.jsonl")
                        {
                            use std::io::Write;
                            let _ = writeln!(f, "{}", snap);
                        }
                    }

                    // Check minimum liquidity
                    if total_depth < ob_cfg.min_depth_usdc {
                        info!("OB SKIP: {} — total depth ${:.0} < min ${:.0}",
                              slug, total_depth, ob_cfg.min_depth_usdc);
                        ob_bought.insert(slug.clone());
                        continue;
                    }

                    // ── Decision: use SURPRISE not raw ratio ──
                    // Positive surprise = more depth than price implies = smart money signal.
                    // The team with highest surprise is the one the orderbook "likes more than the price says."
                    let signal_team = &market_data[best_surprise_idx];

                    if best_surprise < ob_cfg.min_surprise {
                        info!("OB SKIP: {} — surprise {:.1}% < {:.1}% (depth: ${:.0} vs ${:.0}, conc: {:.0}%/{:.0}%)",
                              slug, best_surprise * 100.0, ob_cfg.min_surprise * 100.0,
                              market_data[0].bid_depth, market_data[1].bid_depth,
                              market_data[0].concentration * 100.0, market_data[1].concentration * 100.0);
                        ob_bought.insert(slug.clone());
                        continue;
                    }

                    // Concentration filter: signal team should have concentrated depth (informed, not MM)
                    if signal_team.concentration < ob_cfg.min_concentration {
                        info!("OB SKIP: {} — concentration {:.0}% < {:.0}% (likely market maker)",
                              slug, signal_team.concentration * 100.0, ob_cfg.min_concentration * 100.0);
                        ob_bought.insert(slug.clone());
                        continue;
                    }

                    // ── WIN trade: buy opponent No (signal team expected to win) ──
                    // Find opponent = the team that is NOT the signal team
                    let opponent = market_data.iter().find(|m| m.cid != signal_team.cid);
                    let opponent = match opponent {
                        Some(o) => o,
                        None => continue,
                    };

                    let buy_no_token = opponent.no_token;
                    let buy_cid = opponent.cid;
                    let buy_question = opponent.question;

                    let buy_price = match client.get_best_ask(buy_no_token).await {
                        Ok(p) if p > 0.05 && p < 0.95 => p,
                        Ok(p) => {
                            info!("OB SKIP: {} — No ask {:.2} out of range", slug, p);
                            ob_bought.insert(slug.clone());
                            continue;
                        }
                        Err(e) => {
                            info!("OB SKIP: {} — No token orderbook error: {}", slug, e);
                            continue;
                        }
                    };

                    info!(
                        "OB SIGNAL: {} — surprise {:.1}% conc {:.0}% (${:.0} vs ${:.0}, prices {:.0}¢/{:.0}¢) → buy {} No @ {:.0}¢",
                        slug, best_surprise * 100.0, signal_team.concentration * 100.0,
                        signal_team.bid_depth, opponent.bid_depth,
                        signal_team.best_bid * 100.0, opponent.best_bid * 100.0,
                        buy_question, buy_price * 100.0
                    );

                    let sport = slug.split('-').next().unwrap_or("").to_string();
                    let ratio = raw_ratio; // keep for logging
                    let agg_signal = signal::AggregatedSignal {
                        token_id: buy_no_token.to_string(),
                        condition_id: buy_cid.to_string(),
                        side: "BUY".to_string(),
                        price: buy_price,
                        market_title: buy_question.to_string(),
                        market_type: "win".to_string(),
                        sport: sport.clone(),
                        outcome: "No".to_string(),
                        event_slug: slug.clone(),
                        sources: vec![signal::SignalSource::Copy(signal::CopySignal {
                            source_wallet: "orderbook".to_string(),
                            source_name: "OB_Surprise".to_string(),
                            token_id: buy_no_token.to_string(),
                            condition_id: buy_cid.to_string(),
                            side: "BUY".to_string(),
                            price: buy_price,
                            size: signal_team.bid_depth,
                            market_title: buy_question.to_string(),
                            sport: sport.clone(),
                            outcome: "No".to_string(),
                            event_slug: slug.clone(),
                            confidence: (best_surprise * 5.0).clamp(0.1, 0.95),
                            consensus_count: 0,
                            consensus_wallets: vec![],
                            timestamp: chrono::Utc::now(),
                            signal_delay_ms: 0,
                        })],
                        combined_confidence: (buy_price * 1.10).min(0.95),
                        edge_pct: best_surprise * 100.0,
                        source_size_usdc: signal_team.bid_depth,
                        source_shares: 0.0,
                    };

                    let mut risk_guard = risk.write().await;
                    let ok = executor.execute_flat(
                        &agg_signal, &mut risk_guard, &logger, true, ob_cfg.sizing_pct
                    ).await;
                    drop(risk_guard);

                    match ok {
                        Ok(true) => {
                            info!("OB BOUGHT: {} — {} No @ {:.0}¢ (ratio {:.1}x)",
                                  slug, buy_question, buy_price * 100.0, ratio);
                            ob_bought.insert(slug.clone());
                        }
                        Ok(false) => {
                            info!("OB SKIP: {} — executor returned false", slug);
                            ob_bought.insert(slug.clone());
                        }
                        Err(e) => {
                            tracing::warn!("OB ERROR: {} — {}", slug, e);
                        }
                    }

                    // ── SPREAD trade: if win ratio confirms, check spread market ──
                    if ob_cfg.spread_enabled && ratio >= ob_cfg.spread_min_win_ratio {
                        let spread_markets = extract_markets(&|ql: &str| {
                            ql.contains("spread")
                        });

                        if spread_markets.len() >= 2 {
                            // Fetch spread orderbooks
                            let mut sp_depths: Vec<(&str, &str, &str, &str, f64)> = Vec::new();
                            let mut sp_ok = true;
                            for (cid, q, yes_tok, no_tok) in &spread_markets {
                                match client.get_orderbook(yes_tok).await {
                                    Ok(book) => {
                                        sp_depths.push((cid, q, yes_tok, no_tok, book.bid_depth_usdc()));
                                    }
                                    Err(_) => { sp_ok = false; break; }
                                }
                                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                            }

                            if sp_ok && sp_depths.len() >= 2 {
                                sp_depths.sort_by(|a, b| b.4.partial_cmp(&a.4).unwrap_or(std::cmp::Ordering::Equal));
                                let sp_strong = &sp_depths[0];
                                let sp_weak = &sp_depths[1];
                                let sp_total: f64 = sp_depths.iter().map(|d| d.4).sum();
                                let sp_ratio = if sp_weak.4 > 0.01 { sp_strong.4 / sp_weak.4 } else { 0.0 };

                                // Log spread snapshot
                                if let Ok(mut f) = std::fs::OpenOptions::new()
                                    .create(true).append(true)
                                    .open("data/ob_spread_snapshots.jsonl")
                                {
                                    use std::io::Write;
                                    let snap = serde_json::json!({
                                        "ts": now.to_rfc3339(),
                                        "slug": slug,
                                        "win_ratio": format!("{:.2}", ratio),
                                        "spread_ratio": format!("{:.2}", sp_ratio),
                                        "spread_depth": format!("{:.1}", sp_total),
                                        "markets": sp_depths.iter().map(|(cid, q, _, _, d)| {
                                            serde_json::json!({"cid": cid, "q": q, "depth": format!("{:.1}", d)})
                                        }).collect::<Vec<_>>(),
                                    });
                                    let _ = writeln!(f, "{}", snap);
                                }

                                if sp_ratio >= ob_cfg.spread_min_ratio && sp_total >= ob_cfg.min_depth_usdc {
                                    // Buy spread: weakest spread team No (strongest team covers)
                                    let sp_buy_token = sp_weak.3;
                                    let sp_buy_cid = sp_weak.0;
                                    let sp_buy_q = sp_weak.1;

                                    if let Ok(sp_price) = client.get_best_ask(sp_buy_token).await {
                                        if sp_price > 0.05 && sp_price < 0.95 {
                                            info!(
                                                "OB SPREAD SIGNAL: {} — win {:.1}x + spread {:.1}x → {} No @ {:.0}¢",
                                                slug, ratio, sp_ratio, sp_buy_q, sp_price * 100.0
                                            );

                                            let sp_signal = signal::AggregatedSignal {
                                                token_id: sp_buy_token.to_string(),
                                                condition_id: sp_buy_cid.to_string(),
                                                side: "BUY".to_string(),
                                                price: sp_price,
                                                market_title: sp_buy_q.to_string(),
                                                market_type: "spread".to_string(),
                                                sport: sport.clone(),
                                                outcome: "No".to_string(),
                                                event_slug: slug.clone(),
                                                sources: vec![signal::SignalSource::Copy(signal::CopySignal {
                                                    source_wallet: "orderbook".to_string(),
                                                    source_name: "OB_Spread".to_string(),
                                                    token_id: sp_buy_token.to_string(),
                                                    condition_id: sp_buy_cid.to_string(),
                                                    side: "BUY".to_string(),
                                                    price: sp_price,
                                                    size: sp_strong.4,
                                                    market_title: sp_buy_q.to_string(),
                                                    sport: sport.clone(),
                                                    outcome: "No".to_string(),
                                                    event_slug: slug.clone(),
                                                    confidence: (sp_ratio / 10.0).min(0.95),
                                                    consensus_count: 0,
                                                    consensus_wallets: vec![],
                                                    timestamp: chrono::Utc::now(),
                                                    signal_delay_ms: 0,
                                                })],
                                                combined_confidence: (sp_price * 1.10).min(0.95),
                                                edge_pct: 0.0,
                                                source_size_usdc: sp_strong.4,
                                                source_shares: 0.0,
                                            };

                                            let mut rg = risk.write().await;
                                            match executor.execute_flat(
                                                &sp_signal, &mut rg, &logger, true, ob_cfg.spread_sizing_pct
                                            ).await {
                                                Ok(true) => {
                                                    info!("OB SPREAD BOUGHT: {} — {} No @ {:.0}¢", slug, sp_buy_q, sp_price * 100.0);
                                                }
                                                Ok(false) => {
                                                    info!("OB SPREAD SKIP: {} — executor false", slug);
                                                }
                                                Err(e) => {
                                                    warn!("OB SPREAD ERROR: {} — {}", slug, e);
                                                }
                                            }
                                            drop(rg);
                                        }
                                    }
                                } else if sp_ratio > 0.0 {
                                    info!("OB SPREAD SKIP: {} — spread ratio {:.1}x (need {:.1}x) depth ${:.0}",
                                          slug, sp_ratio, ob_cfg.spread_min_ratio, sp_total);
                                }
                            }
                        }
                    }

                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                }
            }
        }

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
    }
}

/// A leg we successfully bought — used by T+5 verification to detect mismatches.
#[derive(Clone, Debug)]
struct BoughtLeg {
    condition_id: String,
    outcome: String,
    token_id: String,
    price: f64,
    market_title: String,
    game_line: String,
    /// True for OPP_NO legs (synthetic, not a direct Cannae copy) — skip T+5 verify
    synthetic: bool,
}

/// Spawn a background task that waits 5 minutes, then re-checks Cannae's positions
/// for this game. If Cannae has dropped a leg we bought (CV → 0), sell our position.
///
/// This catches the scenario where Cannae flipped or exited a position between our
/// T-5 snapshot and the actual game start. Selling a wrong leg early limits losses.
///
/// Fix #1: Uses our actual PM positions for share count (not estimated).
/// Fix #2: Polls sell order for fill confirmation, cancels if unfilled.
/// Fix #3: Skips synthetic OPP_NO legs (Cannae never held those condition_ids).
fn spawn_t5_verify(
    bought_legs: Vec<BoughtLeg>,
    event_slug: String,
    cannae_wallet: String,
    client: Arc<ClobClient>,
    logger: Arc<TradeLogger>,
) {
    tokio::spawn(async move {
        // Wait 5 minutes after execution
        tokio::time::sleep(Duration::from_secs(300)).await;

        // Filter out synthetic legs (OPP_NO) — Cannae never held those condition_ids
        let verifiable: Vec<&BoughtLeg> = bought_legs.iter()
            .filter(|l| !l.synthetic)
            .collect();

        if verifiable.is_empty() {
            info!("T+5 VERIFY: {} — all {} legs are synthetic, skipping", event_slug, bought_legs.len());
            return;
        }

        info!("T+5 VERIFY: checking {} legs for {} ({} synthetic skipped)",
            verifiable.len(), event_slug, bought_legs.len() - verifiable.len());

        // Fetch Cannae's current positions AND our own positions in parallel
        let funder = client.funder_address();
        let (cannae_result, our_result) = tokio::join!(
            client.get_wallet_positions(&cannae_wallet, 500),
            client.get_wallet_positions(&funder, 500),
        );

        let cannae_positions = match cannae_result {
            Ok(p) => p,
            Err(e) => {
                warn!("T+5 VERIFY: failed to fetch Cannae positions: {e}");
                return;
            }
        };
        let our_positions = match our_result {
            Ok(p) => p,
            Err(e) => {
                warn!("T+5 VERIFY: failed to fetch our positions: {e}");
                return;
            }
        };

        // Build a set of Cannae's current condition_id:outcome pairs with significant CV
        let cannae_active: std::collections::HashSet<String> = cannae_positions.iter()
            .filter(|p| p.current_value_f64() > 10.0) // >$10 CV = still active
            .map(|p| {
                let cid = p.condition_id.as_deref().unwrap_or("");
                let out = p.outcome.as_deref().unwrap_or("").to_lowercase();
                format!("{}:{}", cid, out)
            })
            .collect();

        // Build a lookup for our actual shares by condition_id:outcome
        let our_shares: std::collections::HashMap<String, f64> = our_positions.iter()
            .filter(|p| p.size_f64() > 0.01)
            .map(|p| {
                let cid = p.condition_id.as_deref().unwrap_or("");
                let out = p.outcome.as_deref().unwrap_or("").to_lowercase();
                (format!("{}:{}", cid, out), p.size_f64())
            })
            .collect();

        for leg in &verifiable {
            let key = format!("{}:{}", leg.condition_id, leg.outcome.to_lowercase());
            if cannae_active.contains(&key) {
                info!("T+5 OK: {} {} — Cannae still holds", leg.game_line, leg.market_title);
                continue;
            }

            // Look up our actual shares from PM (not the estimate)
            let actual_shares = match our_shares.get(&key) {
                Some(&s) => s,
                None => {
                    warn!("T+5 MISMATCH: {} {} — Cannae dropped, but we have no PM position (already sold?)",
                        leg.game_line, leg.market_title);
                    continue;
                }
            };

            // Cannae dropped this leg — sell our position
            warn!(
                "T+5 MISMATCH: {} {} {} — Cannae no longer holds! Selling {:.1} shares (actual from PM)",
                leg.game_line, leg.outcome, leg.market_title, actual_shares
            );

            // Get best bid to sell at market
            let sell_price = match client.get_best_bid(&leg.token_id).await {
                Ok(bid) => bid,
                Err(e) => {
                    warn!("T+5 SELL FAILED: no bid for {} — {e}", leg.market_title);
                    continue;
                }
            };

            // Sell via GTC at best bid
            let order_id = match client.create_and_post_order(
                &leg.token_id,
                sell_price,
                actual_shares,
                crate::clob::client::Side::Sell,
                crate::clob::client::OrderType::GTC,
                0,
            ).await {
                Ok(resp) => {
                    let oid = resp.order_id.clone().unwrap_or_default();
                    info!(
                        "T+5 SELL PLACED: {} {} @ {:.0}ct ({:.1} shares) | order={}",
                        leg.outcome, leg.market_title, sell_price * 100.0, actual_shares, oid
                    );
                    oid
                }
                Err(e) => {
                    warn!("T+5 SELL FAILED: {} — {e}", leg.market_title);
                    continue;
                }
            };

            if order_id.is_empty() {
                continue;
            }

            // Poll for fill (3 attempts × 2s = 6s max)
            let mut filled = false;
            for _ in 0..3 {
                tokio::time::sleep(Duration::from_secs(2)).await;
                match client.get_order_status(&order_id).await {
                    Ok((status, _size_matched)) => {
                        if status == "MATCHED" {
                            filled = true;
                            break;
                        } else if status == "CANCELLED" {
                            break;
                        }
                        // LIVE/OPEN → keep polling
                    }
                    Err(_) => {}
                }
            }

            if !filled {
                // Cancel unfilled sell order
                warn!("T+5 SELL NOT FILLED: {} — cancelling order {}", leg.market_title, order_id);
                let _ = client.cancel_order(&order_id).await;
                continue;
            }

            info!("T+5 SELL FILLED: {} {} @ {:.0}ct ({:.1} shares)", leg.outcome, leg.market_title, sell_price * 100.0, actual_shares);

            // Log the sell
            let sell_pnl = (sell_price - leg.price) * actual_shares;
            logger.log(TradeLog {
                timestamp: chrono::Utc::now(),
                token_id: leg.token_id.clone(),
                condition_id: leg.condition_id.clone(),
                market_title: leg.market_title.clone(),
                sport: String::new(),
                side: "SELL".to_string(),
                outcome: leg.outcome.clone(),
                event_slug: Some(event_slug.clone()),
                price: sell_price,
                size_usdc: sell_price * actual_shares,
                size_shares: actual_shares,
                signal_source: "t5_verify".to_string(),
                copy_wallet: None,
                consensus_count: None,
                consensus_wallets: None,
                edge_pct: 0.0,
                confidence: 0.0,
                signal_delay_ms: 0,
                order_id: Some(order_id),
                filled: true,
                dry_run: false,
                result: Some("sold".to_string()),
                pnl: Some(sell_pnl),
                resolved_at: Some(chrono::Utc::now()),
                sell_price: Some(sell_price),
                actual_pnl: Some(sell_pnl),
                closing_price: None,
                taker_ask: None,
                exit_type: Some("t5_mismatch".to_string()),
                strategy_version: None,
            });
        }
    });
}

/// Per game: detect hauptbet per game line, flat size, execute.
///
/// For each game line (moneyline/draw/spread) allowed by sport config:
/// 1. Find the hauptbet (largest USDC position per conditionId of that type)
/// 2. Size: flat % from wave budget (bankroll × pct / price)
/// 3. Execute via FOK on ask
///
/// Returns the list of legs that were actually filled (empty = nothing bought).
async fn execute_stable_game(
    game: &stability::StableGame,
    executor: &mut Executor,
    risk: &Arc<RwLock<RiskManager>>,
    logger: &Arc<TradeLogger>,
    config: &SharedConfig,
    taker_mode: bool,
    client: &Arc<ClobClient>,
    game_schedule: &scheduler::GameSchedule,
) -> Vec<BoughtLeg> {
    use crate::copy_trader::CopyTrader;

    let cfg = config.read().await;
    let sport_sizing = cfg.sport_sizing.clone();
    let wallet_cfg = cfg.copy_trading.watchlist.iter()
        .find(|w| w.address.eq_ignore_ascii_case(&game.source_wallet));
    let allowed_leagues = wallet_cfg.map(|w| w.leagues.clone()).unwrap_or_default();
    let wallet_market_types = wallet_cfg.map(|w| w.market_types.clone()).unwrap_or_default();
    let avg_source_usdc_map = wallet_cfg.map(|w| w.avg_source_usdc_per_league.clone()).unwrap_or_default();
    drop(cfg);

    // League filter
    let league = game.event_slug.split('-').next().unwrap_or("");
    if !allowed_leagues.is_empty() && !allowed_leagues.iter().any(|l| l == league) {
        info!("GAME SKIP: {} — league '{}' not in {}'s allowed leagues", game.event_slug, league, game.source_name);
        return vec![];
    }

    // Which game lines are allowed for this sport?
    let mut allowed_lines = sport_sizing.allowed_game_lines(league);
    // Per-wallet market_types filter: intersect with wallet's allowed types
    if !wallet_market_types.is_empty() {
        allowed_lines.retain(|gl| wallet_market_types.iter().any(|mt| mt == gl));
    }
    if allowed_lines.is_empty() {
        info!("GAME SKIP: {} — no allowed game lines for league {}", game.event_slug, league);
        return vec![];
    }

    // Min Cannae stake filter — measures TOTAL game conviction across ALL legs
    // (win/draw/spread/ou/btts/player_prop), not just copyable ones.
    //
    // Reden (2026-04-07, na strategy-shift onderzoek): sinds W11 (2026-03-09) is in
    // 54% van NBA-games Cannae's hauptbet géén ML maar spread/ou. Een per-leg ML gate
    // filtert juist die high-conviction games eruit. Total game CV reflecteert
    // Cannae's overall conviction in het spel ongeacht welke leg het zit.
    //
    // Aanvullende eis: ml_cv > 0 — we kopiëren alleen ML, dus geen punt om een game
    // te onboarden waar Cannae geen ML-positie heeft.
    // NOTE: min_cannae_game_usdc dollar-floor gate was reverted 2026-04-08.
    // True-hauptbet guard + CV ladder below replace it.
    let _ = league;

    // Log ALL positions for this game (T5 debug)
    info!("T1 POSITIONS: {} — {} positions:", game.event_slug, game.positions.len());
    for pos in &game.positions {
        let title = pos.title.as_deref().unwrap_or("?");
        let outcome = pos.outcome.as_deref().unwrap_or("?");
        let cid = pos.condition_id.as_deref().unwrap_or("?");
        let iv = pos.initial_value_f64();
        let cv = pos.current_value_f64();
        let cp = pos.cur_price_f64();
        let sz = pos.size_f64();
        let ap = pos.avg_price_f64();
        info!("  {} {} | iv=${:.0} cv=${:.0} | {:.0}sh @ {:.2}ct (cur {:.2}ct) | cid={}..{} | {}",
            outcome, CopyTrader::detect_market_type(title),
            iv, cv, sz, ap * 100.0, cp * 100.0,
            &cid[..cid.len().min(8)], &cid[cid.len().saturating_sub(4)..],
            &title[..title.len().min(50)]);
    }

    // ─── True hauptbet guard + Cannae CV ladder ───
    // Part 1 (2026-04-10 selector bugfix): the per-allowed-line selector below
    // is blind to market types outside allowed_game_lines. For football that's
    // BTTS/OU/Spread; for NBA it's OU. Cannae's actual max-CV leg frequently
    // sits in those excluded types (e.g. SCF-CEL BTTS NO $3,179 vs our
    // "hauptbet" Freiburg WIN NO $1,387). Rule: if the true hauptbet (max CV
    // across ALL types) lives in a type we can't copy, skip the whole game.
    //
    // Part 2 (2026-04-10 ladder): the true hauptbet CV determines a fixed
    // size multiplier applied uniformly to all legs on this game. Skip-floor
    // at $500 CV. See sizing::cannae_cv_multiplier and memory
    // feedback_hauptbet_strategy.md for the ladder.
    let game_multiplier: f64 = match game.positions.iter()
        .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal))
    {
        None => {
            info!("GAME SKIP: {} — no positions", game.event_slug);
            return vec![];
        }
        Some(true_haupt) => {
            let haupt_type = CopyTrader::detect_market_type(true_haupt.title.as_deref().unwrap_or(""));
            let haupt_cv = true_haupt.current_value_f64();
            if !allowed_lines.iter().any(|l| *l == haupt_type.as_str()) {
                info!(
                    "GAME SKIP: {} — true hauptbet is '{}' (${:.0} CV, {} / {}), not in allowed {:?}. Skipping rather than copying a minor leg.",
                    game.event_slug,
                    haupt_type,
                    haupt_cv,
                    true_haupt.outcome.as_deref().unwrap_or("?"),
                    true_haupt.title.as_deref().unwrap_or("?"),
                    allowed_lines
                );
                return vec![];
            }
            {
                if haupt_cv <= 0.0 {
                    info!(
                        "GAME SKIP: {} — true hauptbet CV ${:.0} (type {}, {} / {})",
                        game.event_slug, haupt_cv, haupt_type,
                        true_haupt.outcome.as_deref().unwrap_or("?"),
                        true_haupt.title.as_deref().unwrap_or("?"),
                    );
                    return vec![];
                }
                // Proportional sizing: trader's bet / trader's average for this league
                let avg_cv = avg_source_usdc_map.get(league).copied().unwrap_or(0.0);
                let m = crate::sizing::proportional_multiplier(haupt_cv, avg_cv);
                let base_pct = crate::sizing::confidence_pct(0.0);
                info!(
                    "SIZING: {} — hauptbet ${:.0} CV, avg ${:.0} → {:.2}× (effective {:.1}%)",
                    game.event_slug, haupt_cv, avg_cv, m, base_pct * m,
                );
                m
            }
        }
    };

    let base_pct = crate::sizing::confidence_pct(0.0);

    // Classify all positions by game line, using currentValue for hauptbet selection
    #[derive(Clone)]
    struct GameLineBet {
        pos: crate::clob::types::WalletPosition,
        game_line: String,
        size_pct: f64,
        fixed_size: bool, // true = skip confidence_pct override
        exempt_win_yes_ban: bool, // true = bypass SSOT win_yes_ban (3-leg case 1)
        synthetic: bool, // true = OPP_NO (constructed, not direct Cannae copy)
    }

    let bankroll = risk.read().await.bankroll();
    let mut bets: Vec<GameLineBet> = Vec::new();
    let is_football = !matches!(league, "nba" | "nhl" | "mlb" | "nfl" | "cbb" | "ncaa" | "lol" | "cs2" | "dota2" | "val");

    // Per allowed game line: find hauptbet (highest currentValue)
    for &gl in &allowed_lines {
        let hauptbet = game.positions.iter()
            .filter(|p| {
                let title = p.title.as_deref().unwrap_or("");
                CopyTrader::detect_market_type(title) == gl
            })
            .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal));

        if let Some(pos) = hauptbet {
            let price = pos.avg_price_f64();
            // NBA min price (SSOT Pilaar 1, rules.yaml: nba_min_price)
            let nba_min = rules::global().nba_min_price;
            if league == "nba" && price < nba_min {
                info!("SKIP: {} — NBA price {:.2} < {:.2} minimum ({})",
                    pos.title.as_deref().unwrap_or(""), price, nba_min, gl);
                continue;
            }
            // Confidence-based sizing from bet price
            let confidence = (price * 1.10).min(0.95);
            let pct = crate::sizing::confidence_pct(confidence);
            bets.push(GameLineBet { pos: pos.clone(), game_line: gl.to_string(), size_pct: pct, fixed_size: false, exempt_win_yes_ban: false, synthetic: false });
        }
    }

    // Football game modes — follow Cannae's sizing, only substitute Win YES→Opp NO when Draw YES present
    if is_football && !bets.is_empty() {
        let win_hauptbet = bets.iter().find(|b| b.game_line == "win").cloned();
        if let Some(win_bet) = win_hauptbet {
            let win_outcome = win_bet.pos.outcome.as_deref().unwrap_or("");
            let win_is_yes = win_outcome.eq_ignore_ascii_case("Yes")
                || (!win_outcome.eq_ignore_ascii_case("No")
                    && !win_outcome.eq_ignore_ascii_case("Under")
                    && !win_outcome.eq_ignore_ascii_case("Over"));

            let draw_positions: Vec<_> = game.positions.iter()
                .filter(|p| CopyTrader::detect_market_type(p.title.as_deref().unwrap_or("")) == "draw")
                .collect();
            let draw_yes_cv: f64 = draw_positions.iter()
                .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("Yes"))
                .map(|p| p.current_value_f64())
                .sum();
            let draw_no_cv: f64 = draw_positions.iter()
                .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No"))
                .map(|p| p.current_value_f64())
                .sum();
            let has_draw_yes = draw_yes_cv > 0.0;
            let has_draw_no = draw_no_cv > 0.0;

            if !win_is_yes {
                // === WIN NO ===
                let win_no_cv = win_bet.pos.current_value_f64();
                let draw_yes_ratio = if win_no_cv > 0.0 { draw_yes_cv / win_no_cv } else { 0.0 };
                if has_draw_yes && draw_yes_ratio >= 0.05 {
                    // Win NO + Draw YES (>=5% ratio) → 3-leg: WIN_NO 5% + DRAW_YES 2.5% + WIN_NO_B 2.5%
                    // Drop any draw_no from bets, ensure draw_yes is present
                    bets.retain(|b| !(b.game_line == "draw" && b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No")));
                    if !bets.iter().any(|b| b.game_line == "draw") {
                        let draw_yes = draw_positions.iter()
                            .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("Yes"))
                            .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal));
                        if let Some(dy) = draw_yes {
                            bets.push(GameLineBet { pos: (*dy).clone(), game_line: "draw".to_string(), size_pct: base_pct * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: false });
                        }
                    }
                    // Set sizes: base 2.5% × Cannae CV ladder (2026-04-10)
                    for b in bets.iter_mut() {
                        if b.game_line == "win" {
                            b.size_pct = base_pct * game_multiplier;
                        } else if b.game_line == "draw" {
                            b.size_pct = base_pct * game_multiplier;
                        }
                        b.fixed_size = true;
                    }
                    // Add WIN_NO_B (opponent's NO) as 3rd leg @ 2%
                    let win_cid = win_bet.pos.condition_id.as_deref().unwrap_or("").to_owned();
                    let draw_cids: Vec<&str> = draw_positions.iter()
                        .filter_map(|p| p.condition_id.as_deref())
                        .collect();
                    if let Some((opp_cid, no_token_id)) = game_schedule.find_opponent_no_token(&game.event_slug, &win_cid, &draw_cids) {
                        let no_price = match game_schedule.find_yes_token(&game.event_slug, &opp_cid) {
                            Some(yes_tok) => client.get_best_bid(&yes_tok).await.map(|p| (1.0 - p).max(0.30).min(0.90)).unwrap_or(0.55),
                            None => 0.55,
                        };
                        let mut opp_pos = win_bet.pos.clone();
                        opp_pos.asset = Some(no_token_id);
                        opp_pos.condition_id = Some(opp_cid);
                        opp_pos.outcome = Some("No".to_string());
                        opp_pos.avg_price = Some(serde_json::json!(no_price));
                        opp_pos.cur_price = Some(serde_json::json!(no_price));
                        bets.push(GameLineBet { pos: opp_pos, game_line: "win".to_string(), size_pct: base_pct * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: true });
                        info!("GAME MODE: {} → 3-LEG WIN_NO+DRAW_YES+OPP_NO ({:.2}×3 legs, draw/win ratio {:.1}%)", game.event_slug, base_pct * game_multiplier, draw_yes_ratio*100.0);
                    } else {
                        info!("GAME MODE: {} → WIN_NO+DRAW_YES (opp NO not found, 2 legs only, ratio {:.1}%)", game.event_slug, draw_yes_ratio*100.0);
                    }
                } else {
                    // Check Draw NO as confirming signal:
                    // Win NO + Draw NO = "opponent wins" (not draw, not team A)
                    let draw_no_ratio = if win_no_cv > 0.0 { draw_no_cv / win_no_cv } else { 0.0 };
                    if has_draw_no && draw_no_ratio >= 0.60 {
                        // Source wallet is strongly convinced opponent wins (not just "not team A")
                        // Keep win NO + draw NO as 2-leg bet
                        bets.retain(|b| !(b.game_line == "draw" && b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("Yes")));
                        if !bets.iter().any(|b| b.game_line == "draw") {
                            let draw_no = draw_positions.iter()
                                .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No"))
                                .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal));
                            if let Some(dn) = draw_no {
                                bets.push(GameLineBet { pos: (*dn).clone(), game_line: "draw".to_string(), size_pct: base_pct * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: false });
                            }
                        }
                        for b in bets.iter_mut() {
                            b.size_pct = base_pct * game_multiplier;
                            b.fixed_size = true;
                        }
                        info!("GAME MODE: {} → WIN_NO+DRAW_NO (opponent wins, draw_no/win ratio {:.1}%)", game.event_slug, draw_no_ratio*100.0);
                    } else {
                        // Win NO only (no draw, or draw ratio too low)
                        bets.retain(|b| b.game_line != "draw");
                        if has_draw_yes {
                            info!("GAME MODE: {} → WIN_NO (draw_yes ratio {:.1}% < 60%, dropped)", game.event_slug, draw_yes_ratio*100.0);
                        } else if has_draw_no {
                            info!("GAME MODE: {} → WIN_NO (draw_no ratio {:.1}% < 60%, dropped)", game.event_slug, draw_no_ratio*100.0);
                        } else {
                            info!("GAME MODE: {} → WIN_NO ({} bets)", game.event_slug, bets.len());
                        }
                    }
                }
            } else {
                // === WIN YES ===
                if has_draw_yes {
                    let win_yes_price = win_bet.pos.avg_price_f64();
                    if win_yes_price < 0.55 {
                        // Underdog win + draw hedge: low conviction, skip win leg
                        // Keep draw YES only (lower risk, draw still pays)
                        bets.retain(|b| b.game_line != "win");
                        // If hauptbet was draw NO, replace with draw YES
                        let has_draw_no_in_bets = bets.iter().any(|b| b.game_line == "draw"
                            && b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No"));
                        if has_draw_no_in_bets {
                            bets.retain(|b| !(b.game_line == "draw"
                                && b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No")));
                        }
                        if !bets.iter().any(|b| b.game_line == "draw") {
                            let draw_yes = draw_positions.iter()
                                .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("Yes"))
                                .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal));
                            if let Some(dy) = draw_yes {
                                let dy_price = (*dy).avg_price_f64();
                                let dy_conf = (dy_price * 1.10).min(0.95);
                                bets.push(GameLineBet { pos: (*dy).clone(), game_line: "draw".to_string(), size_pct: crate::sizing::confidence_pct(dy_conf), fixed_size: false, exempt_win_yes_ban: false, synthetic: false });
                            }
                        }
                        info!("GAME MODE: {} → DRAW_YES_ONLY (win_YES price {:.2} < 0.55, skip win)", game.event_slug, win_yes_price);
                    } else {
                        // Win YES + Draw YES → replace Win YES with opponent Win NO
                        bets.retain(|b| b.game_line != "draw"); // remove draw, only buy opp NO
                        let win_cid = win_bet.pos.condition_id.as_deref().unwrap_or("").to_owned();
                        let draw_cids: Vec<&str> = draw_positions.iter()
                            .filter_map(|p| p.condition_id.as_deref())
                            .collect();
                        match game_schedule.find_opponent_no_token(&game.event_slug, &win_cid, &draw_cids) {
                            Some((opp_cid, no_token_id)) => {
                                let no_price = match game_schedule.find_yes_token(&game.event_slug, &opp_cid) {
                                    Some(yes_tok) => {
                                        client.get_best_bid(&yes_tok).await
                                            .map(|p| (1.0 - p).max(0.30).min(0.90))
                                            .unwrap_or(0.55)
                                    }
                                    None => 0.55,
                                };
                                let mut sub_pos = win_bet.pos.clone();
                                sub_pos.asset = Some(no_token_id);
                                sub_pos.condition_id = Some(opp_cid.clone());
                                sub_pos.outcome = Some("No".to_string());
                                sub_pos.avg_price = Some(serde_json::json!(no_price));
                                sub_pos.cur_price = Some(serde_json::json!(no_price));
                                bets.retain(|b| b.game_line != "win");
                                bets.push(GameLineBet { pos: sub_pos, game_line: "win".to_string(), size_pct: 0.0, fixed_size: false, exempt_win_yes_ban: false, synthetic: true });
                                info!("GAME MODE: {} → OPP_NO (win YES {:.2} + draw YES → opp NO, 1 bet)", game.event_slug, win_yes_price);
                            }
                            None => {
                                // Fallback: keep Win YES
                                info!("GAME MODE: {} → WIN_YES (opp NO not found, win YES {:.2})", game.event_slug, win_yes_price);
                            }
                        }
                    }
                } else if has_draw_no {
                    let win_yes_price = win_bet.pos.avg_price_f64();
                    if win_yes_price < 0.50 {
                        // Win YES below 50%: underdog — convert to OPP_NO (drop draw NO)
                        bets.retain(|b| b.game_line != "draw");
                        let win_cid = win_bet.pos.condition_id.as_deref().unwrap_or("").to_owned();
                        let draw_cids: Vec<&str> = draw_positions.iter()
                            .filter_map(|p| p.condition_id.as_deref())
                            .collect();
                        match game_schedule.find_opponent_no_token(&game.event_slug, &win_cid, &draw_cids) {
                            Some((opp_cid, no_token_id)) => {
                                let no_price = match game_schedule.find_yes_token(&game.event_slug, &opp_cid) {
                                    Some(yes_tok) => {
                                        client.get_best_bid(&yes_tok).await
                                            .map(|p| (1.0 - p).max(0.30).min(0.90))
                                            .unwrap_or(0.55)
                                    }
                                    None => 0.55,
                                };
                                let mut sub_pos = win_bet.pos.clone();
                                sub_pos.asset = Some(no_token_id);
                                sub_pos.condition_id = Some(opp_cid.clone());
                                sub_pos.outcome = Some("No".to_string());
                                sub_pos.avg_price = Some(serde_json::json!(no_price));
                                sub_pos.cur_price = Some(serde_json::json!(no_price));
                                bets.retain(|b| b.game_line != "win");
                                bets.push(GameLineBet { pos: sub_pos, game_line: "win".to_string(), size_pct: 0.0, fixed_size: false, exempt_win_yes_ban: false, synthetic: true });
                                info!("GAME MODE: {} → OPP_NO (win_YES {:.2} < 0.50 + draw_NO → opp NO, 1 bet)", game.event_slug, win_yes_price);
                            }
                            None => {
                                // Fallback: keep Win YES, drop draw NO
                                bets.retain(|b| b.game_line != "draw");
                                info!("GAME MODE: {} → WIN_YES (opp NO not found, win_YES {:.2} < 0.50)", game.event_slug, win_yes_price);
                            }
                        }
                    } else {
                        // Win YES + Draw NO
                        let win_yes_cv = win_bet.pos.current_value_f64();
                        let draw_no_ratio = if win_yes_cv > 0.0 { draw_no_cv / win_yes_cv } else { 0.0 };
                        let hauptbet_cid = win_bet.pos.condition_id.as_deref().unwrap_or("").to_owned();
                        bets.retain(|b| {
                            if b.game_line == "win" {
                                b.pos.condition_id.as_deref().unwrap_or("") == hauptbet_cid.as_str()
                            } else if b.game_line == "draw" {
                                b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No")
                            } else {
                                true // preserve ou and other non-win/draw legs
                            }
                        });
                        // Ensure draw NO is in bets
                        if !bets.iter().any(|b| b.game_line == "draw") {
                            let draw_no = draw_positions.iter()
                                .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No"))
                                .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal));
                            if let Some(dn) = draw_no {
                                bets.push(GameLineBet { pos: (*dn).clone(), game_line: "draw".to_string(), size_pct: base_pct * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: false });
                            }
                        }
                        if draw_no_ratio >= 0.05 {
                            // 3-LEG: WIN_YES_A + DRAW_NO + WIN_NO_B — base 2.5% × ladder (2026-04-10)
                            for b in bets.iter_mut() {
                                if b.game_line == "win" {
                                    b.size_pct = base_pct * game_multiplier;
                                    b.exempt_win_yes_ban = true;
                                } else if b.game_line == "draw" {
                                    b.size_pct = base_pct * game_multiplier;
                                }
                                b.fixed_size = true;
                            }
                            // Add WIN_NO_B (opponent NO) as 3rd leg
                            let draw_cids: Vec<&str> = draw_positions.iter()
                                .filter_map(|p| p.condition_id.as_deref())
                                .collect();
                            if let Some((opp_cid, no_token_id)) = game_schedule.find_opponent_no_token(&game.event_slug, &hauptbet_cid, &draw_cids) {
                                let no_price = match game_schedule.find_yes_token(&game.event_slug, &opp_cid) {
                                    Some(yes_tok) => client.get_best_bid(&yes_tok).await.map(|p| (1.0 - p).max(0.30).min(0.90)).unwrap_or(0.55),
                                    None => 0.55,
                                };
                                let mut opp_pos = win_bet.pos.clone();
                                opp_pos.asset = Some(no_token_id);
                                opp_pos.condition_id = Some(opp_cid);
                                opp_pos.outcome = Some("No".to_string());
                                opp_pos.avg_price = Some(serde_json::json!(no_price));
                                opp_pos.cur_price = Some(serde_json::json!(no_price));
                                bets.push(GameLineBet { pos: opp_pos, game_line: "win".to_string(), size_pct: base_pct * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: true });
                                info!("GAME MODE: {} → 3-LEG WIN_YES+DRAW_NO+OPP_NO ({:.2}%×3 legs, draw/win ratio {:.1}%, WIN_YES exempt)", game.event_slug, base_pct * game_multiplier, draw_no_ratio*100.0);
                            } else {
                                info!("GAME MODE: {} → WIN_YES+DRAW_NO (opp NO not found, 2 legs, ratio {:.1}%)", game.event_slug, draw_no_ratio*100.0);
                            }
                        } else {
                            info!("GAME MODE: {} → WIN_YES+DRAW_NO (legacy, ratio {:.1}% < 5%)", game.event_slug, draw_no_ratio*100.0);
                        }
                    }
                } else {
                    // Win YES only → replace with opponent Win NO
                    bets.retain(|b| b.game_line != "draw");
                    let win_cid = win_bet.pos.condition_id.as_deref().unwrap_or("").to_owned();
                    let draw_cids: Vec<&str> = draw_positions.iter()
                        .filter_map(|p| p.condition_id.as_deref())
                        .collect();
                    match game_schedule.find_opponent_no_token(&game.event_slug, &win_cid, &draw_cids) {
                        Some((opp_cid, no_token_id)) => {
                            let no_price = match game_schedule.find_yes_token(&game.event_slug, &opp_cid) {
                                Some(yes_tok) => {
                                    client.get_best_bid(&yes_tok).await
                                        .map(|p| (1.0 - p).max(0.30).min(0.90))
                                        .unwrap_or(0.55)
                                }
                                None => 0.55,
                            };
                            let mut sub_pos = win_bet.pos.clone();
                            sub_pos.asset = Some(no_token_id);
                            sub_pos.condition_id = Some(opp_cid.clone());
                            sub_pos.outcome = Some("No".to_string());
                            sub_pos.avg_price = Some(serde_json::json!(no_price));
                            sub_pos.cur_price = Some(serde_json::json!(no_price));
                            bets.retain(|b| b.game_line != "win");
                            bets.push(GameLineBet { pos: sub_pos, game_line: "win".to_string(), size_pct: 0.0, fixed_size: false, exempt_win_yes_ban: false, synthetic: true });
                            info!("GAME MODE: {} → OPP_NO (win YES only → opp NO, 1 bet)", game.event_slug);
                        }
                        None => {
                            // Fallback: keep Win YES
                            info!("GAME MODE: {} → WIN_YES (opp NO not found, fallback)", game.event_slug);
                        }
                    }
                }
            }
        }
    }

    // SSOT Pilaar 1, Regel 1 — WIN YES ban (rules.yaml: win_yes_ban.enabled)
    // Exempt: 3-leg case 1 (WIN_YES + DRAW_NO + OPP_NO) marks the WIN_YES leg as exempt.
    if rules::global().win_yes_ban.enabled {
        let had_banned_win_yes = bets.iter().any(|b| b.game_line == "win"
            && b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("Yes")
            && !b.exempt_win_yes_ban);
        if had_banned_win_yes {
            bets.retain(|b| !(b.game_line == "win"
                && b.pos.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("Yes")
                && !b.exempt_win_yes_ban));
            if bets.is_empty() {
                info!("GAME SKIP: {} — WIN YES dropped, no other legs", game.event_slug);
                return vec![];
            }
            info!("GAME MODE: {} — WIN YES dropped, keeping {} remaining legs", game.event_slug, bets.len());
        }
    }

    // Base sizing × Cannae CV ladder multiplier (2026-04-10).
    // Same base 2.5% for football and NBA. game_multiplier was computed from
    // the true hauptbet CV at game entry and applies uniformly to all non-fixed
    // legs on this game (fixed_size legs already got the multiplier in the
    // hedge-mode branches above).
    for b in bets.iter_mut() {
        if b.fixed_size { continue; }
        let price = b.pos.avg_price_f64();
        let conf = (price * 1.10).min(0.95);
        let base = crate::sizing::confidence_pct(conf);
        b.size_pct = base * game_multiplier;
    }

    // ─── Per-game cap: 15% bankroll total (2026-04-10) ───
    // At top tier (>$20k CV, 3× multiplier) a 3-leg hedge mode is 3 × 7.5% = 22.5%.
    // Cap the SUM of size_pct across all legs on this game at 15%. If over,
    // scale every leg proportionally down so the per-game exposure respects
    // the bankroll cap. This is independent of the per-moment 90% deployment
    // cap in risk.rs which protects the overall book, not single-game exposure.
    const PER_GAME_CAP_PCT: f64 = 15.0;
    let total_pct: f64 = bets.iter().map(|b| b.size_pct).sum();
    if total_pct > PER_GAME_CAP_PCT {
        let scale = PER_GAME_CAP_PCT / total_pct;
        for b in bets.iter_mut() {
            b.size_pct *= scale;
        }
        info!(
            "PER-GAME CAP: {} — total {:.2}% > {:.1}% cap, scaling all {} legs by {:.3}× (new total {:.2}%)",
            game.event_slug, total_pct, PER_GAME_CAP_PCT, bets.len(), scale, PER_GAME_CAP_PCT
        );
    }

    // SSOT Pilaar 1, Regel 3 — Prijsband per leg-type (rules.yaml: price_band)
    // DRAW YES bypasses per Regel 4 (min/max null in rules.yaml).
    {
        let band = &rules::global().price_band;
        bets.retain(|b| {
            let price = b.pos.avg_price_f64();
            let outcome = b.pos.outcome.as_deref().unwrap_or("");
            let is_yes = outcome.eq_ignore_ascii_case("Yes");
            let leg_band = match (b.game_line.as_str(), is_yes) {
                ("draw", true) => &band.draw_yes,
                ("draw", false) => &band.draw_no,
                ("win", false) => &band.win_no,
                _ => &band.win_no, // win+yes shouldn't reach here (banned in Regel 1)
            };
            if let Some(min) = leg_band.min {
                if price < min {
                    info!("SKIP: price {:.2} < {:.2} minimum — {} {} {}",
                        price, min, outcome, b.game_line,
                        b.pos.title.as_deref().unwrap_or(""));
                    return false;
                }
            }
            if let Some(max) = leg_band.max {
                if price > max {
                    info!("SKIP: price {:.2} > {:.2} maximum — {} {} {}",
                        price, max, outcome, b.game_line,
                        b.pos.title.as_deref().unwrap_or(""));
                    return false;
                }
            }
            true
        });
    }

    // Safety cap: max 10% total per game
    let total_pct: f64 = bets.iter().map(|b| b.size_pct).sum();
    if total_pct > 10.0 {
        let scale = 10.0 / total_pct;
        for b in bets.iter_mut() {
            b.size_pct *= scale;
        }
        info!("GAME CAP: {} — scaled {:.1}% → 10.0% ({} bets)", game.event_slug, total_pct, bets.len());
    }

    if bets.is_empty() {
        info!("GAME SKIP: {} — no bets after conviction logic", game.event_slug);
        return vec![];
    }

    // Log T5 PLAN
    info!(
        "GAME EXECUTE: {} ({}) — {} bets",
        game.event_slug, league, bets.len(),
    );
    for bet in &bets {
        let title = bet.pos.title.as_deref().unwrap_or("");
        let outcome = bet.pos.outcome.as_deref().unwrap_or("");
        let usdc = bankroll * bet.size_pct / 100.0;
        info!(
            "  T1 PLAN: {} {} | {} | ${:.0} ({:.0}%) | Cannae cv=${:.0} iv=${:.0}",
            outcome, bet.game_line, &title[..title.len().min(50)],
            usdc, bet.size_pct, bet.pos.current_value_f64(), bet.pos.initial_value_f64(),
        );
    }

    // Execute each game line bet
    let mut bought_legs: Vec<BoughtLeg> = Vec::new();
    for bet in &bets {
        let pos = &bet.pos;
        let title = pos.title.as_deref().unwrap_or("").to_string();
        let condition_id = pos.condition_id.as_deref().unwrap_or("").to_string();
        let outcome = pos.outcome.as_deref().unwrap_or("").to_string();
        let token_id = pos.asset.as_deref().unwrap_or("").to_string();
        // Use game.event_slug (from scheduler, always populated) instead of
        // pos.event_slug which PM API often returns as null
        let event_slug = game.event_slug.clone();
        let price = pos.avg_price_f64();
        let sport = CopyTrader::detect_sport_static(&title, &event_slug);
        let market_type = CopyTrader::detect_market_type(&title);
        let source_size_usdc = pos.current_value_f64();
        let source_shares = pos.size_f64();
        let confidence = (price * 1.10).min(0.95);

        let agg_signal = signal::AggregatedSignal {
            token_id: token_id.clone(),
            condition_id: condition_id.clone(),
            side: "BUY".to_string(),
            price,
            market_title: title.clone(),
            market_type,
            sport,
            outcome: outcome.clone(),
            event_slug,
            sources: vec![signal::SignalSource::Copy(signal::CopySignal {
                source_wallet: game.source_wallet.clone(),
                source_name: game.source_name.clone(),
                token_id: pos.asset.as_deref().unwrap_or("").to_string(),
                condition_id: pos.condition_id.as_deref().unwrap_or("").to_string(),
                side: "BUY".to_string(),
                price,
                size: source_size_usdc,
                market_title: title.clone(),
                sport: CopyTrader::detect_sport_static(
                    pos.title.as_deref().unwrap_or(""),
                    &game.event_slug,
                ),
                outcome: pos.outcome.as_deref().unwrap_or("").to_string(),
                event_slug: game.event_slug.clone(),
                confidence,
                consensus_count: 1,
                consensus_wallets: vec![game.source_name.clone()],
                timestamp: chrono::Utc::now(),
                signal_delay_ms: 0,
            })],
            combined_confidence: confidence,
            edge_pct: 0.0,
            source_size_usdc,
            source_shares,
        };

        // Flat sizing: bankroll × pct / price
        let mut risk_guard = risk.write().await;
        let label = if taker_mode { "T5" } else { "GAME" };
        let result = executor.execute_flat(
            &agg_signal, &mut risk_guard, &logger,
            taker_mode, bet.size_pct,
        ).await;
        match result {
            Ok(filled) => {
                if filled {
                    info!("{} FILLED: {} {} {}", label, bet.game_line,
                        agg_signal.outcome, agg_signal.market_title);
                    bought_legs.push(BoughtLeg {
                        condition_id,
                        outcome,
                        token_id,
                        price,
                        market_title: agg_signal.market_title.clone(),
                        game_line: bet.game_line.clone(),
                        synthetic: bet.synthetic,
                    });
                }
            }
            Err(e) => {
                warn!("{} ERROR: {}: {e}", label, agg_signal.market_title);
            }
        }
    }
    bought_legs
}


async fn odds_arb_loop(
    client: Arc<ClobClient>,
    config: SharedConfig,
    logger: Arc<TradeLogger>,
    risk: Arc<RwLock<RiskManager>>,
) {
    let mut executor = Executor::new(client.clone(), config.clone());

    loop {
        let (enabled, poll_interval, odds_config) = {
            let c = config.read().await;
            (
                c.odds_arb.enabled,
                c.odds_arb.poll_interval_seconds,
                c.odds_arb.clone(),
            )
        };

        if !enabled {
            tokio::time::sleep(Duration::from_secs(60)).await;
            continue;
        }

        let odds_api_key = std::env::var("ODDS_API_KEY").unwrap_or_default();
        if odds_api_key.is_empty() {
            warn!("ODDS_API_KEY not set, skipping odds arb");
            tokio::time::sleep(Duration::from_secs(poll_interval)).await;
            continue;
        }

        let odds_client = OddsClient::new(&odds_api_key, &odds_config.base_url);

        // Fetch odds for all configured sports
        let mut all_odds = Vec::new();
        for sport in &odds_config.sports {
            match odds_client.get_odds(sport).await {
                Ok(events) => all_odds.extend(events),
                Err(e) => warn!("failed to fetch odds for {sport}: {e}"),
            }
        }

        // Fetch matching Polymarket markets
        let pm_sports_tags: Vec<String> = odds_config
            .sports
            .iter()
            .map(|s| pm_tag_from_odds_sport(s))
            .collect();

        match sports::fetch_sports_markets(&client, &pm_sports_tags).await {
            Ok(pm_markets) => {
                let signals = if odds_config.mode == "close_games" {
                    let sigs = odds::find_close_game_signals(
                        &all_odds,
                        &pm_markets,
                        odds_config.max_competitiveness_pct,
                    );
                    if !sigs.is_empty() {
                        info!(
                            "CLOSE_GAMES: found {} signals from close games (comp < {:.0}%)",
                            sigs.len(),
                            odds_config.max_competitiveness_pct
                        );
                    }
                    sigs
                } else {
                    odds::find_arb_opportunities(&all_odds, &pm_markets, odds_config.min_edge_pct)
                };

                if !signals.is_empty() {
                    if odds_config.log_only {
                        info!("LOG_ONLY: would execute {} signals:", signals.len());
                        for sig in &signals {
                            info!(
                                "  LOG_ONLY: {} | price={:.3} | bm_prob={:.3} | comp={:.1}%",
                                sig.market_title, sig.pm_price, sig.bookmaker_prob, sig.edge_pct
                            );
                        }
                    } else {
                        info!("executing {} odds signals", signals.len());
                        let aggregated = SignalAggregator::aggregate(&[], &signals);

                        for signal in &aggregated {
                            let mut risk_guard = risk.write().await;
                            match executor.execute(signal, &mut risk_guard, &logger).await {
                                Ok(_) => {}
                                Err(e) => warn!("arb execution error: {e}"),
                            }
                        }
                    }
                }
            }
            Err(e) => warn!("failed to fetch PM sports markets: {e}"),
        }

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
    }
}

fn pm_tag_from_odds_sport(odds_sport: &str) -> String {
    match odds_sport {
        "basketball_nba" => "nba".to_string(),
        "icehockey_nhl" => "nhl".to_string(),
        s if s.starts_with("soccer_") => "soccer".to_string(),
        s if s.starts_with("tennis_") => "tennis".to_string(),
        "mma_mixed_martial_arts" => "mma".to_string(),
        other => other.to_string(),
    }
}
