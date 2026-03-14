use std::collections::HashSet;
use std::io::Write;
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
    /// Avoids re-reading the entire file on every signal check.
    open_positions: Mutex<HashSet<String>>,
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

        // Bootstrap the open positions cache from existing data
        let open_positions = Self::load_open_positions_from_file(&path);

        Self {
            path,
            file: Mutex::new(file),
            open_positions: Mutex::new(open_positions),
        }
    }

    fn load_open_positions_from_file(path: &PathBuf) -> HashSet<String> {
        let contents = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(_) => return HashSet::new(),
        };
        contents
            .lines()
            .filter_map(|line| serde_json::from_str::<TradeLog>(line).ok())
            .filter(|t| t.result.is_none() && t.filled)
            .map(|t| format!("{}:{}", t.condition_id, t.outcome.to_lowercase()))
            .collect()
    }

    fn position_key(condition_id: &str, outcome: &str) -> String {
        format!("{}:{}", condition_id, outcome.to_lowercase())
    }

    pub fn log(&self, trade: TradeLog) {
        // Update open positions cache
        if trade.filled && trade.result.is_none() && !trade.outcome.is_empty() {
            let key = Self::position_key(&trade.condition_id, &trade.outcome);
            self.open_positions.lock().unwrap().insert(key);
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
    /// Uses the in-memory cache instead of re-reading the entire file.
    pub fn has_open_position(&self, condition_id: &str, outcome: &str) -> bool {
        let key = Self::position_key(condition_id, outcome);
        self.open_positions.lock().unwrap().contains(&key)
    }

    /// Rewrite the entire log with updated records (used by the resolver).
    /// Thread-safe: truncates the file under the mutex then rewrites all records.
    /// Also rebuilds the open positions cache from the new data.
    pub fn rewrite_all(&self, trades: Vec<TradeLog>) {
        // Rebuild open positions cache
        let new_open: HashSet<String> = trades
            .iter()
            .filter(|t| t.result.is_none() && t.filled && !t.outcome.is_empty())
            .map(|t| Self::position_key(&t.condition_id, &t.outcome))
            .collect();
        *self.open_positions.lock().unwrap() = new_open;

        let mut file = self.file.lock().unwrap();
        if let Err(e) = file.set_len(0) {
            tracing::error!("failed to truncate trade log: {e}");
            return;
        }
        for trade in &trades {
            if let Ok(json) = serde_json::to_string(trade) {
                let _ = writeln!(file, "{json}");
            }
        }
    }
}
