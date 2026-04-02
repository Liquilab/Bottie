use anyhow::Result;
use tracing::{debug, info};

use crate::clob::client::ClobClient;
use crate::odds::PolymarketSportsMatch;
use std::sync::Arc;

/// Fetch active sports markets from Polymarket
pub async fn fetch_sports_markets(
    client: &Arc<ClobClient>,
    sports_tags: &[String],
) -> Result<Vec<PolymarketSportsMatch>> {
    let mut all_matches = Vec::new();
    let mut seen_tags = std::collections::HashSet::new();

    for tag in sports_tags {
        if !seen_tags.insert(tag) {
            continue; // skip duplicate tags (e.g. 11x "soccer")
        }
        let events = client.search_sports_events(tag).await?;
        debug!("found {} events for tag: {}", events.len(), tag);

        for event in &events {
            if let Some(markets) = &event.markets {
                for market in markets {
                    let condition_id = match &market.condition_id {
                        Some(id) => id.clone(),
                        None => continue,
                    };

                    // Try to get token IDs: first from tokens, fallback to clobTokenIds
                    let mut yes_token = String::new();
                    let mut no_token = String::new();

                    if let Some(tokens) = &market.tokens {
                        for token in tokens {
                            match token.outcome.as_deref() {
                                Some("Yes") => {
                                    yes_token = token.token_id.as_deref().unwrap_or("").to_string()
                                }
                                Some("No") => {
                                    no_token = token.token_id.as_deref().unwrap_or("").to_string()
                                }
                                _ => {}
                            }
                        }
                    }

                    // Fallback: clobTokenIds = ["yes_id", "no_id"]
                    // API returns this as a JSON string, not an array
                    if yes_token.is_empty() || no_token.is_empty() {
                        if let Some(clob_ids) = &market.clob_token_ids {
                            let parsed: Option<Vec<String>> = match clob_ids {
                                serde_json::Value::Array(arr) => {
                                    Some(arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                                }
                                serde_json::Value::String(s) => {
                                    serde_json::from_str(s).ok()
                                }
                                _ => None,
                            };
                            if let Some(ids) = parsed {
                                if ids.len() >= 2 {
                                    yes_token = ids[0].clone();
                                    no_token = ids[1].clone();
                                }
                            }
                        }
                    }

                    if yes_token.is_empty() || no_token.is_empty() {
                        continue;
                    }

                    // Parse prices from outcome_prices
                    let (yes_price, no_price) = parse_outcome_prices(&market.outcome_prices);

                    // Use market question as title (e.g. "Will Team X win?")
                    // Fall back to event title if question is missing
                    let market_title = market.question.as_deref()
                        .unwrap_or(event.title.as_deref().unwrap_or(""));
                    let event_title = event.title.as_deref().unwrap_or("");
                    let (team_a, team_b) = extract_teams(event_title);

                    all_matches.push(PolymarketSportsMatch {
                        title: market_title.to_string(),
                        condition_id,
                        yes_token_id: yes_token,
                        no_token_id: no_token,
                        yes_price,
                        no_price,
                        team_a,
                        team_b,
                        sport: tag.clone(),
                    });
                }
            }
        }
    }

    info!("fetched {} sports matches total", all_matches.len());
    Ok(all_matches)
}

fn parse_outcome_prices(prices: &Option<serde_json::Value>) -> (f64, f64) {
    match prices {
        Some(serde_json::Value::String(s)) => {
            // Format: "[\"0.55\",\"0.45\"]" or "0.55, 0.45"
            let cleaned = s.replace(['[', ']', '"', ' '], "");
            let parts: Vec<&str> = cleaned.split(',').collect();
            if parts.len() >= 2 {
                let yes = parts[0].parse().unwrap_or(0.5);
                let no = parts[1].parse().unwrap_or(0.5);
                (yes, no)
            } else {
                (0.5, 0.5)
            }
        }
        Some(serde_json::Value::Array(arr)) => {
            let yes = arr
                .first()
                .and_then(|v| v.as_str().and_then(|s| s.parse().ok()).or(v.as_f64()))
                .unwrap_or(0.5);
            let no = arr
                .get(1)
                .and_then(|v| v.as_str().and_then(|s| s.parse().ok()).or(v.as_f64()))
                .unwrap_or(0.5);
            (yes, no)
        }
        _ => (0.5, 0.5),
    }
}

fn extract_teams(title: &str) -> (String, String) {
    // Common patterns: "Team A vs Team B", "Team A v Team B", "Team A - Team B"
    let separators = [" vs ", " vs. ", " v ", " - ", " @ "];
    for sep in &separators {
        if let Some(idx) = title.to_lowercase().find(&sep.to_lowercase()) {
            let a = title[..idx].trim().to_string();
            let b = title[idx + sep.len()..].trim().to_string();
            // Remove trailing question marks or "to win" etc
            let b = b
                .trim_end_matches('?')
                .trim_end_matches(" to win")
                .trim()
                .to_string();
            if !a.is_empty() && !b.is_empty() {
                return (a, b);
            }
        }
    }
    (title.to_string(), String::new())
}
