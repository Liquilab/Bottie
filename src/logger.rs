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
    pub edge_pct: f64,
    pub confidence: f64,
    pub signal_delay_ms: u64,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_slug: Option<String>,

    pub order_id: Option<String>,
    pub filled: bool,
    pub dry_run: bool,

    pub result: Option<String>, // "win" | "loss" | "refund"
    pub pnl: Option<f64>,
    pub resolved_at: Option<DateTime<Utc>>,

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
        let (open_positions, open_event_types) = Self::load_open_caches_from_file(&path);

        Self {
            path,
            file: Mutex::new(file),
            open_positions: Mutex::new(open_positions),
            open_event_types: Mutex::new(open_event_types),
        }
    }

    fn load_open_caches_from_file(path: &PathBuf) -> (HashSet<String>, HashMap<String, String>) {
        let contents = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(_) => return (HashSet::new(), HashMap::new()),
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
        for t in &open {
            if let Some(slug) = &t.event_slug {
                if !slug.is_empty() {
                    let mtype = Self::market_type(&t.market_title);
                    event_types.insert(slug.clone(), mtype);
                }
            }
        }
        (positions, event_types)
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
            if let Some(slug) = &trade.event_slug {
                if !slug.is_empty() {
                    let mtype = Self::market_type(&trade.market_title);
                    self.open_event_types.lock().unwrap().insert(slug.clone(), mtype);
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

    /// Check if there is a CONFLICTING open position on this event.
    /// Moneyline/draw bets conflict with each other (only one outcome wins).
    /// Spread and O/U don't conflict with moneyline or each other.
    pub fn has_conflicting_event(&self, event_slug: &str, market_title: &str) -> bool {
        if event_slug.is_empty() {
            return false;
        }
        let new_type = Self::market_type(market_title);
        let event_types = self.open_event_types.lock().unwrap();
        match event_types.get(event_slug) {
            Some(existing_type) => {
                // Moneyline conflicts with moneyline (e.g. Stuttgart Yes vs Draw Yes)
                // Spread and total don't conflict with anything
                existing_type == "moneyline" && new_type == "moneyline"
            }
            None => false, // no existing position on this event
        }
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
        for t in &open {
            if let Some(slug) = &t.event_slug {
                if !slug.is_empty() {
                    let mtype = Self::market_type(&t.market_title);
                    new_event_types.insert(slug.clone(), mtype);
                }
            }
        }
        *self.open_positions.lock().unwrap() = new_positions;
        *self.open_event_types.lock().unwrap() = new_event_types;

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
