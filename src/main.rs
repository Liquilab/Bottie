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
    let mut stability_tracker = stability::StabilityTracker::new();

    // Seed attempted map from live PM positions (source of truth).
    // Also seed stability tracker with events we already hold.
    {
        let funder = client.funder_address();
        match client.get_wallet_positions(&funder, 500).await {
            Ok(positions) => {
                executor.seed_from_positions(&positions);
                // RUS-266: also seed from trades.jsonl — PM API inconsistently returns positions
                executor.seed_from_trade_log(std::path::Path::new("data/trades.jsonl"));
                // Seed stability tracker: events we already hold should never re-enter
                let our_events = positions
                    .iter()
                    .filter(|p| p.size_f64() > 0.01)
                    .filter_map(|p| {
                        p.event_slug
                            .clone()
                            .or_else(|| p.slug.clone())
                            .map(|s| s.trim_end_matches("-more-markets").to_string())
                    })
                    .filter(|s| !s.is_empty());
                stability_tracker.seed_emitted(our_events);
            }
            Err(e) => tracing::warn!("could not seed from PM positions: {e}"),
        }
    }
    let mut polls_since_sync: u32 = 0;
    let mut polls_since_stability_log: u32 = 0;
    let mut emit_cooldown: HashMap<String, chrono::DateTime<chrono::Utc>> = HashMap::new();

    // T-30 schedule state
    let mut game_schedule = scheduler::GameSchedule::new();
    let mut t30_attempted: HashSet<String> = HashSet::new();

    // RUS-278: Pre-computed ROI cache — refreshed every 20 polls (~5 min), NOT per candidate.
    let mut roi_cache: HashMap<(String, String), f64> = HashMap::new();
    let mut polls_since_roi_refresh: u32 = 20; // Force immediate refresh on first cycle

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

        // Read stability config
        let (stability_window, stability_threshold) = {
            let c = config.read().await;
            (
                c.copy_trading.stability_window_minutes,
                c.copy_trading.stability_threshold_pct,
            )
        };

        // Poll for new copy signals + raw wallet positions
        let (_copy_signals, raw_positions) = copy_trader.poll().await;

        // Build our current event_slugs (from PM positions already seeded at startup
        // + any events the stability tracker has already emitted)
        // We use the executor's attempted set indirectly through seed_from_positions.
        // For stability: get our own current positions to know which events to skip.
        let our_event_slugs: std::collections::HashSet<String> = {
            let funder = client.funder_address();
            match client.get_wallet_positions(&funder, 500).await {
                Ok(positions) => positions
                    .iter()
                    .filter(|p| p.size_f64() > 0.01)
                    .filter_map(|p| {
                        p.event_slug
                            .clone()
                            .or_else(|| p.slug.clone())
                            .map(|s| s.trim_end_matches("-more-markets").to_string())
                    })
                    .filter(|s| !s.is_empty())
                    .collect(),
                Err(_) => std::collections::HashSet::new(),
            }
        };

        // Feed wallet positions into stability tracker (with per-wallet league filter)
        {
            let c = config.read().await;
            for (addr, name, positions) in &raw_positions {
                // Fail closed: skip wallets not in watchlist config
                let Some(wallet_cfg) = c.copy_trading.watchlist.iter()
                    .find(|w| w.address.eq_ignore_ascii_case(addr))
                else {
                    warn!("STABILITY SKIP: no watchlist config for source wallet {}", addr);
                    continue;
                };
                let wallet_leagues = wallet_cfg.leagues.clone();
                stability_tracker.update(
                    addr,
                    name,
                    positions,
                    &our_event_slugs,
                    stability_threshold,
                    &wallet_leagues,
                );
            }
        }

        // Collect ALL fetched positions for stability check (to get current data)
        let all_cannae_positions: Vec<crate::clob::types::WalletPosition> = raw_positions
            .iter()
            .flat_map(|(_, _, positions)| positions.clone())
            .collect();

        // Check for newly stable games — get candidates WITHOUT modifying state
        let candidates = stability_tracker.get_ready_candidates(
            stability_window,
            &all_cannae_positions,
        );

        // Prune stale entries periodically
        stability_tracker.prune_stale();

        // RUS-278: Process candidates — trades check first, THEN in-memory conflict resolution
        {
            // Phase 1: Filter candidates that are still actively trading.
            // Dedup: one trades check per wallet, not per candidate.
            let mut wallet_still_active: HashMap<String, bool> = HashMap::new();
            let mut ready_candidates: Vec<&stability::StableGame> = Vec::new();
            for game in &candidates {
                let active = match wallet_still_active.get(&game.source_wallet) {
                    Some(cached) => *cached,
                    None => {
                        let result = cannae_still_trading(&client, &game.source_wallet, game, 120).await;
                        wallet_still_active.insert(game.source_wallet.clone(), result);
                        result
                    }
                };
                if active {
                    continue;
                }
                ready_candidates.push(game);
            }

            if !ready_candidates.is_empty() {
                // Phase 2: Group by event_slug for conflict detection (pure in-memory)
                let mut by_event: HashMap<String, Vec<&stability::StableGame>> = HashMap::new();
                for game in &ready_candidates {
                    by_event.entry(game.event_slug.clone()).or_default().push(game);
                }

                let num_wallets = {
                    let cfg = config.read().await;
                    cfg.copy_trading.watchlist.len()
                };

                for (event_slug, wallet_games) in &by_event {
                    // Phase 2a: Emit cooldown — wait for other wallets (only if multi-wallet)
                    if num_wallets > 1 {
                        let now = chrono::Utc::now();
                        let first_ready = emit_cooldown.entry(event_slug.clone())
                            .or_insert(now);
                        let waited_secs = now.signed_duration_since(*first_ready).num_seconds();
                        if waited_secs < 30 {
                            continue;
                        }
                    }
                    emit_cooldown.remove(event_slug.as_str());

                    // Phase 2b: Pick winner — pure in-memory lookup from roi_cache
                    let winner = if wallet_games.len() == 1 {
                        wallet_games[0]
                    } else {
                        // Conflict! Pick highest ROI from cache
                        info!(
                            "CONFLICT: {} — {} wallets: [{}]",
                            event_slug,
                            wallet_games.len(),
                            wallet_games.iter()
                                .map(|g| g.source_name.as_str())
                                .collect::<Vec<_>>().join(", ")
                        );

                        let mut best: Option<&stability::StableGame> = None;
                        let mut best_roi = f64::NEG_INFINITY;

                        for game in wallet_games {
                            let mt = game.positions.iter()
                                .filter_map(|p| p.title.as_deref())
                                .map(|title| CopyTrader::detect_market_type(title))
                                .next()
                                .unwrap_or_else(|| "win".to_string());

                            let key = (game.source_name.to_lowercase(), mt.clone());
                            let roi = roi_cache.get(&key).copied().unwrap_or(f64::NEG_INFINITY);

                            let outcome = game.positions.first()
                                .and_then(|p| p.outcome.as_deref())
                                .unwrap_or("?");
                            info!(
                                "CONFLICT: {} bets {} on {} (ROI={:.1}%{})",
                                game.source_name, outcome, mt, roi,
                                if roi > 99.0 { " seed" } else { "" }
                            );

                            if roi > best_roi {
                                best_roi = roi;
                                best = Some(game);
                            }
                        }

                        match best {
                            Some(winner) => {
                                info!(
                                    "CONFLICT RESOLVED: {} wins (ROI={:.1}%)",
                                    winner.source_name, best_roi
                                );
                                winner
                            }
                            None => {
                                warn!(
                                    "CONFLICT FALLBACK: no ROI data for {} — taking {}",
                                    event_slug, wallet_games[0].source_name
                                );
                                wallet_games[0]
                            }
                        }
                    };

                    // Phase 3: Execute the winner
                    let wait_mins = stability_tracker.pending_summary()
                        .iter()
                        .find(|(s, _, _)| s == event_slug)
                        .map(|(_, m, _)| *m)
                        .unwrap_or(0);
                    info!(
                        "STABILITY EMIT: {} after {}min ({} quiet for 2h+)",
                        event_slug, wait_mins, winner.source_name
                    );

                    execute_stable_game(
                        winner, &mut executor, &risk, &logger, &config
                    ).await;
                    stability_tracker.confirm_emitted(event_slug);
                }
            }
        }

        // Log pending stability status periodically (every 20 polls = ~5 min)
        polls_since_stability_log += 1;
        if polls_since_stability_log >= 20 {
            polls_since_stability_log = 0;
            let pending = stability_tracker.pending_count();
            if pending > 0 {
                let summary = stability_tracker.pending_summary();
                for (slug, wait_mins, is_stable) in &summary {
                    let state = if *is_stable { "stable" } else { "filling" };
                    info!("STABILITY PENDING: {} ({}min, {})", slug, wait_mins, state);
                }
            }
        }

        // --- T-30 schedule-based copy trading ---
        {
            let schedule_cfg = config.read().await.schedule.clone();
            if schedule_cfg.enabled && !schedule_cfg.sport_tags.is_empty() {
                // Refresh schedule if stale
                if game_schedule.needs_refresh(schedule_cfg.refresh_interval_minutes as i64) {
                    scheduler::refresh_schedule(
                        &client,
                        &schedule_cfg.sport_tags,
                        &mut game_schedule,
                    ).await;
                }

                // Check for T-30 matches
                let watchlist = config.read().await.copy_trading.watchlist.clone();
                let matches = scheduler::check_t30_games(
                    &client,
                    &game_schedule,
                    &watchlist,
                    schedule_cfg.t_minus_minutes as i64,
                    &mut t30_attempted,
                ).await;

                for t30_match in &matches {
                    let game = stability::StableGame {
                        event_slug: t30_match.game_event_slug.clone(),
                        positions: t30_match.positions.clone(),
                        source_wallet: t30_match.wallet_address.clone(),
                        source_name: t30_match.wallet_name.clone(),
                    };
                    info!(
                        "T30 EXECUTE: {} from {} ({} positions)",
                        game.event_slug, game.source_name, game.positions.len()
                    );
                    execute_stable_game_taker(
                        &game, &mut executor, &risk, &logger, &config, schedule_cfg.taker_mode,
                    ).await;
                }
            }
        }

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
    }
}

/// Check if Cannae has recent trades on any conditionId in this event.
/// Returns true if Cannae is still actively trading this event.
async fn cannae_still_trading(
    client: &ClobClient,
    cannae_address: &str,
    game: &stability::StableGame,
    cooldown_minutes: u64,
) -> bool {
    let since = chrono::Utc::now()
        .checked_sub_signed(chrono::Duration::minutes(cooldown_minutes as i64))
        .unwrap_or_else(chrono::Utc::now);
    let since_unix = since.timestamp();

    let trades = match client.get_trades_since(cannae_address, since_unix, 10).await {
        Ok(t) => t,
        Err(e) => {
            warn!("trades check failed for {}: {} — allowing emit", game.event_slug, e);
            return false; // On error: allow emit (don't block on API failure)
        }
    };

    let event_cids: std::collections::HashSet<String> = game.positions.iter()
        .filter_map(|p| p.condition_id.clone())
        .collect();

    for trade in &trades {
        if let Some(cid) = &trade.condition_id {
            if event_cids.contains(cid) {
                let age_mins = trade.timestamp_secs()
                    .map(|ts| (chrono::Utc::now().timestamp() - ts) / 60)
                    .unwrap_or(0);
                info!(
                    "STABILITY WAIT: {} — Cannae traded {}min ago on cid={}...",
                    game.event_slug, age_mins, &cid[..12.min(cid.len())]
                );
                return true;
            }
        }
    }

    false
}

/// Build AggregatedSignals from a stable game's Cannae positions,
/// select legs based on config (max_legs_per_event), and execute.
async fn execute_stable_game(
    game: &stability::StableGame,
    executor: &mut Executor,
    risk: &Arc<RwLock<RiskManager>>,
    logger: &Arc<TradeLogger>,
    config: &SharedConfig,
) {
    use crate::copy_trader::CopyTrader;

    let cfg = config.read().await;
    // Fail closed: skip games sourced from wallets not in watchlist config
    let Some(wallet_cfg) = cfg.copy_trading.watchlist.iter()
        .find(|w| w.address.eq_ignore_ascii_case(&game.source_wallet))
    else {
        warn!(
            "EXECUTE SKIP: no watchlist config for source wallet {}",
            game.source_wallet
        );
        return;
    };
    let max_legs = wallet_cfg.max_legs_per_event;
    let allowed_leagues = wallet_cfg.leagues.clone();
    let allowed_market_types = wallet_cfg.market_types.clone();
    drop(cfg);

    // League filter: event_slug format is "{league}-{teams}-{date}"
    let league_prefix = game.event_slug.split('-').next().unwrap_or("");
    if !allowed_leagues.is_empty() && !allowed_leagues.iter().any(|l| l == league_prefix) {
        info!(
            "STABILITY SKIP: {} not in allowed leagues (prefix={}, allowed={:?})",
            game.event_slug, league_prefix, allowed_leagues
        );
        return;
    }

    // Group positions by conditionId, keep largest per condition
    let mut best_per_condition: std::collections::HashMap<
        String,
        &crate::clob::types::WalletPosition,
    > = std::collections::HashMap::new();

    for pos in &game.positions {
        let cid = pos.condition_id.as_deref().unwrap_or("");
        if cid.is_empty() {
            continue;
        }
        let existing = best_per_condition.get(cid);
        // RUS-260 Fix B: Compare on $ value (initial_value), not shares.
        // Cheap legs have more shares per dollar, biasing toward them.
        if existing.is_none() || pos.initial_value_f64() > existing.unwrap().initial_value_f64() {
            best_per_condition.insert(cid.to_string(), pos);
        }
    }

    // Hauptbet selection: best per condition, filtered by allowed market types
    let mut game_legs: Vec<&crate::clob::types::WalletPosition> = best_per_condition
        .values()
        .copied()
        .filter(|pos| {
            if allowed_market_types.is_empty() {
                return true;
            }
            let title = pos.title.as_deref().unwrap_or("");
            let detected = CopyTrader::detect_market_type(title);
            let allowed = allowed_market_types.iter().any(|mt| mt == &detected);
            if !allowed {
                info!(
                    "STABILITY FILTER: skipping leg '{}' (type={}, allowed={:?})",
                    &title[..title.len().min(60)],
                    detected,
                    allowed_market_types
                );
            }
            allowed
        })
        .collect();

    // Sort by USDC size descending, apply max_legs limit
    game_legs.sort_by(|a, b| {
        b.initial_value_f64()
            .partial_cmp(&a.initial_value_f64())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    if max_legs > 0 {
        game_legs.truncate(max_legs);
    }

    // Need at least 1 leg
    if game_legs.is_empty() {
        info!(
            "STABILITY SKIP: {} has no legs after conditionId grouping",
            game.event_slug,
        );
        return;
    }

    // Cannae's total USDC for SELECTED legs only (not all positions).
    // We size based on what we actually bet on — if we only take the hauptbet,
    // the full game_budget goes to that single leg.
    let cannae_selected_total: f64 = game_legs.iter().map(|p| p.initial_value_f64()).sum();
    if cannae_selected_total <= 0.0 {
        return;
    }

    info!(
        "STABILITY EXECUTE: {} — {} legs, Cannae selected ${:.0}",
        game.event_slug,
        game_legs.len(),
        cannae_selected_total
    );

    // Build AggregatedSignal for each leg and execute
    for pos in &game_legs {
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

        // Market-anchored confidence: assume 10% edge over market
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

        let mut risk_guard = risk.write().await;
        match executor
            .execute_with_game_context(&agg_signal, &mut risk_guard, &logger, cannae_selected_total)
            .await
        {
            Ok(filled) => {
                if filled {
                    info!("STABILITY FILLED: {}", agg_signal.market_title);
                }
            }
            Err(e) => {
                warn!("STABILITY ERROR: {}: {e}", agg_signal.market_title);
            }
        }
    }
}

/// T-30 variant of execute_stable_game that supports taker mode.
/// Delegates to execute_stable_game logic but uses execute_with_game_context_taker.
async fn execute_stable_game_taker(
    game: &stability::StableGame,
    executor: &mut Executor,
    risk: &Arc<RwLock<RiskManager>>,
    logger: &Arc<TradeLogger>,
    config: &SharedConfig,
    taker_mode: bool,
) {
    use crate::copy_trader::CopyTrader;

    let cfg = config.read().await;
    let Some(wallet_cfg) = cfg.copy_trading.watchlist.iter()
        .find(|w| w.address.eq_ignore_ascii_case(&game.source_wallet))
    else {
        warn!(
            "T30 SKIP: no watchlist config for source wallet {}",
            game.source_wallet
        );
        return;
    };
    let max_legs = wallet_cfg.max_legs_per_event;
    let allowed_leagues = wallet_cfg.leagues.clone();
    let allowed_market_types = wallet_cfg.market_types.clone();
    drop(cfg);

    // League filter
    let league_prefix = game.event_slug.split('-').next().unwrap_or("");
    if !allowed_leagues.is_empty() && !allowed_leagues.iter().any(|l| l == league_prefix) {
        info!(
            "T30 SKIP: {} not in allowed leagues (prefix={}, allowed={:?})",
            game.event_slug, league_prefix, allowed_leagues
        );
        return;
    }

    // Group positions by conditionId, keep largest per condition
    let mut best_per_condition: std::collections::HashMap<
        String,
        &crate::clob::types::WalletPosition,
    > = std::collections::HashMap::new();

    for pos in &game.positions {
        let cid = pos.condition_id.as_deref().unwrap_or("");
        if cid.is_empty() {
            continue;
        }
        let existing = best_per_condition.get(cid);
        if existing.is_none() || pos.initial_value_f64() > existing.unwrap().initial_value_f64() {
            best_per_condition.insert(cid.to_string(), pos);
        }
    }

    // Hauptbet selection with market type filter
    let mut game_legs: Vec<&crate::clob::types::WalletPosition> = best_per_condition
        .values()
        .copied()
        .filter(|pos| {
            if allowed_market_types.is_empty() {
                return true;
            }
            let title = pos.title.as_deref().unwrap_or("");
            let detected = CopyTrader::detect_market_type(title);
            allowed_market_types.iter().any(|mt| mt == &detected)
        })
        .collect();

    game_legs.sort_by(|a, b| {
        b.initial_value_f64()
            .partial_cmp(&a.initial_value_f64())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    if max_legs > 0 {
        game_legs.truncate(max_legs);
    }

    if game_legs.is_empty() {
        info!("T30 SKIP: {} has no legs after filtering", game.event_slug);
        return;
    }

    let cannae_selected_total: f64 = game_legs.iter().map(|p| p.initial_value_f64()).sum();
    if cannae_selected_total <= 0.0 {
        return;
    }

    info!(
        "T30 EXECUTE: {} — {} legs, source ${:.0}, taker={}",
        game.event_slug, game_legs.len(), cannae_selected_total, taker_mode
    );

    for pos in &game_legs {
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

        let mut risk_guard = risk.write().await;
        match executor
            .execute_with_game_context_taker(
                &agg_signal, &mut risk_guard, &logger, cannae_selected_total, taker_mode,
            )
            .await
        {
            Ok(filled) => {
                if filled {
                    info!("T30 FILLED: {}", agg_signal.market_title);
                }
            }
            Err(e) => {
                warn!("T30 ERROR: {}: {e}", agg_signal.market_title);
            }
        }
    }
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
