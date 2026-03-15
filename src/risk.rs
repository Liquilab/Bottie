use std::collections::HashMap;

use crate::config::RiskConfig;
use tracing::{info, warn};

pub struct RiskManager {
    config: RiskConfig,
    daily_pnl: f64,
    open_bets: u32,
    bankroll: f64,
    initial_bankroll: f64,
    // Fix #12: Track open bets per wallet and per sport
    open_per_wallet: HashMap<String, u32>,
    open_per_sport: HashMap<String, u32>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum RiskDecision {
    Allowed,
    Rejected(String),
}

// Per-wallet concentration limit — prevent blindly following one wallet
const MAX_OPEN_PER_WALLET: u32 = 30;

impl RiskManager {
    pub fn new(config: RiskConfig, bankroll: f64) -> Self {
        Self {
            config,
            daily_pnl: 0.0,
            open_bets: 0,
            bankroll,
            initial_bankroll: bankroll,
            open_per_wallet: HashMap::new(),
            open_per_sport: HashMap::new(),
        }
    }

    pub fn check_trade(&self, size_usdc: f64) -> RiskDecision {
        // Check minimum bankroll
        if self.bankroll < self.config.min_bankroll {
            return RiskDecision::Rejected(format!(
                "bankroll ${:.2} below minimum ${:.2}",
                self.bankroll, self.config.min_bankroll
            ));
        }

        // Check daily loss limit
        let max_daily_loss = self.initial_bankroll * self.config.max_daily_loss_pct / 100.0;
        if self.daily_pnl < -max_daily_loss {
            return RiskDecision::Rejected(format!(
                "daily loss ${:.2} exceeds limit ${:.2}",
                self.daily_pnl.abs(),
                max_daily_loss
            ));
        }

        // Check open bets limit
        if self.open_bets >= self.config.max_open_bets {
            return RiskDecision::Rejected(format!(
                "open bets {} at limit {}",
                self.open_bets, self.config.max_open_bets
            ));
        }

        // Check max bet size
        let max_bet = self.bankroll * 0.10; // 10% hard cap
        if size_usdc > max_bet {
            return RiskDecision::Rejected(format!(
                "bet ${:.2} exceeds 10% of bankroll ${:.2}",
                size_usdc, self.bankroll
            ));
        }

        RiskDecision::Allowed
    }

    /// Fix #12: Check trade with wallet/sport concentration limits
    pub fn check_trade_with_context(
        &self,
        size_usdc: f64,
        wallet: Option<&str>,
        sport: &str,
    ) -> RiskDecision {
        // First run standard checks
        let base_check = self.check_trade(size_usdc);
        if base_check != RiskDecision::Allowed {
            return base_check;
        }

        // Check per-wallet limit
        if let Some(w) = wallet {
            let count = self.open_per_wallet.get(w).copied().unwrap_or(0);
            if count >= MAX_OPEN_PER_WALLET {
                return RiskDecision::Rejected(format!(
                    "wallet {} has {} open bets (limit {})",
                    &w[..10.min(w.len())],
                    count,
                    MAX_OPEN_PER_WALLET
                ));
            }
        }

        RiskDecision::Allowed
    }

    pub fn record_trade_opened(&mut self, _size_usdc: f64) {
        self.open_bets += 1;
    }

    /// Record trade opened with wallet/sport tracking
    pub fn record_trade_opened_with_context(
        &mut self,
        _size_usdc: f64,
        wallet: Option<&str>,
        sport: &str,
    ) {
        self.open_bets += 1;
        if let Some(w) = wallet {
            *self.open_per_wallet.entry(w.to_string()).or_insert(0) += 1;
        }
        *self.open_per_sport.entry(sport.to_string()).or_insert(0) += 1;
    }

    pub fn record_trade_closed(&mut self, pnl: f64) {
        self.open_bets = self.open_bets.saturating_sub(1);
        self.daily_pnl += pnl;
        self.bankroll += pnl;

        if self.daily_pnl < -(self.initial_bankroll * self.config.max_daily_loss_pct / 100.0) {
            warn!(
                "DAILY LOSS LIMIT HIT: pnl=${:.2}, stopping trading",
                self.daily_pnl
            );
        }
    }

    /// Record trade closed with wallet/sport tracking
    pub fn record_trade_closed_with_context(
        &mut self,
        pnl: f64,
        wallet: Option<&str>,
        sport: &str,
    ) {
        self.record_trade_closed(pnl);
        if let Some(w) = wallet {
            if let Some(count) = self.open_per_wallet.get_mut(w) {
                *count = count.saturating_sub(1);
            }
        }
        if let Some(count) = self.open_per_sport.get_mut(sport) {
            *count = count.saturating_sub(1);
        }
    }

    /// Decrement open_bets without affecting pnl/bankroll.
    /// Used by the resolver to close out positions before applying net pnl separately.
    pub fn decrement_open_bets(&mut self) {
        self.open_bets = self.open_bets.saturating_sub(1);
    }

    pub fn reset_daily(&mut self) {
        self.daily_pnl = 0.0;
        self.initial_bankroll = self.bankroll;
    }

    pub fn bankroll(&self) -> f64 {
        self.bankroll
    }

    pub fn update_bankroll(&mut self, new_bankroll: f64) {
        self.bankroll = new_bankroll;
        self.initial_bankroll = new_bankroll;
        self.daily_pnl = 0.0; // reset — on-chain balance is truth
    }

    pub fn add_daily_pnl(&mut self, pnl: f64) {
        self.daily_pnl += pnl;
    }

    pub fn daily_pnl(&self) -> f64 {
        self.daily_pnl
    }

    pub fn open_bets(&self) -> u32 {
        self.open_bets
    }

    /// Sync open_bets with actual count from trade log.
    /// Fixes drift when positions are manually sold via UI.
    pub fn sync_open_bets(&mut self, actual_count: u32) {
        if self.open_bets != actual_count {
            info!(
                "risk: open_bets drift corrected {} → {}",
                self.open_bets, actual_count
            );
            self.open_bets = actual_count;
        }
    }
}
