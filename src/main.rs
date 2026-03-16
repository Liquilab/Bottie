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
        tokio::spawn(async move {
            // Wait 60s for the bot to place initial trades before first check
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            resolver::resolver_loop(client, logger, risk, tracker).await;
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
    let mut polls_since_sync: u32 = 0;

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
            // Recount actual open bets from trade log (fixes drift from manual UI sells)
            let actual_open = logger.load_all().iter()
                .filter(|t| t.filled && t.result.is_none() && !t.dry_run)
                .count() as u32;
            {
                let mut r = risk.write().await;
                r.sync_open_bets(actual_open);
            }
        }

        // Poll for new copy signals
        let copy_signals = copy_trader.poll().await;

        if !copy_signals.is_empty() {
            // Fix #1: Enforce min_consensus at runtime
            let min_traders = {
                config.read().await.copy_trading.consensus.min_traders
            };

            let mut filtered_signals: Vec<_> = copy_signals
                .into_iter()
                .filter(|s| {
                    if s.consensus_count < min_traders {
                        info!(
                            "SKIP: consensus {} < min {} for {}",
                            s.consensus_count, min_traders, s.market_title
                        );
                        false
                    } else {
                        true
                    }
                })
                .collect::<Vec<_>>();

            // Sort by consensus count descending, then soonest resolve date first.
            // Extract date from title (e.g. "2026-03-16") for sorting.
            fn extract_date(title: &str) -> String {
                if let Some(pos) = title.find("2026-") {
                    title[pos..pos+10.min(title.len())].to_string()
                } else {
                    "9999-99-99".to_string()
                }
            }
            filtered_signals.sort_by(|a, b| {
                b.consensus_count.cmp(&a.consensus_count)
                    .then_with(|| extract_date(&a.market_title).cmp(&extract_date(&b.market_title)))
            });

            // Aggregate (no arb signals in this loop)
            let aggregated = SignalAggregator::aggregate(&filtered_signals, &[]);

            for signal in &aggregated {
                let mut risk_guard = risk.write().await;
                match executor.execute(signal, &mut risk_guard, &logger).await {
                    Ok(filled) => {
                        if filled {
                            info!("trade executed: {}", signal.market_title);
                        }
                    }
                    Err(e) => {
                        warn!("execution error: {e}");
                    }
                }
            }
        }

        tokio::time::sleep(Duration::from_secs(poll_interval)).await;
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
