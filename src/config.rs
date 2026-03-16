use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use alloy_primitives::Address;
use anyhow::{Context, Result};
use serde::Deserialize;
use tokio::sync::RwLock;
use tracing::info;

// Contract addresses (Polygon mainnet)
pub const EXCHANGE_ADDRESS: &str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
pub const NEG_RISK_EXCHANGE_ADDRESS: &str = "0xC5d563A36AE78145C45a50134d48A1215220f80a";
pub const USDC_ADDRESS: &str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
pub const CTF_ADDRESS: &str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";

pub const CHAIN_ID: u64 = 137;
pub const CTF_DECIMALS: u32 = 6;
pub const CTF_DECIMAL_FACTOR: f64 = 1_000_000.0;
pub const ZERO_ADDRESS: &str = "0x0000000000000000000000000000000000000000";

// API endpoints
pub const CLOB_API: &str = "https://clob.polymarket.com";
pub const GAMMA_API: &str = "https://gamma-api.polymarket.com";
pub const DATA_API: &str = "https://data-api.polymarket.com";

// Order constraints
pub const MIN_ORDER_SHARES: f64 = 5.0;
pub const MIN_ORDER_VALUE: f64 = 1.0;
pub const TICK_SIZE: f64 = 0.01;

#[derive(Debug, Clone)]
pub struct BotConfig {
    pub private_key: String,
    pub funder: Address,
    pub signer: Address,
    pub api_key: String,
    pub api_secret: String,
    pub api_passphrase: String,
    pub neg_risk: bool,
    pub dry_run: bool,
    pub odds_api_key: String,
    pub telegram_bot_token: String,
    pub telegram_chat_id: String,
}

impl BotConfig {
    pub fn from_env() -> Result<Self> {
        dotenvy::dotenv().ok();

        let mut private_key = std::env::var("PRIVATE_KEY").context("PRIVATE_KEY not set")?;
        // Ensure 0x prefix for alloy signer
        if !private_key.starts_with("0x") {
            private_key = format!("0x{private_key}");
        }
        let wallet = std::env::var("WALLET_ADDRESS").context("WALLET_ADDRESS not set")?;
        let funder_str = std::env::var("FUNDER_ADDRESS").context("FUNDER_ADDRESS not set")?;

        let api_key = std::env::var("POLY_API_KEY").unwrap_or_default();
        let api_secret = std::env::var("POLY_API_SECRET").unwrap_or_default();
        let api_passphrase = std::env::var("POLY_PASSPHRASE").unwrap_or_default();

        let odds_api_key = std::env::var("ODDS_API_KEY").unwrap_or_default();
        let telegram_bot_token = std::env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
        let telegram_chat_id = std::env::var("TELEGRAM_CHAT_ID").unwrap_or_default();

        Ok(Self {
            private_key,
            signer: wallet.parse().context("invalid WALLET_ADDRESS")?,
            funder: funder_str.parse().context("invalid FUNDER_ADDRESS")?,
            api_key,
            api_secret,
            api_passphrase,
            neg_risk: true, // sports markets use neg-risk exchange
            dry_run: false,
            odds_api_key,
            telegram_bot_token,
            telegram_chat_id,
        })
    }

    pub fn exchange_address(&self) -> Address {
        let addr = if self.neg_risk {
            NEG_RISK_EXCHANGE_ADDRESS
        } else {
            EXCHANGE_ADDRESS
        };
        addr.parse().expect("invalid exchange address")
    }
}

// --- YAML Config (hot-reloadable) ---

#[derive(Debug, Clone, Deserialize)]
pub struct AppConfig {
    pub copy_trading: CopyTradingConfig,
    pub odds_arb: OddsArbConfig,
    pub sizing: SizingConfig,
    pub risk: RiskConfig,
    pub autoresearch: AutoresearchConfig,
    #[serde(default)]
    pub autoresearch_params: AutoresearchParams,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CopyTradingConfig {
    pub enabled: bool,
    pub poll_interval_seconds: u64,
    pub watchlist: Vec<WatchlistEntry>,
    pub consensus: ConsensusConfig,
    pub max_delay_seconds: u64,
    /// Max days until market resolution — skip markets that resolve further out (capital efficiency)
    #[serde(default = "default_max_resolution_days")]
    pub max_resolution_days: u32,
    /// Poll interval for warm-tier wallets (no recent signal). Hot wallets use poll_interval_seconds.
    #[serde(default = "default_warm_poll_interval")]
    pub warm_poll_interval_seconds: u64,
    /// Batch size for parallel wallet fetches (higher = faster but more API pressure)
    #[serde(default = "default_batch_size")]
    pub batch_size: usize,
}

fn default_warm_poll_interval() -> u64 {
    60
}

fn default_batch_size() -> usize {
    8
}

fn default_max_resolution_days() -> u32 {
    7 // default: skip markets resolving more than 7 days out
}

#[derive(Debug, Clone, Deserialize)]
pub struct WatchlistEntry {
    pub address: String,
    pub name: String,
    pub weight: f64,
    pub sports: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ConsensusConfig {
    pub min_traders: u32,
    /// Consensus window in minutes — bets older than this are pruned
    #[serde(default = "default_consensus_window")]
    pub window_minutes: u32,
    pub multiplier_2: f64,
    pub multiplier_3plus: f64,
}

fn default_consensus_window() -> u32 {
    30
}

#[derive(Debug, Clone, Deserialize)]
pub struct OddsArbConfig {
    pub enabled: bool,
    pub poll_interval_seconds: u64,
    pub base_url: String,
    pub sports: Vec<String>,
    pub min_edge_pct: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SizingConfig {
    pub kelly_fraction: f64,
    pub max_bet_pct: f64,
    pub copy_base_size_pct: f64,
    #[serde(default = "default_min_price")]
    pub min_price: f64,
}

fn default_min_price() -> f64 {
    0.05
}

#[derive(Debug, Clone, Deserialize)]
pub struct RiskConfig {
    pub max_daily_loss_pct: f64,
    pub min_bankroll: f64,
    pub max_open_bets: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AutoresearchConfig {
    pub interval_hours: u64,
    pub min_backtest_trades: u32,
    pub min_improvement_pct: f64,
    pub claude_model: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct AutoresearchParams {
    #[serde(default)]
    pub wallet_weights_override: HashMap<String, f64>,
    #[serde(default)]
    pub sport_multipliers: HashMap<String, f64>,
    #[serde(default)]
    pub timing_rules: Vec<String>,
    #[serde(default)]
    pub active_strategies: Vec<String>,
    /// Current strategy version tag for deployment attribution (Fix #7)
    #[serde(default)]
    pub current_strategy_version: Option<String>,
}

impl AppConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let contents = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read config: {}", path.display()))?;
        let config: Self =
            serde_yaml::from_str(&contents).context("failed to parse config.yaml")?;
        Ok(config)
    }
}

pub type SharedConfig = Arc<RwLock<AppConfig>>;

pub async fn watch_config(path: std::path::PathBuf, shared: SharedConfig) {
    use notify::{Event, EventKind, RecursiveMode, Watcher};
    use tokio::sync::mpsc;

    let (tx, mut rx) = mpsc::channel::<()>(1);

    let mut watcher = notify::recommended_watcher(move |res: Result<Event, notify::Error>| {
        if let Ok(event) = res {
            if matches!(event.kind, EventKind::Modify(_) | EventKind::Create(_)) {
                let _ = tx.try_send(());
            }
        }
    })
    .expect("failed to create file watcher");

    watcher
        .watch(&path, RecursiveMode::NonRecursive)
        .expect("failed to watch config file");

    // Keep watcher alive
    let _watcher = watcher;

    while rx.recv().await.is_some() {
        // Debounce
        tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
        // Drain any extra events
        while rx.try_recv().is_ok() {}

        match AppConfig::load(&path) {
            Ok(new_config) => {
                info!("config.yaml reloaded successfully");
                let mut w = shared.write().await;
                *w = new_config;
            }
            Err(e) => {
                tracing::warn!("failed to reload config.yaml: {e}");
            }
        }
    }
}
