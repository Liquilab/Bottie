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
pub const CTF_DECIMAL_FACTOR: f64 = 1_000_000.0;
pub const ZERO_ADDRESS: &str = "0x0000000000000000000000000000000000000000";

// API endpoints
pub const CLOB_API: &str = "https://clob.polymarket.com";
pub const GAMMA_API: &str = "https://gamma-api.polymarket.com";
pub const DATA_API: &str = "https://data-api.polymarket.com";

// Order constraints
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
    #[serde(default)]
    pub auto_sell: AutoSellConfig,
    #[serde(default)]
    pub autoresearch_params: AutoresearchParams,
    #[serde(default)]
    pub schedule: ScheduleConfig,
    #[serde(default)]
    pub sport_sizing: SportSizingConfig,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct ScheduleConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_t_minus")]
    pub t_minus_minutes: u32,
    #[serde(default = "default_t5")]
    pub t5_minutes: u32,
    #[serde(default = "default_t1")]
    pub t1_minutes: u32,
    #[serde(default = "default_refresh_interval")]
    pub refresh_interval_minutes: u32,
    #[serde(default)]
    pub taker_mode: bool,
    #[serde(default)]
    pub sport_tags: Vec<String>,
}

fn default_t_minus() -> u32 {
    240
}

fn default_t5() -> u32 {
    5
}

fn default_t1() -> u32 {
    1
}

fn default_refresh_interval() -> u32 {
    60
}

#[derive(Debug, Clone, Deserialize)]
pub struct AutoSellConfig {
    #[serde(default = "auto_sell_default_enabled")]
    pub enabled: bool,
    #[serde(default = "auto_sell_default_min_bid")]
    pub min_bid: f64,
}

impl Default for AutoSellConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            min_bid: 0.98,
        }
    }
}

fn auto_sell_default_enabled() -> bool { false }
fn auto_sell_default_min_bid() -> f64 { 0.98 }

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
    /// Wait for Cannae's GTC orders to fill before copying.
    /// When enabled, positions must be stable (< threshold% change) for
    /// window_minutes before the bot copies them.
    #[serde(default = "default_stability_window")]
    pub stability_window_minutes: u32,
    /// Maximum % change in shares per leg to consider positions stable
    #[serde(default = "default_stability_threshold")]
    pub stability_threshold_pct: f64,
    /// Conflict resolution: when multiple wallets signal the same event,
    /// pick the best wallet based on live ROI or seed ranking.
    #[serde(default)]
    pub conflict_resolution: ConflictResolutionConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ConflictResolutionConfig {
    #[serde(default)]
    pub spread: Vec<String>,
    #[serde(default)]
    pub ou: Vec<String>,
    #[serde(default)]
    pub win: Vec<String>,
    #[serde(default)]
    pub ml: Vec<String>,
    #[serde(default)]
    pub draw: Vec<String>,
    #[serde(default = "default_min_trades_for_live_roi")]
    pub min_trades_for_live_roi: u32,
}

impl Default for ConflictResolutionConfig {
    fn default() -> Self {
        Self {
            spread: vec![],
            ou: vec![],
            win: vec![],
            ml: vec![],
            draw: vec![],
            min_trades_for_live_roi: 20,
        }
    }
}

fn default_min_trades_for_live_roi() -> u32 {
    20
}

impl ConflictResolutionConfig {
    /// Return the seed rank (0-based, lower = better) for a wallet name within a market type.
    /// Maps both CopyTrader types ("ou", "spread", "win", "draw", "ml", "btts", "other")
    /// and logger types ("total", "moneyline") to the correct seed list.
    /// Returns None if the wallet is not in the seed list for that type.
    pub fn seed_rank(&self, wallet_name: &str, market_type: &str) -> Option<usize> {
        let list = match market_type {
            "spread" => &self.spread,
            "ou" | "total" => &self.ou,
            "win" | "moneyline" => &self.win,
            "ml" => &self.ml,
            "draw" => &self.draw,
            // btts, other → no seed list
            _ => return None,
        };
        list.iter().position(|n| n == wallet_name)
    }
}

fn default_warm_poll_interval() -> u64 {
    60
}

fn default_batch_size() -> usize {
    8
}

fn default_stability_window() -> u32 {
    30 // 30 minutes: wait for Cannae's GTC orders to fill
}

fn default_stability_threshold() -> f64 {
    5.0 // 5% change threshold
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
    /// Allowed market types for this wallet (e.g. ["win", "ou", "spread", "ml", "draw"]).
    /// Empty = all types allowed.
    #[serde(default)]
    pub market_types: Vec<String>,
    /// Allowed league prefixes from event_slug (e.g. ["epl", "bun", "lal"]).
    /// Empty = all leagues allowed.
    #[serde(default)]
    pub leagues: Vec<String>,
    /// Max legs per event to copy. 0 = unlimited.
    #[serde(default)]
    pub max_legs_per_event: usize,
    /// Per-wallet min price override (falls back to global sizing.min_price)
    #[serde(default)]
    pub min_price: Option<f64>,
    /// Per-wallet max price override (falls back to global sizing.max_price)
    #[serde(default)]
    pub max_price: Option<f64>,
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
    /// "arb" (default) or "close_games"
    #[serde(default = "default_odds_mode")]
    pub mode: String,
    /// Max competitiveness % for close_games mode (fav_prob - dog_prob)
    #[serde(default = "default_max_competitiveness")]
    pub max_competitiveness_pct: f64,
    /// Flat bet size in USDC for close_games mode
    #[serde(default = "default_flat_size")]
    pub flat_size_usdc: f64,
    /// Log-only mode (paper trading): log signals but don't execute
    #[serde(default)]
    pub log_only: bool,
}

fn default_odds_mode() -> String {
    "arb".to_string()
}

fn default_max_competitiveness() -> f64 {
    10.0
}

fn default_flat_size() -> f64 {
    2.50
}

#[derive(Debug, Clone, Deserialize)]
pub struct SizingConfig {
    pub max_bet_pct: f64,
    #[serde(default = "default_min_price")]
    pub min_price: f64,
    #[serde(default = "default_max_price")]
    pub max_price: f64,
}

fn default_min_price() -> f64 {
    0.05
}

fn default_max_price() -> f64 {
    0.95
}

#[derive(Debug, Clone, Deserialize)]
pub struct RiskConfig {
    pub max_daily_loss_pct: f64,
    pub min_bankroll: f64,
    pub max_open_bets: u32,
    /// Max percentage of bankroll that can be deployed (cash committed to open positions).
    /// 0 = disabled (no limit). Default: 70%.
    #[serde(default = "default_max_deployment_pct")]
    pub max_deployment_pct: f64,
}

fn default_max_deployment_pct() -> f64 {
    70.0
}

/// Sport-specific sizing caps (max % of bankroll per game line).
/// Budget per line = min(90% bankroll / total_lines_in_wave, sport_cap).
#[derive(Debug, Clone, Deserialize)]
pub struct SportSizingConfig {
    /// Voetbal moneyline (NO underdog / YES favorite). Data: 100% WR, 43% ROI.
    #[serde(default = "default_voetbal_ml")]
    pub voetbal_ml_pct: f64,
    /// Voetbal draw (NO draw). Data: 100% WR, 37% ROI.
    #[serde(default = "default_voetbal_draw")]
    pub voetbal_draw_pct: f64,
    /// NHL moneyline. Data: 65% WR, 16% ROI.
    #[serde(default = "default_nhl_ml")]
    pub nhl_ml_pct: f64,
    /// NBA moneyline. Data: 60% WR, 2-10% ROI (improving).
    #[serde(default = "default_nba_ml")]
    pub nba_ml_pct: f64,
    /// NBA spread. Data: 54% WR, 7% ROI.
    #[serde(default = "default_nba_spread")]
    pub nba_spread_pct: f64,
    /// FIF (FIFA WCQ/friendlies) moneyline. Data: 22% WR, -91% ROI on 9 bets. Experimental.
    #[serde(default = "default_fif_ml")]
    pub fif_ml_pct: f64,
    /// FIF draw.
    #[serde(default = "default_fif_draw")]
    pub fif_draw_pct: f64,
    /// MLB moneyline.
    #[serde(default)]
    pub mlb_ml_pct: f64,
    /// NFL moneyline.
    #[serde(default)]
    pub nfl_ml_pct: f64,
    /// Fallback sizing % for leagues without a sport-specific field.
    #[serde(default = "default_fallback_pct")]
    pub fallback_pct: f64,
    /// Minimum bet size in USDC. Below this → skip game.
    #[serde(default = "default_min_bet_usdc")]
    pub min_bet_usdc: f64,
}

impl Default for SportSizingConfig {
    fn default() -> Self {
        Self {
            voetbal_ml_pct: 8.0,
            voetbal_draw_pct: 5.0,
            nhl_ml_pct: 5.0,
            nba_ml_pct: 3.0,
            nba_spread_pct: 3.0,
            fif_ml_pct: 0.0,
            fif_draw_pct: 0.0,
            mlb_ml_pct: 0.0,
            nfl_ml_pct: 0.0,
            fallback_pct: 2.0,
            min_bet_usdc: 2.50,
        }
    }
}

fn default_voetbal_ml() -> f64 { 8.0 }
fn default_voetbal_draw() -> f64 { 5.0 }
fn default_nhl_ml() -> f64 { 5.0 }
fn default_nba_ml() -> f64 { 3.0 }
fn default_nba_spread() -> f64 { 3.0 }
fn default_fif_ml() -> f64 { 0.0 }
fn default_fif_draw() -> f64 { 0.0 }
fn default_fallback_pct() -> f64 { 2.0 }
fn default_min_bet_usdc() -> f64 { 2.50 }

impl SportSizingConfig {
    /// Get the max % cap for a given sport + game line combination.
    /// Returns None if this sport/game_line combo should be skipped.
    ///
    /// Uses sport-specific overrides when set (> 0), otherwise falls back
    /// to the generic fallback_pct (typically copy_base_size_pct from config).
    pub fn cap_for(&self, league: &str, game_line: &str) -> Option<f64> {
        let is_football = !matches!(league, "nba" | "nhl" | "mlb" | "nfl" | "cbb" | "ncaa");

        // Sport-specific override if configured (> 0)
        let specific = if league == "fif" {
            match game_line {
                "win" => Some(self.fif_ml_pct),
                "draw" => Some(self.fif_draw_pct),
                _ => None,
            }
        } else if is_football {
            match game_line {
                "win" => Some(self.voetbal_ml_pct),
                "draw" => Some(self.voetbal_draw_pct),
                _ => None,
            }
        } else {
            // US sports + tennis + esports: check named field, else fallback
            match (league, game_line) {
                ("nhl", "win") => Some(self.nhl_ml_pct),
                ("nba", "win") => Some(self.nba_ml_pct),
                ("nba", "spread") => Some(self.nba_spread_pct),
                ("mlb", "win") => Some(self.mlb_ml_pct),
                ("nfl", "win") => Some(self.nfl_ml_pct),
                (_, "win") => Some(self.fallback_pct),
                _ => None,
            }
        };

        // Filter out 0.0 (= disabled)
        specific.filter(|&v| v > 0.0)
    }

    /// List allowed game line types for a given league.
    pub fn allowed_game_lines(&self, league: &str) -> Vec<&'static str> {
        let is_football = !matches!(league, "nba" | "nhl" | "mlb" | "nfl" | "cbb" | "ncaa");
        if is_football {
            // Football (incl fif): win + draw
            let mut v = vec![];
            if self.cap_for(league, "win").is_some() { v.push("win"); }
            if self.cap_for(league, "draw").is_some() { v.push("draw"); }
            v
        } else {
            // US sports / tennis / esports: win always, spread for NBA
            let mut v = vec!["win"];
            if league == "nba" && self.nba_spread_pct > 0.0 {
                v.push("spread");
            }
            v
        }
    }
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
