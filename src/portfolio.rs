use crate::logger::{TradeLog, TradeLogger};

pub struct Portfolio {
    pub total_trades: u32,
    pub wins: u32,
    pub losses: u32,
    pub total_pnl: f64,
    pub best_trade: f64,
    pub worst_trade: f64,
}

impl Portfolio {
    pub fn from_logs(logger: &TradeLogger) -> Self {
        let trades = logger.load_all();
        let resolved: Vec<&TradeLog> = trades.iter().filter(|t| t.result.is_some()).collect();

        let wins = resolved.iter().filter(|t| t.result.as_deref() == Some("win")).count() as u32;
        let losses = resolved.iter().filter(|t| t.result.as_deref() == Some("loss")).count() as u32;
        let total_pnl: f64 = resolved.iter().filter_map(|t| t.pnl).sum();
        let best = resolved.iter().filter_map(|t| t.pnl).fold(0.0f64, f64::max);
        let worst = resolved.iter().filter_map(|t| t.pnl).fold(0.0f64, f64::min);

        Self {
            total_trades: resolved.len() as u32,
            wins,
            losses,
            total_pnl,
            best_trade: best,
            worst_trade: worst,
        }
    }

    pub fn win_rate(&self) -> f64 {
        if self.total_trades == 0 {
            return 0.0;
        }
        self.wins as f64 / self.total_trades as f64
    }

    pub fn summary(&self) -> String {
        format!(
            "Trades: {} | W/L: {}/{} | Win rate: {:.1}% | PnL: ${:.2} | Best: ${:.2} | Worst: ${:.2}",
            self.total_trades,
            self.wins,
            self.losses,
            self.win_rate() * 100.0,
            self.total_pnl,
            self.best_trade,
            self.worst_trade
        )
    }
}
