use std::collections::{HashMap, HashSet};
use std::io::{Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::sync::Mutex;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeLog {
    pub timestamp: DateTime<Utc>,
    pub token_id: String,
    pub condition_id: String,
    pub market_title: String,
    pub sport: String,
    pub side: String,
    pub outcome: String,        // "Yes" | "No"
    pub price: f64,
    pub size_usdc: f64,
    pub size_shares: f64,

    pub signal_source: String,
    pub copy_wallet: Option<String>,
    pub consensus_count: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub consensus_wallets: Option<Vec<String>>,
    pub edge_pct: f64,
    pub confidence: f64,
    pub signal_delay_ms: u64,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_slug: Option<String>,

    pub order_id: Option<String>,
    pub filled: bool,
    pub dry_run: bool,

    pub result: Option<String>, // "win" | "loss" | "refund" | "take_profit" | "sold" | "phantom"
    pub pnl: Option<f64>,
    pub resolved_at: Option<DateTime<Utc>>,

    /// Price at which position was exited (TP sell or estimated at phantom-sync time for manual sells)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sell_price: Option<f64>,
    /// Actual PnL = sell_price × shares - buy_price × shares (populated for all non-resolution exits)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actual_pnl: Option<f64>,
    /// How position was closed: "resolution" | "take_profit" | "manual" | "phantom"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exit_type: Option<String>,

    /// Strategy version tag from autoresearch deployment (Fix #7)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub strategy_version: Option<String>,
}

pub struct TradeLogger {
    path: PathBuf,
    file: Mutex<std::fs::File>,
    /// In-memory set of "condition_id:outcome" for open (unresolved, filled) positions.
    open_positions: Mutex<HashSet<String>>,
    /// event_slug → market_type for open positions.
    /// Prevents conflicting moneyline/draw bets on same event.
    /// Spread and O/U don't conflict with moneyline.
    open_event_types: Mutex<HashMap<String, String>>,
    /// event_dedup_key → copy_wallet for open positions.
    /// Used to detect contradictions: different wallets betting on the same event.
    open_event_wallets: Mutex<HashMap<String, String>>,
}

impl TradeLogger {
    pub fn new(path: PathBuf) -> Self {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).ok();
        }

        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .expect("failed to open trade log file");

        // Bootstrap caches from existing data
        let (open_positions, open_event_types, open_event_wallets) = Self::load_open_caches_from_file(&path);

        Self {
            path,
            file: Mutex::new(file),
            open_positions: Mutex::new(open_positions),
            open_event_types: Mutex::new(open_event_types),
            open_event_wallets: Mutex::new(open_event_wallets),
        }
    }

    fn load_open_caches_from_file(path: &PathBuf) -> (HashSet<String>, HashMap<String, String>, HashMap<String, String>) {
        let contents = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(_) => return (HashSet::new(), HashMap::new(), HashMap::new()),
        };
        let open: Vec<TradeLog> = contents
            .lines()
            .filter_map(|line| serde_json::from_str::<TradeLog>(line).ok())
            .filter(|t| t.result.is_none() && t.filled)
            .collect();
        let positions = open.iter()
            .map(|t| format!("{}:{}", t.condition_id, t.outcome.to_lowercase()))
            .collect();
        let mut event_types = HashMap::new();
        let mut event_wallets = HashMap::new();
        for t in &open {
            let dedup_key = Self::event_dedup_key(
                t.event_slug.as_deref().unwrap_or(""),
                &t.market_title,
            );
            if !dedup_key.is_empty() {
                let mtype = Self::market_type(&t.market_title);
                event_types.insert(dedup_key.clone(), mtype);
                if let Some(wallet) = &t.copy_wallet {
                    event_wallets.insert(dedup_key, wallet.clone());
                }
            }
        }
        (positions, event_types, event_wallets)
    }

    /// Classify market type from title.
    /// "moneyline" and "draw" conflict with each other.
    /// "spread" and "total" don't conflict with anything.
    fn market_type(title: &str) -> String {
        let t = title.to_lowercase();
        if t.contains("spread") || t.contains("(-") || t.contains("(+") {
            "spread".to_string()
        } else if t.contains("o/u") || t.contains("over/under") || t.contains("total") {
            "total".to_string()
        } else {
            // "win on", "draw", "vs." all are moneyline-type (mutually exclusive outcomes)
            "moneyline".to_string()
        }
    }

    fn position_key(condition_id: &str, outcome: &str) -> String {
        format!("{}:{}", condition_id, outcome.to_lowercase())
    }

    pub fn log(&self, trade: TradeLog) {
        // Update open positions cache
        if trade.filled && trade.result.is_none() && !trade.outcome.is_empty() {
            let key = Self::position_key(&trade.condition_id, &trade.outcome);
            self.open_positions.lock().unwrap().insert(key);
            let dedup_key = Self::event_dedup_key(
                trade.event_slug.as_deref().unwrap_or(""),
                &trade.market_title,
            );
            if !dedup_key.is_empty() {
                let mtype = Self::market_type(&trade.market_title);
                self.open_event_types.lock().unwrap().insert(dedup_key.clone(), mtype);
                if let Some(wallet) = &trade.copy_wallet {
                    self.open_event_wallets.lock().unwrap().insert(dedup_key, wallet.clone());
                }
            }
        }

        let json = match serde_json::to_string(&trade) {
            Ok(j) => j,
            Err(e) => {
                tracing::error!("failed to serialize trade log: {e}");
                return;
            }
        };

        let mut file = self.file.lock().unwrap();
        if let Err(e) = writeln!(file, "{json}") {
            tracing::error!("failed to write trade log: {e}");
        }
    }

    pub fn load_all(&self) -> Vec<TradeLog> {
        let contents = match std::fs::read_to_string(&self.path) {
            Ok(c) => c,
            Err(_) => return vec![],
        };

        contents
            .lines()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect()
    }

    /// Check if there is already an open (unresolved, filled) position for a market+outcome.
    pub fn has_open_position(&self, condition_id: &str, outcome: &str) -> bool {
        let key = Self::position_key(condition_id, outcome);
        self.open_positions.lock().unwrap().contains(&key)
    }

    /// Normalize event slug: strip "-more-markets" suffix so
    /// "bra-cha-gre-2026-03-16" and "bra-cha-gre-2026-03-16-more-markets" match.
    fn normalize_slug(slug: &str) -> String {
        slug.trim_end_matches("-more-markets").to_string()
    }

    /// Extract a dedup key from a trade. Uses event_slug if available,
    /// otherwise derives one from market title (team names / event description).
    /// This prevents trades with empty event_slug from bypassing dedup.
    pub fn event_dedup_key(event_slug: &str, market_title: &str) -> String {
        let slug = Self::normalize_slug(event_slug);
        if !slug.is_empty() {
            return slug;
        }
        // Fallback: extract base event from title by removing market-type suffixes
        // "Spread: Trail Blazers (-10.5)" → "trail blazers"
        // "Trail Blazers vs. Nets: O/U 221.5" → "trail blazers vs. nets"
        // "Will Brentford FC win on 2026-03-16?" → "brentford fc 2026-03-16"
        let t = market_title.to_lowercase();
        let t = t.trim_start_matches("spread: ")
                 .trim_start_matches("will ");
        // Remove everything after common separators
        let base = t.split(": o/u").next()
            .and_then(|s| s.split(": both").next())
            .and_then(|s| s.split(" (-").next())
            .and_then(|s| s.split(" (+").next())
            .unwrap_or(t);
        // Remove trailing question marks and whitespace
        let clean = base.trim_end_matches('?').trim();
        if clean.len() >= 5 {
            format!("_title:{}", clean)
        } else {
            String::new() // too short to be reliable
        }
    }

    /// Check if there is an open position on this event from a DIFFERENT wallet.
    /// Prevents contradictions: e.g. Cannae bets "win" while sovereign bets "O/U" on the same event.
    /// Returns Some(existing_wallet) if contradiction detected, None if clear.
    pub fn has_conflicting_wallet_on_event(&self, event_slug: &str, market_title: &str, new_wallet: &str) -> Option<String> {
        let dedup_key = Self::event_dedup_key(event_slug, market_title);
        if dedup_key.is_empty() {
            return None;
        }
        let event_wallets = self.open_event_wallets.lock().unwrap();
        if let Some(existing_wallet) = event_wallets.get(&dedup_key) {
            if existing_wallet != new_wallet {
                return Some(existing_wallet.clone());
            }
        }
        None
    }

    /// Check if there is ANY open position on this event.
    /// With consensus strategy: 1 trade per event, regardless of market type.
    /// Uses event_dedup_key to also match trades that had empty event_slug.
    pub fn has_any_open_on_event(&self, event_slug: &str, market_title: &str) -> bool {
        let dedup_key = Self::event_dedup_key(event_slug, market_title);
        if dedup_key.is_empty() {
            return false;
        }
        let event_types = self.open_event_types.lock().unwrap();
        event_types.contains_key(&dedup_key)
    }

    /// Rewrite the entire log with updated records (used by the resolver).
    /// Thread-safe: truncates the file under the mutex then rewrites all records.
    /// Also rebuilds the open positions cache from the new data.
    pub fn rewrite_all(&self, trades: Vec<TradeLog>) {
        // Rebuild caches
        let open: Vec<&TradeLog> = trades.iter()
            .filter(|t| t.result.is_none() && t.filled)
            .collect();
        let new_positions: HashSet<String> = open.iter()
            .filter(|t| !t.outcome.is_empty())
            .map(|t| Self::position_key(&t.condition_id, &t.outcome))
            .collect();
        let mut new_event_types = HashMap::new();
        let mut new_event_wallets = HashMap::new();
        for t in &open {
            let dedup_key = Self::event_dedup_key(
                t.event_slug.as_deref().unwrap_or(""),
                &t.market_title,
            );
            if !dedup_key.is_empty() {
                let mtype = Self::market_type(&t.market_title);
                new_event_types.insert(dedup_key.clone(), mtype);
                if let Some(wallet) = &t.copy_wallet {
                    new_event_wallets.insert(dedup_key, wallet.clone());
                }
            }
        }
        *self.open_positions.lock().unwrap() = new_positions;
        *self.open_event_types.lock().unwrap() = new_event_types;
        *self.open_event_wallets.lock().unwrap() = new_event_wallets;

        let mut file = self.file.lock().unwrap();
        if let Err(e) = file.set_len(0) {
            tracing::error!("failed to truncate trade log: {e}");
            return;
        }
        // CRITICAL: reset cursor to start after truncation, otherwise writes at old offset
        if let Err(e) = file.seek(SeekFrom::Start(0)) {
            tracing::error!("failed to seek trade log: {e}");
            return;
        }
        for trade in &trades {
            if let Ok(json) = serde_json::to_string(trade) {
                let _ = writeln!(file, "{json}");
            }
        }
        let _ = file.flush();
    }
}
