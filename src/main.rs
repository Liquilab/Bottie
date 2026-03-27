mod budget;
mod clob;
mod config;
mod copy_trader;
mod execution;
mod logger;
mod odds;
mod portfolio;
mod resolver;
mod risk;
mod scheduler;
mod signal;
mod signing;
mod sizing;
mod sports;
mod stability;
mod sync;
mod wallet_tracker;
mod watchlist_refresh;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use alloy_signer_local::PrivateKeySigner;
use anyhow::{Context, Result};
use clap::Parser;
use tokio::sync::RwLock;
use std::collections::{HashMap, HashSet};
use tracing::{error, info, warn};

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
    let mut game_schedule = scheduler::GameSchedule::new();
    let mut watched_games: Vec<scheduler::WatchedGame> = Vec::new(); // continuously discovered, waiting for T-5
    let mut t5_executed: HashSet<String> = HashSet::new(); // event_slugs already bought

    // RUS-278: Pre-computed ROI cache — refreshed every 20 polls (~5 min), NOT per candidate.
    let mut roi_cache: HashMap<(String, String), f64> = HashMap::new();
    let mut polls_since_roi_refresh: u32 = 20; // Force immediate refresh on first cycle

    // Cannae position summary: log every ~30 min (120 polls × 15s = 30 min)
    let cannae_summary_every_n: u32 = 120;
    let mut polls_since_cannae_summary: u32 = cannae_summary_every_n; // Force immediate on first cycle

    loop {
        let poll_interval = {
            let c = config.read().await;
            if !c.copy_trading.enabled {
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

        // RUS-278: Refresh ROI cache periodically (every 20 polls = ~5 min)
        polls_since_roi_refresh += 1;
        if polls_since_roi_refresh >= 20 {
            polls_since_roi_refresh = 0;
            let cfg = config.read().await;
            let conflict_cfg = &cfg.copy_trading.conflict_resolution;
            let min_trades = conflict_cfg.min_trades_for_live_roi;

            let live_roi = logger.compute_wallet_roi();

            // Canonicalize market type names: logger uses "total"/"moneyline",
            // CopyTrader uses "ou"/"win". Normalize to CopyTrader names.
            let canonical_mt = |mt: &str| -> String {
                match mt {
                    "total" => "ou".to_string(),
                    "moneyline" => "win".to_string(),
                    other => other.to_string(),
                }
            };

            roi_cache.clear();
            for ((wallet, mt), (roi, count)) in &live_roi {
                if *count >= min_trades {
                    roi_cache.insert((wallet.to_lowercase(), canonical_mt(mt)), *roi);
                }
            }

            // Fill in seed rankings for wallets without enough live data
            for wallet_entry in &cfg.copy_trading.watchlist {
                let name = wallet_entry.name.to_lowercase();
                for mt in &["spread", "ou", "win", "ml", "draw"] {
                    let key = (name.clone(), mt.to_string());
                    if !roi_cache.contains_key(&key) {
                        if let Some(rank) = conflict_cfg.seed_rank(&wallet_entry.name, mt) {
                            roi_cache.insert(key, 100.0 - rank as f64);
                        }
                    }
                }
            }

            drop(cfg);
            info!(
                "ROI CACHE: refreshed — {} entries ({} from live data)",
                roi_cache.len(),
                live_roi.values().filter(|(_, count)| *count >= 20).count()
            );
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

            // Collect Cannae positions (first wallet = Cannae)
            let cannae_positions: Vec<_> = raw_positions
                .iter()
                .filter(|(_, name, _)| name.eq_ignore_ascii_case("Cannae"))
                .flat_map(|(_, _, positions)| positions.clone())
                .collect();

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
                        .chain(t5_executed.iter().cloned())
                        .collect();

                    let new_watched = scheduler::discover_continuous(
                        &client,
                        &game_schedule,
                        &watchlist,
                        schedule_cfg.t_minus_minutes,
                        &already_watched,
                    ).await;

                    if !new_watched.is_empty() {
                        info!(
                            "DISCOVERED: {} new games to watch",
                            new_watched.len()
                        );
                        watched_games.extend(new_watched);
                    }

                    // 3. T-5 Confirm+Buy: for watched games starting soon, re-fetch, compare, execute
                    let t5_matches = scheduler::confirm_and_execute_t5(
                        &client,
                        &watched_games,
                        &watchlist,
                        schedule_cfg.t5_minutes,
                        &t5_executed,
                    ).await;

                    // Sort T5 matches by Cannae game total DESC (biggest games first)
                    let mut t5_sorted = t5_matches;
                    t5_sorted.sort_by(|a, b| {
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

                    for t5_match in &t5_sorted {
                        let game = stability::StableGame {
                            event_slug: t5_match.game_event_slug.clone(),
                            positions: t5_match.positions.clone(),
                            source_wallet: t5_match.wallet_address.clone(),
                            source_name: t5_match.wallet_name.clone(),
                        };
                        info!(
                            "T5 EXECUTE: {} from {} ({} positions, discovery had {})",
                            game.event_slug, game.source_name,
                            t5_match.positions.len(), t5_match.t30_position_count,
                        );
                        execute_stable_game(
                            &game, &mut executor, &risk, &logger, &config, true,
                            &wave_budget,
                        ).await;
                        // Always mark as executed — never retry. If the order failed,
                        // retrying every 15 seconds spams the CLOB API and wastes fees.
                        t5_executed.insert(t5_match.game_event_slug.clone());
                    }

                    // Cleanup: remove watched games that have already started (past due)
                    let now = chrono::Utc::now();
                    watched_games.retain(|g| g.start_time > now);
                }
            }
        }

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
    }
}

/// Per game: detect hauptbet per game line, flat size, execute.
///
/// For each game line (moneyline/draw/spread) allowed by sport config:
/// 1. Find the hauptbet (largest USDC position per conditionId of that type)
/// 2. Size: flat % from wave budget (bankroll × pct / price)
/// 3. Execute via FOK on ask
async fn execute_stable_game(
    game: &stability::StableGame,
    executor: &mut Executor,
    risk: &Arc<RwLock<RiskManager>>,
    logger: &Arc<TradeLogger>,
    config: &SharedConfig,
    taker_mode: bool,
    wave_budget: &budget::WaveBudget,
) -> bool {
    use crate::copy_trader::CopyTrader;

    let cfg = config.read().await;
    let sport_sizing = cfg.sport_sizing.clone();
    let allowed_leagues = cfg.copy_trading.watchlist.iter()
        .find(|w| w.address.eq_ignore_ascii_case(&game.source_wallet))
        .map(|w| w.leagues.clone())
        .unwrap_or_default();
    drop(cfg);

    // League filter
    let league = game.event_slug.split('-').next().unwrap_or("");
    if !allowed_leagues.is_empty() && !allowed_leagues.iter().any(|l| l == league) {
        return false;
    }

    // Which game lines are allowed for this sport?
    let allowed_lines = sport_sizing.allowed_game_lines(league);
    if allowed_lines.is_empty() {
        info!("GAME SKIP: {} — no allowed game lines for league {}", game.event_slug, league);
        return false;
    }

    // Group all positions by conditionId, pick hauptbet (largest USDC) per conditionId
    let mut best_per_cid: std::collections::HashMap<
        String,
        &crate::clob::types::WalletPosition,
    > = std::collections::HashMap::new();

    for pos in &game.positions {
        let cid = pos.condition_id.as_deref().unwrap_or("");
        if cid.is_empty() { continue; }
        let entry = best_per_cid.entry(cid.to_string()).or_insert(pos);
        if pos.initial_value_f64() > entry.initial_value_f64() {
            *entry = pos;
        }
    }

    // Per allowed game line: find the hauptbet (largest position of that type across all cids)
    struct GameLineBet<'a> {
        pos: &'a crate::clob::types::WalletPosition,
        game_line: String,
        size_pct: f64,
    }

    let bankroll = risk.read().await.bankroll();
    let mut bets: Vec<GameLineBet> = Vec::new();

    for &gl in &allowed_lines {
        // Find the largest hauptbet of this game line type
        let mut best_for_line: Option<&crate::clob::types::WalletPosition> = None;
        let mut best_usdc = 0.0_f64;

        for pos in best_per_cid.values() {
            let title = pos.title.as_deref().unwrap_or("");
            let detected = CopyTrader::detect_market_type(title);
            if detected != gl { continue; }

            let usdc = pos.initial_value_f64();
            if usdc > best_usdc {
                best_usdc = usdc;
                best_for_line = Some(pos);
            }
        }

        if let Some(pos) = best_for_line {
            let pct = wave_budget.get_line_pct(bankroll, league, gl);
            if pct > 0.0 {
                bets.push(GameLineBet {
                    pos,
                    game_line: gl.to_string(),
                    size_pct: pct,
                });
            }
        }
    }

    if bets.is_empty() {
        info!("GAME SKIP: {} — no bets after filtering/budget", game.event_slug);
        return false;
    }

    // Log T5 PLAN
    info!(
        "GAME EXECUTE: {} ({}) — {} game lines",
        game.event_slug, league, bets.len(),
    );
    for bet in &bets {
        let title = bet.pos.title.as_deref().unwrap_or("");
        let outcome = bet.pos.outcome.as_deref().unwrap_or("");
        let usdc = bankroll * bet.size_pct / 100.0;
        info!(
            "  T5 PLAN: {} {} | {} | ${:.0} ({:.0}%) | Cannae ${:.0}",
            outcome, bet.game_line, &title[..title.len().min(50)],
            usdc, bet.size_pct, bet.pos.initial_value_f64(),
        );
    }

    // Execute each game line bet
    let mut any_filled = false;
    for bet in &bets {
        let pos = bet.pos;
        let title = pos.title.as_deref().unwrap_or("").to_string();
        let condition_id = pos.condition_id.as_deref().unwrap_or("").to_string();
        let outcome = pos.outcome.as_deref().unwrap_or("").to_string();
        let token_id = pos.asset.as_deref().unwrap_or("").to_string();
        let event_slug = pos
            .event_slug
            .clone()
            .or_else(|| pos.slug.clone())
            .unwrap_or_default()
            .trim_end_matches("-more-markets")
            .to_string();
        let price = pos.avg_price_f64();
        let sport = CopyTrader::detect_sport_static(&title, &event_slug);
        let market_type = CopyTrader::detect_market_type(&title);
        let source_size_usdc = pos.initial_value_f64();
        let source_shares = pos.size_f64();
        let confidence = (price * 1.10).min(0.95);

        let agg_signal = signal::AggregatedSignal {
            token_id,
            condition_id,
            side: "BUY".to_string(),
            price,
            market_title: title.clone(),
            market_type,
            sport,
            outcome,
            event_slug,
            sources: vec![signal::SignalSource::Copy(signal::CopySignal {
                source_wallet: game.source_wallet.clone(),
                source_name: game.source_name.clone(),
                token_id: pos.asset.as_deref().unwrap_or("").to_string(),
                condition_id: pos.condition_id.as_deref().unwrap_or("").to_string(),
                side: "BUY".to_string(),
                price,
                size: source_size_usdc,
                market_title: title,
                sport: CopyTrader::detect_sport_static(
                    pos.title.as_deref().unwrap_or(""),
                    &pos.event_slug.as_deref().unwrap_or(""),
                ),
                outcome: pos.outcome.as_deref().unwrap_or("").to_string(),
                event_slug: pos
                    .event_slug
                    .clone()
                    .unwrap_or_default()
                    .trim_end_matches("-more-markets")
                    .to_string(),
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
                    any_filled = true;
                }
            }
            Err(e) => {
                warn!("{} ERROR: {}: {e}", label, agg_signal.market_title);
            }
        }
    }
    any_filled
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
                let arb_signals =
                    odds::find_arb_opportunities(&all_odds, &pm_markets, odds_config.min_edge_pct);

                if !arb_signals.is_empty() {
                    info!("found {} arb opportunities", arb_signals.len());
                    let aggregated = SignalAggregator::aggregate(&[], &arb_signals);

                    for signal in &aggregated {
                        let mut risk_guard = risk.write().await;
                        match executor.execute(signal, &mut risk_guard, &logger).await {
                            Ok(_) => {}
                            Err(e) => warn!("arb execution error: {e}"),
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
        "soccer_epl"
        | "soccer_uefa_champs_league"
        | "soccer_uefa_europa_league" => "soccer".to_string(),
        s if s.starts_with("tennis_") => "tennis".to_string(),
        "mma_mixed_martial_arts" => "mma".to_string(),
        other => other.to_string(),
    }
}
