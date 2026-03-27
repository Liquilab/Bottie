use std::collections::BTreeMap;

use tracing::info;

use crate::clob::types::WalletPosition;
use crate::config::SportSizingConfig;
use crate::copy_trader::CopyTrader;

/// Wave-based budget planner.
///
/// Allocates per-game-line budgets based on:
/// 1. Current bankroll (recalculated every poll cycle)
/// 2. Number of game lines remaining in the current wave
/// 3. Sport-specific caps (voetbal ML 8%, draw 5%, etc.)
///
/// Budget per line = min(90% bankroll / total_lines, sport_cap)
/// If budget < $2.50 → skip smallest games (lowest Cannae game total)
pub struct WaveBudget {
    pub sport_sizing: SportSizingConfig,
    /// Cannae games: event_slug → CannaeGameInfo
    pub cannae_games: BTreeMap<String, CannaeGameInfo>,
}

#[derive(Debug, Clone)]
pub struct CannaeGameInfo {
    pub total_usdc: f64,
    pub league: String,
    pub legs: Vec<CannaeGameLine>,
}

#[derive(Debug, Clone)]
pub struct CannaeGameLine {
    pub title: String,
    pub game_line: String,
    pub usdc: f64,
}

impl WaveBudget {
    pub fn new(sport_sizing: SportSizingConfig) -> Self {
        Self {
            sport_sizing,
            cannae_games: BTreeMap::new(),
        }
    }

    pub fn update_config(&mut self, sport_sizing: SportSizingConfig) {
        self.sport_sizing = sport_sizing;
    }

    /// Refresh Cannae game data from raw positions.
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
            let title = pos.title.as_deref().unwrap_or("").to_string();
            let game_line = CopyTrader::detect_market_type(&title);
            let league = slug.split('-').next().unwrap_or("").to_string();

            let entry = self
                .cannae_games
                .entry(slug.to_string())
                .or_insert_with(|| CannaeGameInfo {
                    total_usdc: 0.0,
                    league: league.clone(),
                    legs: Vec::new(),
                });

            entry.total_usdc += usdc;

            // Dedup legs by title
            if !entry.legs.iter().any(|l| l.title == title) && !title.is_empty() {
                entry.legs.push(CannaeGameLine {
                    title,
                    game_line,
                    usdc,
                });
            }
        }
    }

    /// Count the total number of game lines we would buy across all games.
    /// Only counts game lines that pass the sport-specific filter.
    pub fn count_filtered_lines(&self) -> usize {
        let mut count = 0;
        for game in self.cannae_games.values() {
            let allowed = self.sport_sizing.allowed_game_lines(&game.league);
            // Count unique game line types in this game that are allowed
            let mut seen_types: Vec<&str> = Vec::new();
            for leg in &game.legs {
                let gl = leg.game_line.as_str();
                if allowed.contains(&gl) && !seen_types.contains(&gl) {
                    seen_types.push(gl);
                    count += 1;
                }
            }
        }
        count
    }

    /// Calculate the per-line budget percentage.
    /// Returns min(90% bankroll / total_lines, sport_cap) for each game line.
    ///
    /// bankroll = current cash (automatically includes recycled capital).
    pub fn line_budget_pct(&self, bankroll: f64) -> f64 {
        let total_lines = self.count_filtered_lines().max(1) as f64;
        let deployment_cap = 90.0; // 90% of bankroll max
        let even_split = deployment_cap / total_lines;
        even_split // caller applies min(even_split, sport_cap) per line
    }

    /// Get the sizing percentage for a specific game line.
    /// Returns 0.0 if the game line should be skipped.
    pub fn get_line_pct(&self, bankroll: f64, league: &str, game_line: &str) -> f64 {
        // Sport-specific cap
        let cap = match self.sport_sizing.cap_for(league, game_line) {
            Some(c) => c,
            None => return 0.0, // this game line not allowed for this sport
        };

        // Budget-driven limit
        let budget_pct = self.line_budget_pct(bankroll);

        // Use the smaller of cap and budget
        let pct = cap.min(budget_pct);

        // Check minimum bet
        let usdc = bankroll * pct / 100.0;
        if usdc < self.sport_sizing.min_bet_usdc {
            return 0.0;
        }

        pct
    }

    /// Log the CANNAE GAMES flight board.
    pub fn log_flight_board(&self, schedule: &crate::scheduler::GameSchedule, bankroll: f64) {
        if self.cannae_games.is_empty() {
            return;
        }

        let total_budget: f64 = self.cannae_games.values().map(|g| g.total_usdc).sum();
        let filtered_lines = self.count_filtered_lines();
        let budget_pct = self.line_budget_pct(bankroll);

        info!(
            "BUDGET: bankroll=${:.0} | {} games, {} lines | {:.1}%/line (before caps)",
            bankroll, self.cannae_games.len(), filtered_lines, budget_pct,
        );
        info!(
            "CANNAE GAMES: {} events, ${:.0} total",
            self.cannae_games.len(),
            total_budget,
        );

        // Sort by total USDC descending
        let mut sorted: Vec<_> = self.cannae_games.iter().collect();
        sorted.sort_by(|a, b| {
            b.1.total_usdc
                .partial_cmp(&a.1.total_usdc)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Build slug → kickoff map from schedule
        let kickoff_map: std::collections::HashMap<&str, &chrono::DateTime<chrono::Utc>> =
            schedule
                .games
                .iter()
                .map(|g| (g.event_slug.as_str(), &g.start_time))
                .collect();

        for (slug, game) in sorted.iter().take(20) {
            let leg_types: String = game
                .legs
                .iter()
                .take(5)
                .map(|l| l.game_line.as_str())
                .collect::<Vec<_>>()
                .join("+");
            let kickoff = kickoff_map
                .get(slug.as_str())
                .map(|dt| dt.format("%H:%M UTC").to_string())
                .unwrap_or_else(|| "??:??".to_string());

            // Show which lines we'd buy and at what %
            let allowed = self.sport_sizing.allowed_game_lines(&game.league);
            let our_lines: Vec<String> = game.legs.iter()
                .filter(|l| allowed.contains(&l.game_line.as_str()))
                .map(|l| {
                    let pct = self.get_line_pct(bankroll, &game.league, &l.game_line);
                    format!("{}@{:.0}%", l.game_line, pct)
                })
                .collect();
            let our_str = if our_lines.is_empty() {
                "SKIP".to_string()
            } else {
                our_lines.join("+")
            };

            info!(
                "  ${:>7.0} | {} legs ({}) | {} | {} | {}",
                game.total_usdc, game.legs.len(), leg_types, kickoff, our_str, slug
            );
        }
    }
}
