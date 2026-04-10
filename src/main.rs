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
mod rules;
mod scheduler;
mod signal;
mod signing;
mod sizing;
mod sports;
mod stability;
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
    let mut game_schedule = scheduler::GameSchedule::load_from_disk(std::path::Path::new("data/schedule_cache.json"));
    let mut watched_games: Vec<scheduler::WatchedGame> = Vec::new(); // continuously discovered, waiting for T-5
    let mut t5_executed: HashSet<String> = HashSet::new(); // event_slugs already bought at T-5
    let mut t1_executed: HashSet<String> = HashSet::new(); // event_slugs re-checked at T-1

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
                        .chain(t5_executed.iter().cloned())
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

                    // 3. T-5 Confirm+Buy: use pre-fetched positions (no extra API calls).
                    //    Window = 0..t10_minutes (explicit, no hidden cushion).
                    let t5_matches = scheduler::confirm_and_execute_t5(
                        &watched_games,
                        &watchlist,
                        schedule_cfg.t10_minutes,
                        &t5_executed,
                        &raw_positions,
                    );

                    // Always use latest config for sizing (hot-reload may have changed %)
                    wave_budget.update_config(config.read().await.sport_sizing.clone());

                    // Per-poll dedup: T-5 and T-1 windows overlap (e.g. 0..12 vs 0..3),
                    // so a game in the 0..3 zone could trigger both passes in the same
                    // poll cycle. Track every game T-5 *evaluated* this iteration so the
                    // T-1 pass skips it within the same poll. Reset every loop iteration —
                    // on the next poll, T-5 will skip via t5_executed and T-1 will get
                    // its own fresh shot at any newly-arrived Cannae positions.
                    let mut tried_this_poll: HashSet<String> = HashSet::new();

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
                        // Skip if this game was already successfully executed by another wallet
                        if t5_executed.contains(&t5_match.game_event_slug) {
                            continue;
                        }
                        // Mark as tried this poll cycle so the T-1 pass below skips it.
                        tried_this_poll.insert(t5_match.game_event_slug.clone());
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
                        let bought = execute_stable_game(
                            &game, &mut executor, &risk, &logger, &config, true,
                            &client, &game_schedule,
                        ).await;
                        if !bought.is_empty() {
                            // Mark as executed — don't retry with other wallets
                            t5_executed.insert(t5_match.game_event_slug.clone());
                            // Spawn T+5 verification task
                            spawn_t5_verify(
                                bought,
                                t5_match.game_event_slug.clone(),
                                t5_match.wallet_address.clone(),
                                client.clone(),
                                logger.clone(),
                            );
                            // Brief pause between orders to avoid CLOB 425 rate limit
                            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        }
                        // If not executed (league filter, no bets), let another wallet try
                    }

                    // 4. T-1 second pass: re-check games close to kickoff to catch late
                    //    Cannae trades that arrived after T-5 already fired. Uses fresh
                    //    raw_positions; condition-level dedup (attempted set) prevents
                    //    double-buying legs already filled at T-5.
                    //    Window = 0..t1_minutes (explicit, no hidden cushion — fires at
                    //    actual T-1, not T-3 like the old +2 cushion would have caused).
                    let t1_matches = scheduler::confirm_and_execute_t5(
                        &watched_games,
                        &watchlist,
                        schedule_cfg.t1_minutes,
                        &t1_executed,
                        &raw_positions,
                    );

                    // Same sort as T-5: biggest Cannae games first so they get priority
                    // when capital allocation is tight.
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
                        // Same-poll dedup: skip if T-5 already evaluated this game in
                        // this iteration. Next poll cycle resets tried_this_poll.
                        if tried_this_poll.contains(&t1_match.game_event_slug) {
                            continue;
                        }
                        if t1_executed.contains(&t1_match.game_event_slug) {
                            continue;
                        }
                        let game = stability::StableGame {
                            event_slug: t1_match.game_event_slug.clone(),
                            positions: t1_match.positions.clone(),
                            source_wallet: t1_match.wallet_address.clone(),
                            source_name: t1_match.wallet_name.clone(),
                        };
                        info!(
                            "T1 EXECUTE: {} from {} ({} positions, t5_bought={})",
                            game.event_slug, game.source_name,
                            t1_match.positions.len(),
                            t5_executed.contains(&t1_match.game_event_slug),
                        );
                        let bought = execute_stable_game(
                            &game, &mut executor, &risk, &logger, &config, true,
                            &client, &game_schedule,
                        ).await;
                        if !bought.is_empty() {
                            t1_executed.insert(t1_match.game_event_slug.clone());
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

                    // Cleanup: remove watched games that have already started (past due)
                    let now = chrono::Utc::now();
                    watched_games.retain(|g| g.start_time > now);
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
    let allowed_leagues = cfg.copy_trading.watchlist.iter()
        .find(|w| w.address.eq_ignore_ascii_case(&game.source_wallet))
        .map(|w| w.leagues.clone())
        .unwrap_or_default();
    drop(cfg);

    // League filter
    let league = game.event_slug.split('-').next().unwrap_or("");
    if !allowed_leagues.is_empty() && !allowed_leagues.iter().any(|l| l == league) {
        info!("GAME SKIP: {} — league '{}' not in {}'s allowed leagues", game.event_slug, league, game.source_name);
        return vec![];
    }

    // Which game lines are allowed for this sport?
    let allowed_lines = sport_sizing.allowed_game_lines(league);
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
    info!("T5 POSITIONS: {} — {} positions:", game.event_slug, game.positions.len());
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
            match crate::sizing::cannae_cv_multiplier(haupt_cv) {
                None => {
                    info!(
                        "GAME SKIP: {} — true hauptbet CV ${:.0} below $500 skip-floor (type {}, {} / {})",
                        game.event_slug,
                        haupt_cv,
                        haupt_type,
                        true_haupt.outcome.as_deref().unwrap_or("?"),
                        true_haupt.title.as_deref().unwrap_or("?"),
                    );
                    return vec![];
                }
                Some(m) => {
                    info!(
                        "LADDER: {} — hauptbet ${:.0} CV ({} {}) → multiplier {:.2}× (effective {:.2}%)",
                        game.event_slug,
                        haupt_cv,
                        haupt_type,
                        true_haupt.outcome.as_deref().unwrap_or("?"),
                        m,
                        2.5 * m,
                    );
                    m
                }
            }
        }
    };

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
    let is_football = !matches!(league, "nba" | "nhl" | "mlb" | "nfl" | "cbb" | "ncaa");

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
                            bets.push(GameLineBet { pos: (*dy).clone(), game_line: "draw".to_string(), size_pct: 2.5 * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: false });
                        }
                    }
                    // Set sizes: base 2.5% × Cannae CV ladder (2026-04-10)
                    for b in bets.iter_mut() {
                        if b.game_line == "win" {
                            b.size_pct = 2.5 * game_multiplier;
                        } else if b.game_line == "draw" {
                            b.size_pct = 2.5 * game_multiplier;
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
                        bets.push(GameLineBet { pos: opp_pos, game_line: "win".to_string(), size_pct: 2.5 * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: true });
                        info!("GAME MODE: {} → 3-LEG WIN_NO+DRAW_YES+OPP_NO ({:.2}×3 legs, draw/win ratio {:.1}%)", game.event_slug, 2.5 * game_multiplier, draw_yes_ratio*100.0);
                    } else {
                        info!("GAME MODE: {} → WIN_NO+DRAW_YES (opp NO not found, 2 legs only, ratio {:.1}%)", game.event_slug, draw_yes_ratio*100.0);
                    }
                } else {
                    // Win NO only (no draw, or draw < 5% dust)
                    bets.retain(|b| b.game_line != "draw");
                    if has_draw_yes {
                        info!("GAME MODE: {} → WIN_NO (draw_yes ratio {:.1}% < 5% dust, dropped)", game.event_slug, draw_yes_ratio*100.0);
                    } else {
                        info!("GAME MODE: {} → WIN_NO ({} bets)", game.event_slug, bets.len());
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
                                false
                            }
                        });
                        // Ensure draw NO is in bets
                        if !bets.iter().any(|b| b.game_line == "draw") {
                            let draw_no = draw_positions.iter()
                                .filter(|p| p.outcome.as_deref().unwrap_or("").eq_ignore_ascii_case("No"))
                                .max_by(|a, b| a.current_value_f64().partial_cmp(&b.current_value_f64()).unwrap_or(std::cmp::Ordering::Equal));
                            if let Some(dn) = draw_no {
                                bets.push(GameLineBet { pos: (*dn).clone(), game_line: "draw".to_string(), size_pct: 2.5 * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: false });
                            }
                        }
                        if draw_no_ratio >= 0.05 {
                            // 3-LEG: WIN_YES_A + DRAW_NO + WIN_NO_B — base 2.5% × ladder (2026-04-10)
                            for b in bets.iter_mut() {
                                if b.game_line == "win" {
                                    b.size_pct = 2.5 * game_multiplier;
                                    b.exempt_win_yes_ban = true;
                                } else if b.game_line == "draw" {
                                    b.size_pct = 2.5 * game_multiplier;
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
                                bets.push(GameLineBet { pos: opp_pos, game_line: "win".to_string(), size_pct: 2.5 * game_multiplier, fixed_size: true, exempt_win_yes_ban: false, synthetic: true });
                                info!("GAME MODE: {} → 3-LEG WIN_YES+DRAW_NO+OPP_NO ({:.2}%×3 legs, draw/win ratio {:.1}%, WIN_YES exempt)", game.event_slug, 2.5 * game_multiplier, draw_no_ratio*100.0);
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
            "  T5 PLAN: {} {} | {} | ${:.0} ({:.0}%) | Cannae cv=${:.0} iv=${:.0}",
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
