mod clob;
mod config;
mod copy_trader;
mod execution;
mod logger;
mod odds;
mod portfolio;
mod resolver;
mod risk;
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

        // Feed Cannae positions into stability tracker
        for (addr, name, positions) in &raw_positions {
            stability_tracker.update(
                addr,
                name,
                positions,
                &our_event_slugs,
                stability_threshold,
            );
        }

        // Collect ALL fetched positions for drain_stable (to get current data)
        let all_cannae_positions: Vec<crate::clob::types::WalletPosition> = raw_positions
            .iter()
            .flat_map(|(_, _, positions)| positions.clone())
            .collect();

        // Check for newly stable games
        let stable_games = stability_tracker.drain_stable(
            stability_window,
            &all_cannae_positions,
        );

        // Process stable games: build signals, apply 3-leg filter, execute
        for game in &stable_games {
            execute_stable_game(
                &game,
                &mut executor,
                &risk,
                &logger,
                &config,
            )
            .await;
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

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
    }
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
    let max_legs = cfg.copy_trading.watchlist.first()
        .map(|w| w.max_legs_per_event)
        .unwrap_or(0);
    let allowed_market_types = cfg.copy_trading.watchlist.first()
        .map(|w| w.market_types.clone())
        .unwrap_or_default();
    drop(cfg);

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
        if existing.is_none() || pos.size_f64() > existing.unwrap().size_f64() {
            best_per_condition.insert(cid.to_string(), pos);
        }
    }

    // Filter by allowed market types and collect legs
    let mut game_legs: Vec<&crate::clob::types::WalletPosition> = Vec::new();
    for pos in best_per_condition.values() {
        let title = pos.title.as_deref().unwrap_or("");
        let market_type = CopyTrader::detect_market_type(title);
        if !allowed_market_types.is_empty()
            && !allowed_market_types.iter().any(|mt| mt == &market_type)
        {
            continue;
        }
        game_legs.push(pos);
    }

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
            "STABILITY SKIP: {} has no matching legs",
            game.event_slug,
        );
        return;
    }

    // Calculate Cannae's total USDC for ALL positions in this game
    let cannae_game_total: f64 = game.positions.iter().map(|p| p.initial_value_f64()).sum();
    if cannae_game_total <= 0.0 {
        return;
    }

    info!(
        "STABILITY EXECUTE: {} — {} legs, Cannae total ${:.0}",
        game.event_slug,
        game_legs.len(),
        cannae_game_total
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
            .execute_with_game_context(&agg_signal, &mut risk_guard, &logger, cannae_game_total)
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
