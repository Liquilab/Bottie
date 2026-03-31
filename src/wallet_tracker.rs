use std::collections::HashMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WatchedWallet {
    pub address: String,
    pub name: String,
    pub overall_win_rate: f64,
    pub sport_win_rates: HashMap<String, f64>,
    pub sport_trade_counts: HashMap<String, u32>,
    pub avg_roi: f64,
    pub total_tracked_trades: u32,
    pub total_wins: u32,
    pub weight: f64,
    pub last_seen: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct WalletTracker {
    wallets: HashMap<String, WatchedWallet>,
}

impl WalletTracker {
    pub fn new() -> Self {
        Self {
            wallets: HashMap::new(),
        }
    }

    pub fn from_config(entries: &[crate::config::WatchlistEntry]) -> Self {
        let mut wallets = HashMap::new();
        for entry in entries {
            let addr = entry.address.to_lowercase();
            wallets.insert(
                addr.clone(),
                WatchedWallet {
                    address: addr,
                    name: entry.name.clone(),
                    overall_win_rate: 0.55, // default assumption
                    sport_win_rates: HashMap::new(),
                    sport_trade_counts: HashMap::new(),
                    avg_roi: 0.0,
                    total_tracked_trades: 0,
                    total_wins: 0,
                    weight: entry.weight,
                    last_seen: Utc::now(),
                },
            );
        }
        Self { wallets }
    }

    pub fn is_watched(&self, address: &str) -> bool {
        self.wallets.contains_key(&address.to_lowercase())
    }

    pub fn get_wallet(&self, address: &str) -> Option<&WatchedWallet> {
        self.wallets.get(&address.to_lowercase())
    }

    pub fn get_wallet_mut(&mut self, address: &str) -> Option<&mut WatchedWallet> {
        self.wallets.get_mut(&address.to_lowercase())
    }

    pub fn all_addresses(&self) -> Vec<&str> {
        self.wallets.keys().map(|s| s.as_str()).collect()
    }

    pub fn wallet_count(&self) -> usize {
        self.wallets.len()
    }

    pub fn add_wallet(&mut self, wallet: WatchedWallet) {
        let addr = wallet.address.to_lowercase();
        self.wallets.entry(addr).or_insert(wallet);
    }

    pub fn record_trade_result(
        &mut self,
        address: &str,
        sport: &str,
        won: bool,
        pnl: f64,
    ) {
        if let Some(w) = self.wallets.get_mut(&address.to_lowercase()) {
            w.total_tracked_trades += 1;
            if won {
                w.total_wins += 1;
            }
            // Bayesian win rate with Laplace smoothing (prior: 50% over 10 virtual trades)
            w.overall_win_rate = (w.total_wins as f64 + 5.0) / (w.total_tracked_trades as f64 + 10.0);

            // Update sport-specific win rate using per-sport trade count
            let win_val = if won { 1.0 } else { 0.0 };
            let sport_count = w.sport_trade_counts.entry(sport.to_string()).or_insert(0);
            *sport_count += 1;
            let sn = *sport_count as f64;
            let sport_rate = w.sport_win_rates.entry(sport.to_string()).or_insert(0.5);
            *sport_rate = *sport_rate * ((sn - 1.0) / sn) + win_val / sn;

            // Update ROI
            let n = w.total_tracked_trades as f64;
            w.avg_roi = w.avg_roi * ((n - 1.0) / n) + pnl / n;
            w.last_seen = Utc::now();
        }
    }

}
