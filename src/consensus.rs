use std::collections::{HashMap, HashSet};
use tracing::info;

use crate::subgraph::SubgraphHolder;
use crate::scheduler::GameSchedule;

#[derive(Debug, Clone)]
pub struct ConsensusResult {
    /// Condition ID of the consensus leg (before opponent No conversion)
    pub best_cid: String,
    /// Side of the consensus leg ("Yes" or "No")
    pub best_side: String,
    /// Consensus percentage (0-100): unique traders on best leg / total unique traders
    pub consensus_pct: f64,
    /// Total shares on the consensus leg
    pub consensus_shares: f64,
    /// Total unique traders across all legs
    pub n_total_traders: usize,
    /// Condition ID to buy (opponent No if consensus is Yes)
    pub buy_cid: String,
    /// Side to buy (always "No" after conversion)
    pub buy_side: String,
    /// Token ID to buy
    pub buy_token_id: String,
    /// Question/title of the buy market
    pub buy_question: String,
}

/// Calculate whale consensus for a football game.
///
/// Takes top-100 holders per win-market condition, groups by leg (cid + side),
/// finds the leg with the most shares (weighted consensus), and converts to
/// opponent No if needed.
///
/// Returns None if:
/// - consensus_pct < min_pct
/// - fewer than min_traders unique traders
/// - opponent No token not found
pub fn calculate_consensus(
    holders_per_cid: &HashMap<String, Vec<SubgraphHolder>>,
    game_schedule: &GameSchedule,
    event_slug: &str,
    min_pct: f64,
    min_traders: u32,
) -> Option<ConsensusResult> {
    if holders_per_cid.is_empty() {
        return None;
    }

    // Collect per-leg stats: (cid, side) -> (unique traders, total shares)
    struct LegStats {
        traders: HashSet<String>,
        shares: f64,
    }

    let mut legs: HashMap<(String, String), LegStats> = HashMap::new();
    let mut all_traders: HashSet<String> = HashSet::new();

    // Track which cids are in the data (for opponent lookup)
    let cids: Vec<String> = holders_per_cid.keys().cloned().collect();

    for (cid, holders) in holders_per_cid {
        for h in holders {
            let side = if h.outcome_index == 0 { "Yes" } else { "No" };
            let key = (cid.clone(), side.to_string());
            let entry = legs.entry(key).or_insert_with(|| LegStats {
                traders: HashSet::new(),
                shares: 0.0,
            });
            if entry.traders.insert(h.user.clone()) {
                entry.shares += h.balance;
            }
            all_traders.insert(h.user.clone());
        }
    }

    let n_total = all_traders.len();
    if (n_total as u32) < min_traders {
        info!("CONSENSUS SKIP: {} — only {} traders (min {})", event_slug, n_total, min_traders);
        return None;
    }

    // Find leg with most shares (weighted consensus)
    let best = legs.iter()
        .max_by(|a, b| a.1.shares.partial_cmp(&b.1.shares).unwrap_or(std::cmp::Ordering::Equal))?;

    let (best_cid, best_side) = best.0;
    let consensus_pct = 100.0 * best.1.traders.len() as f64 / n_total as f64;
    let consensus_shares = best.1.shares;

    if consensus_pct < min_pct {
        info!(
            "CONSENSUS SKIP: {} — {:.0}% < {:.0}% threshold ({} traders on {} {})",
            event_slug, consensus_pct, min_pct, best.1.traders.len(), best_side, &best_cid[..best_cid.len().min(12)]
        );
        return None;
    }

    // Determine buy leg: always opponent No
    let (buy_cid, buy_token_id, buy_question) = if best_side == "Yes" {
        // Consensus = Team A Yes → buy Team B No (opponent)
        // Exclude draw cids (we only have win cids here, no draw)
        let draw_cids: Vec<&str> = vec![];
        match game_schedule.find_opponent_no_token(event_slug, best_cid, &draw_cids) {
            Some((opp_cid, no_token)) => {
                // Find question for the opponent market
                let q = game_schedule.games.iter()
                    .find(|g| g.event_slug == event_slug)
                    .and_then(|g| g.market_tokens.iter().find(|(c, _, _)| c == &opp_cid))
                    .map(|(_, q, _)| q.clone())
                    .unwrap_or_default();
                (opp_cid, no_token, q)
            }
            None => {
                // Fallback: buy same condition No
                let no_token = game_schedule.games.iter()
                    .find(|g| g.event_slug == event_slug)
                    .and_then(|g| g.market_tokens.iter().find(|(c, _, _)| c == best_cid))
                    .and_then(|(_, _, tokens)| tokens.iter().find(|(o, _)| o.eq_ignore_ascii_case("No")))
                    .map(|(_, t)| t.clone())
                    .unwrap_or_default();
                if no_token.is_empty() {
                    info!("CONSENSUS SKIP: {} — no No token found for {}", event_slug, &best_cid[..best_cid.len().min(12)]);
                    return None;
                }
                let q = game_schedule.games.iter()
                    .find(|g| g.event_slug == event_slug)
                    .and_then(|g| g.market_tokens.iter().find(|(c, _, _)| c == best_cid))
                    .map(|(_, q, _)| q.clone())
                    .unwrap_or_default();
                (best_cid.clone(), no_token, q)
            }
        }
    } else {
        // Consensus = Team A No → buy Team A No directly
        let no_token = game_schedule.games.iter()
            .find(|g| g.event_slug == event_slug)
            .and_then(|g| g.market_tokens.iter().find(|(c, _, _)| c == best_cid))
            .and_then(|(_, _, tokens)| tokens.iter().find(|(o, _)| o.eq_ignore_ascii_case("No")))
            .map(|(_, t)| t.clone())
            .unwrap_or_default();
        if no_token.is_empty() {
            info!("CONSENSUS SKIP: {} — no No token found for {}", event_slug, &best_cid[..best_cid.len().min(12)]);
            return None;
        }
        let q = game_schedule.games.iter()
            .find(|g| g.event_slug == event_slug)
            .and_then(|g| g.market_tokens.iter().find(|(c, _, _)| c == best_cid))
            .map(|(_, q, _)| q.clone())
            .unwrap_or_default();
        (best_cid.clone(), no_token, q)
    };

    info!(
        "T1 CONSENSUS: {} — {:.0}% consensus ({}/{} traders, {:.0} shares) on {} {} → buy {} No",
        event_slug, consensus_pct, best.1.traders.len(), n_total, consensus_shares,
        best_side, &best_cid[..best_cid.len().min(12)],
        &buy_cid[..buy_cid.len().min(12)],
    );

    Some(ConsensusResult {
        best_cid: best_cid.clone(),
        best_side: best_side.clone(),
        consensus_pct,
        consensus_shares,
        n_total_traders: n_total,
        buy_cid,
        buy_side: "No".to_string(),
        buy_token_id,
        buy_question,
    })
}
