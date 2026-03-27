use std::collections::BTreeMap;

use tracing::info;

use crate::clob::types::WalletPosition;
use crate::config::BudgetConfig;
use crate::copy_trader::CopyTrader;

/// Tracks Cannae's games and allocates per-game budgets.
///
/// Flow:
/// 1. CANNAE GAMES log (30 min) feeds game list + remaining_games count
/// 2. T5 games sorted by Cannae game total DESC
/// 3. game_budget = effective_cash / remaining_games_today
/// 4. Per leg: win = 100% tier, ou/spread = ou_spread_multiplier × tier
pub struct BudgetPlanner {
    config: BudgetConfig,
    /// Cannae games seen this cycle: event_slug → (total_usdc, leg_count, Vec<leg_title>)
    pub cannae_games: BTreeMap<String, CannaeGameInfo>,
}

#[derive(Debug, Clone)]
pub struct CannaeGameInfo {
    pub total_usdc: f64,
    pub leg_count: usize,
    pub legs: Vec<CannaeLegs>,
}

#[derive(Debug, Clone)]
pub struct CannaeLegs {
    pub title: String,
    pub market_type: String,
    pub usdc: f64,
    pub shares: f64,
}

/// Per-leg allocation for the T5 PLAN log
#[derive(Debug, Clone)]
pub struct LegAllocation {
    pub title: String,
    pub market_type: String,
    pub cannae_usdc: f64,
    pub our_usdc: f64,
    pub multiplier: f64,
    pub tier_label: String,
}

impl BudgetPlanner {
    pub fn new(config: BudgetConfig) -> Self {
        Self {
            config,
            cannae_games: BTreeMap::new(),
        }
    }

    pub fn update_config(&mut self, config: BudgetConfig) {
        self.config = config;
    }

    /// Refresh Cannae game data from raw positions.
    /// Called every ~30 min from the CANNAE GAMES log cycle.
    pub fn refresh_from_positions(&mut self, positions: &[WalletPosition]) {
        self.cannae_games.clear();

        for pos in positions {
            let cur = pos.cur_price_f64();
            if cur <= 0.01 || cur >= 0.99 {
                continue;
            }

            let slug = pos
                .event_slug
                .as_deref()
                .or(pos.slug.as_deref())
                .unwrap_or("")
                .trim_end_matches("-more-markets");
            if slug.is_empty() {
                continue;
            }

            let usdc = pos.initial_value_f64();
            let shares = pos.size_f64();
            let title = pos.title.as_deref().unwrap_or("").to_string();
            let market_type = CopyTrader::detect_market_type(&title);

            let entry = self
                .cannae_games
                .entry(slug.to_string())
                .or_insert_with(|| CannaeGameInfo {
                    total_usdc: 0.0,
                    leg_count: 0,
                    legs: Vec::new(),
                });

            entry.total_usdc += usdc;

            // Dedup legs by title
            if !entry.legs.iter().any(|l| l.title == title) && !title.is_empty() {
                entry.leg_count += 1;
                entry.legs.push(CannaeLegs {
                    title,
                    market_type,
                    usdc,
                    shares,
                });
            }
        }
    }

    /// Number of remaining games (all Cannae games with active positions).
    pub fn remaining_games(&self) -> usize {
        self.cannae_games.len().max(1)
    }

    /// Calculate effective cash = available_cash + recycling estimate.
    pub fn effective_cash(
        &self,
        available_cash: f64,
        own_positions: &[WalletPosition],
    ) -> f64 {
        let recycling = self.estimate_recycling(own_positions);
        available_cash + recycling
    }

    /// Estimate capital returning from positions resolving within horizon.
    /// Positions with cur_price >= threshold are likely winners.
    pub fn estimate_recycling(&self, own_positions: &[WalletPosition]) -> f64 {
        let min_price = self.config.recycling_min_price;
        let mut recycling = 0.0_f64;

        for pos in own_positions {
            let cur = pos.cur_price_f64();
            if cur >= min_price && cur < 0.99 {
                // Expected return = shares × cur_price (market-implied value)
                let value = pos.size_f64() * cur;
                recycling += value;
            }
        }

        recycling
    }

    /// Per-game budget: effective_cash / remaining_games.
    pub fn game_budget(&self, effective_cash: f64) -> f64 {
        let remaining = self.remaining_games() as f64;
        effective_cash / remaining
    }

    /// Market-type multiplier: win = 1.0, ou/spread = config multiplier.
    pub fn market_type_multiplier(&self, market_type: &str) -> f64 {
        match market_type {
            "win" => 1.0,
            "ou" | "spread" => self.config.ou_spread_multiplier,
            // btts, draw, player_prop, other: use experiment multiplier too
            _ => self.config.ou_spread_multiplier,
        }
    }

    /// Log the CANNAE GAMES flight board.
    pub fn log_flight_board(&self, schedule: &crate::scheduler::GameSchedule) {
        if self.cannae_games.is_empty() {
            return;
        }

        let total_budget: f64 = self.cannae_games.values().map(|g| g.total_usdc).sum();
        info!(
            "CANNAE GAMES: {} events, ${:.0} total, {} remaining",
            self.cannae_games.len(),
            total_budget,
            self.remaining_games()
        );

        // Sort by total USDC descending
        let mut sorted: Vec<_> = self.cannae_games.iter().collect();
        sorted.sort_by(|a, b| {
            b.1.total_usdc
                .partial_cmp(&a.1.total_usdc)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Build slug → kickoff map from schedule
        let kickoff_map: std::collections::HashMap<&str, &chrono::DateTime<chrono::Utc>> = schedule.games
            .iter()
            .map(|g| (g.event_slug.as_str(), &g.start_time))
            .collect();

        for (slug, game) in sorted.iter().take(20) {
            let leg_types: String = game
                .legs
                .iter()
                .take(5)
                .map(|l| l.market_type.as_str())
                .collect::<Vec<_>>()
                .join("+");
            let kickoff = kickoff_map.get(slug.as_str())
                .map(|dt| dt.format("%H:%M UTC").to_string())
                .unwrap_or_else(|| "??:??".to_string());
            info!(
                "  ${:>7.0} | {} legs ({}) | {} | {}",
                game.total_usdc, game.leg_count, leg_types, kickoff, slug
            );
        }
    }

    /// Log the T5 PLAN for a specific game about to execute.
    pub fn log_t5_plan(
        &self,
        event_slug: &str,
        game_budget: f64,
        legs: &[LegAllocation],
    ) {
        let total_our: f64 = legs.iter().map(|l| l.our_usdc).sum();
        info!(
            "T5 PLAN: {} | game_budget=${:.0} | {} legs | total=${:.0}",
            event_slug,
            game_budget,
            legs.len(),
            total_our,
        );
        for leg in legs {
            let title_short = if leg.title.len() > 50 {
                let mut end = 50;
                while end < leg.title.len() && !leg.title.is_char_boundary(end) {
                    end += 1;
                }
                &leg.title[..end]
            } else {
                &leg.title
            };
            info!(
                "  ${:>5.0} | {:>6} | {} | Cannae ${:.0} | {}",
                leg.our_usdc,
                leg.market_type,
                title_short,
                leg.cannae_usdc,
                leg.tier_label,
            );
        }
    }
}
