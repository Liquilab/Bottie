//! Sync module: imports manual trades from the Polymarket UI into the bot's trade log,
//! and syncs bankroll with on-chain USDC balance.

use std::collections::HashSet;
use std::sync::Arc;

use anyhow::Result;
use chrono::DateTime;
use tracing::{info, warn};

use crate::clob::client::ClobClient;
use crate::copy_trader::CopyTrader;
use crate::logger::{TradeLog, TradeLogger};

/// Sync trades from own wallet activity into the trade log.
/// Detects trades not already in the log (by transaction_hash) and imports them
/// as signal_source="manual". This ensures manual PM UI trades show up in
/// PnL tracking, autoresearch, and the dashboard.
pub async fn sync_own_trades(
    client: &ClobClient,
    logger: &TradeLogger,
    own_address: &str,
) -> Result<u32> {
    // Load existing tx hashes from trade log for dedup
    let existing_trades = logger.load_all();
    let known_txs: HashSet<String> = existing_trades
        .iter()
        .filter_map(|t| t.order_id.clone())
        .collect();
    // Also collect tx hashes stored in a dedicated field if present
    let known_conditions: HashSet<String> = existing_trades
        .iter()
        .filter(|t| t.filled && !t.dry_run)
        .map(|t| format!("{}:{}", t.condition_id, t.outcome.to_lowercase()))
        .collect();

    // Fetch recent activity for our own wallet (up to 500 trades)
    let activities = client.get_wallet_activity(own_address, 500).await?;

    let mut imported = 0u32;

    for act in &activities {
        // Only import BUY trades (we track positions, not sells)
        if !act.is_buy() {
            continue;
        }

        let tx_hash = match &act.transaction_hash {
            Some(h) if !h.is_empty() => h.clone(),
            _ => continue,
        };

        // Skip if we already know this trade (by tx hash in order_id)
        if known_txs.contains(&tx_hash) {
            continue;
        }

        let condition_id = act.condition_id.clone().unwrap_or_default();
        let outcome = act.outcome.clone().unwrap_or_default();
        let price = act.price_f64();
        let usdc_size = act.usdc_size_f64();

        if condition_id.is_empty() || outcome.is_empty() || price <= 0.0 || usdc_size <= 0.0 {
            continue;
        }

        // Skip if we already have a position on this condition+outcome
        // (the bot may have placed this trade itself with a different tx format)
        let pos_key = format!("{}:{}", condition_id, outcome.to_lowercase());
        if known_conditions.contains(&pos_key) {
            continue;
        }

        // Skip very recent trades (< 10 min old) — these are likely trades the bot
        // just placed itself. Execution.rs already logged them with proper consensus_count.
        // Importing them again as "manual" would lose the consensus metadata.
        if let Some(secs) = act.timestamp_secs() {
            let age_mins = (chrono::Utc::now().timestamp() - secs) / 60;
            if age_mins < 10 {
                continue;
            }
        }

        let trade_time = match act.timestamp_secs() {
            Some(secs) => DateTime::from_timestamp(secs, 0).unwrap_or(chrono::Utc::now()),
            None => chrono::Utc::now(),
        };

        let title = act.title.clone().unwrap_or_else(|| {
            condition_id[..12.min(condition_id.len())].to_string()
        });
        let sport = CopyTrader::detect_sport_static(&title, "");
        let size_shares = usdc_size / price;
        let asset = act.asset.clone().unwrap_or_default();

        info!(
            "SYNC: importing manual trade | {} {} @ {:.0}ct = ${:.2}",
            title, outcome, price * 100.0, usdc_size
        );

        logger.log(TradeLog {
            timestamp: trade_time,
            token_id: asset,
            condition_id,
            market_title: title,
            sport,
            side: "BUY".to_string(),
            outcome,
            event_slug: None,
            price,
            size_usdc: usdc_size,
            size_shares,
            signal_source: "manual".to_string(),
            copy_wallet: None,
            consensus_count: None,
            consensus_wallets: None,
            edge_pct: 0.0,
            confidence: 0.0,
            signal_delay_ms: 0,
            order_id: Some(tx_hash),
            filled: true,
            dry_run: false,
            result: None,
            pnl: None,
            resolved_at: None,
            sell_price: None,
            actual_pnl: None,
            exit_type: None,
            strategy_version: None,
        });

        imported += 1;
    }

    if imported > 0 {
        info!("SYNC: imported {} manual trades from wallet", imported);
    } else {
        info!("SYNC: no new manual trades to import");
    }

    Ok(imported)
}

/// Sync bankroll with on-chain USDC balance + positions value.
/// Returns the total portfolio value (cash + open positions).
pub async fn sync_bankroll(client: &ClobClient) -> Result<f64> {
    let cash = client.get_usdc_balance().await?;

    // Get positions value from data-api /value endpoint
    let funder = client.funder_address();
    let positions_value = match client.get_positions_value(&funder).await {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!("positions value sync failed, using cash only: {e}");
            0.0
        }
    };

    let total = cash + positions_value;
    info!("SYNC: cash=${:.2} + positions=${:.2} = portfolio=${:.2}", cash, positions_value, total);
    Ok(total)
}
