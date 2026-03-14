use serde::{Deserialize, Serialize};

/// Order as submitted to CLOB API
#[derive(Debug, Clone, Serialize)]
pub struct ClobOrder {
    pub salt: u64,
    pub maker: String,
    pub signer: String,
    pub taker: String,
    #[serde(rename = "tokenId")]
    pub token_id: String,
    #[serde(rename = "makerAmount")]
    pub maker_amount: String,
    #[serde(rename = "takerAmount")]
    pub taker_amount: String,
    pub expiration: String,
    pub nonce: String,
    #[serde(rename = "feeRateBps")]
    pub fee_rate_bps: String,
    pub side: String,
    #[serde(rename = "signatureType")]
    pub signature_type: u8,
    pub signature: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PostOrderRequest {
    pub order: ClobOrder,
    pub owner: String,
    #[serde(rename = "orderType")]
    pub order_type: String,
    #[serde(rename = "postOnly")]
    pub post_only: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PostOrderResponse {
    #[serde(rename = "orderID")]
    pub order_id: Option<String>,
    pub id: Option<String>,
    pub status: Option<String>,
    pub skipped: Option<String>,
    #[serde(rename = "size_matched")]
    pub size_matched: Option<String>,
    #[serde(default)]
    pub success: Option<bool>,
    #[serde(default, rename = "errorMsg")]
    pub error_msg: Option<String>,
}

impl PostOrderResponse {
    pub fn is_rejected(&self) -> bool {
        self.skipped.is_some()
            || (self.order_id.is_none() && self.id.is_none())
            || self.success == Some(false)
    }

    pub fn effective_id(&self) -> Option<&str> {
        self.order_id.as_deref().or(self.id.as_deref())
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CancelOrderRequest {
    #[serde(rename = "orderID")]
    pub order_id: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FeeRateResponse {
    #[serde(rename = "fee_rate_bps")]
    pub fee_rate_bps: Option<serde_json::Value>,
}

/// Trade from the data-api (public trades feed)
/// Fields are serde_json::Value because the API returns mixed types
/// (numbers for size/price/timestamp, strings for others)
#[derive(Debug, Clone, Deserialize)]
pub struct DataApiTrade {
    pub id: Option<serde_json::Value>,
    #[serde(rename = "proxyWallet")]
    pub proxy_wallet: Option<String>,
    pub side: Option<String>,
    pub size: Option<serde_json::Value>,
    pub price: Option<serde_json::Value>,
    pub timestamp: Option<serde_json::Value>,
    pub title: Option<String>,
    pub slug: Option<String>,
    #[serde(rename = "eventSlug")]
    pub event_slug: Option<String>,
    pub outcome: Option<String>,
    #[serde(rename = "outcomeIndex")]
    pub outcome_index: Option<u32>,
    #[serde(rename = "conditionId")]
    pub condition_id: Option<String>,
    pub asset: Option<String>,
    #[serde(rename = "transactionHash")]
    pub transaction_hash: Option<String>,
    pub name: Option<String>,
    pub pseudonym: Option<String>,
}

impl DataApiTrade {
    pub fn price_f64(&self) -> f64 {
        match &self.price {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn size_f64(&self) -> f64 {
        match &self.size {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn timestamp_secs(&self) -> Option<i64> {
        match &self.timestamp {
            Some(serde_json::Value::Number(n)) => n.as_i64(),
            Some(serde_json::Value::String(s)) => s.parse().ok(),
            _ => None,
        }
    }

    pub fn trade_id(&self) -> Option<String> {
        // Prefer explicit id field, fall back to transactionHash
        match &self.id {
            Some(serde_json::Value::String(s)) if !s.is_empty() => return Some(s.clone()),
            Some(serde_json::Value::Number(n)) => return Some(n.to_string()),
            _ => {}
        }
        self.transaction_hash.clone()
    }
}

/// Activity entry from data-api/activity?user={address}
#[derive(Debug, Clone, Deserialize)]
pub struct WalletActivity {
    #[serde(rename = "proxyWallet")]
    pub proxy_wallet: Option<String>,
    pub timestamp: Option<serde_json::Value>,
    #[serde(rename = "conditionId")]
    pub condition_id: Option<String>,
    #[serde(rename = "type")]
    pub activity_type: Option<String>,
    #[serde(rename = "usdcSize")]
    pub usdc_size: Option<serde_json::Value>,
    #[serde(rename = "transactionHash")]
    pub transaction_hash: Option<String>,
    pub price: Option<serde_json::Value>,
    pub asset: Option<String>,
    pub side: Option<String>,
    pub outcome: Option<String>,
    pub title: Option<String>,
}

impl WalletActivity {
    pub fn price_f64(&self) -> f64 {
        match &self.price {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn usdc_size_f64(&self) -> f64 {
        match &self.usdc_size {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn timestamp_secs(&self) -> Option<i64> {
        match &self.timestamp {
            Some(serde_json::Value::Number(n)) => n.as_i64(),
            Some(serde_json::Value::String(s)) => s.parse().ok(),
            _ => None,
        }
    }

    pub fn is_buy(&self) -> bool {
        self.activity_type.as_deref() == Some("BUY")
            || self.side.as_deref() == Some("BUY")
    }
}

/// Orderbook from CLOB API
#[derive(Debug, Clone, Deserialize)]
pub struct BookLevel {
    pub price: serde_json::Value,
    pub size: serde_json::Value,
}

impl BookLevel {
    pub fn price_f64(&self) -> f64 {
        match &self.price {
            serde_json::Value::Number(n) => n.as_f64().unwrap_or(0.0),
            serde_json::Value::String(s) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct OrderBook {
    pub bids: Option<Vec<BookLevel>>,
    pub asks: Option<Vec<BookLevel>>,
}

impl OrderBook {
    pub fn best_ask(&self) -> Option<f64> {
        let ask = self.asks.as_ref()?
            .iter()
            .map(|l| l.price_f64())
            .filter(|&p| p > 0.0 && p < 1.0)
            .reduce(f64::min)?;
        Some(ask)
    }
}

/// Gamma API market status for resolution checking
#[derive(Debug, Clone, Deserialize)]
pub struct GammaMarketStatus {
    #[serde(rename = "conditionId")]
    pub condition_id: Option<String>,
    pub closed: Option<bool>,
    pub outcomes: Option<serde_json::Value>,
    #[serde(rename = "outcomePrices")]
    pub outcome_prices: Option<serde_json::Value>,
    #[serde(rename = "endDate")]
    pub end_date: Option<String>,
}

impl GammaMarketStatus {
    /// Returns the winning outcome name if the market is closed and resolved.
    pub fn winning_outcome(&self) -> Option<String> {
        if self.closed != Some(true) {
            return None;
        }
        let outcomes = self.parse_string_array(&self.outcomes)?;
        let prices = self.parse_string_array(&self.outcome_prices)?;
        for (outcome, price_str) in outcomes.iter().zip(prices.iter()) {
            let p: f64 = price_str.parse().unwrap_or(0.0);
            if p > 0.99 {
                return Some(outcome.clone());
            }
        }
        None
    }

    fn parse_string_array(&self, val: &Option<serde_json::Value>) -> Option<Vec<String>> {
        match val {
            Some(serde_json::Value::String(s)) => serde_json::from_str(s).ok(),
            Some(serde_json::Value::Array(arr)) => Some(
                arr.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect(),
            ),
            _ => None,
        }
    }
}

/// Gamma API event response for market discovery
#[derive(Debug, Clone, Deserialize)]
pub struct GammaEventResponse {
    pub markets: Option<Vec<GammaMarketResponse>>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GammaMarketResponse {
    #[serde(rename = "conditionId")]
    pub condition_id: Option<String>,
    #[serde(rename = "clobTokenIds")]
    pub clob_token_ids: Option<serde_json::Value>,
    pub tokens: Option<Vec<GammaTokenResponse>>,
    #[serde(rename = "outcomePrices")]
    pub outcome_prices: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GammaTokenResponse {
    pub outcome: Option<String>,
    pub token_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ClobMarketResponse {
    pub tokens: Option<Vec<ClobTokenResponse>>,
    pub condition_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ClobTokenResponse {
    pub outcome: Option<String>,
    pub token_id: Option<String>,
}

/// Position from data-api/positions?user={address}
/// Used for snapshot-diff based copy trading (replaces unreliable /activity endpoint)
#[derive(Debug, Clone, Deserialize)]
pub struct WalletPosition {
    pub asset: Option<String>,
    #[serde(rename = "conditionId")]
    pub condition_id: Option<String>,
    pub title: Option<String>,
    pub slug: Option<String>,
    pub outcome: Option<String>,
    pub size: Option<serde_json::Value>,
    #[serde(rename = "avgPrice")]
    pub avg_price: Option<serde_json::Value>,
    #[serde(rename = "currentValue")]
    pub current_value: Option<serde_json::Value>,
    #[serde(rename = "cashPnl")]
    pub cash_pnl: Option<serde_json::Value>,
    #[serde(rename = "proxyWallet")]
    pub proxy_wallet: Option<String>,
    #[serde(rename = "curPrice")]
    pub cur_price: Option<serde_json::Value>,
    #[serde(rename = "initialValue")]
    pub initial_value: Option<serde_json::Value>,
}

impl WalletPosition {
    pub fn size_f64(&self) -> f64 {
        match &self.size {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn avg_price_f64(&self) -> f64 {
        match &self.avg_price {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    pub fn initial_value_f64(&self) -> f64 {
        match &self.initial_value {
            Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    /// Unique key for this position (conditionId:outcome)
    pub fn position_key(&self) -> String {
        format!(
            "{}:{}",
            self.condition_id.as_deref().unwrap_or(""),
            self.outcome.as_deref().unwrap_or("")
        )
    }
}

/// Sports event from Gamma API
#[derive(Debug, Clone, Deserialize)]
pub struct GammaSportsEvent {
    pub id: Option<String>,
    pub slug: Option<String>,
    pub title: Option<String>,
    pub description: Option<String>,
    #[serde(rename = "startDate")]
    pub start_date: Option<String>,
    #[serde(rename = "endDate")]
    pub end_date: Option<String>,
    pub markets: Option<Vec<GammaMarketResponse>>,
    pub tags: Option<Vec<GammaTag>>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GammaTag {
    pub id: Option<String>,
    pub label: Option<String>,
    pub slug: Option<String>,
}
