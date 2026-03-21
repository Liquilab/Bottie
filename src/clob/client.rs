use std::time::{SystemTime, UNIX_EPOCH};

use alloy_primitives::{Address, U256};
use alloy_signer_local::PrivateKeySigner;
use anyhow::{Context, Result};
use rand::Rng;
use reqwest::Client;
use tracing::{debug, warn};

use crate::config::{BotConfig, CLOB_API, CTF_DECIMAL_FACTOR, DATA_API, GAMMA_API, ZERO_ADDRESS};
use crate::signing::eip712::{sign_clob_auth, sign_order, ClobAuthData, OrderData};
use crate::signing::hmac_auth::{build_l2_headers, L1Headers};

use super::types::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn as_u8(&self) -> u8 {
        match self {
            Side::Buy => 0,
            Side::Sell => 1,
        }
    }
}

impl std::fmt::Display for Side {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Side::Buy => write!(f, "BUY"),
            Side::Sell => write!(f, "SELL"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderType {
    GTC,
    GTD,
    FOK,
}

impl std::fmt::Display for OrderType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderType::GTC => write!(f, "GTC"),
            OrderType::GTD => write!(f, "GTD"),
            OrderType::FOK => write!(f, "FOK"),
        }
    }
}

pub struct ClobClient {
    http: Client,
    signer: PrivateKeySigner,
    config: BotConfig,
}

impl ClobClient {
    pub fn new(signer: PrivateKeySigner, config: BotConfig) -> Self {
        let http = Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .build()
            .expect("failed to build HTTP client");

        Self {
            http,
            signer,
            config,
        }
    }

    fn signer_address(&self) -> String {
        format!("{}", self.signer.address())
    }

    pub fn funder_address(&self) -> String {
        format!("{}", self.config.funder)
    }

    fn timestamp() -> String {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string()
    }

    fn to_token_decimals(amount: f64) -> U256 {
        let raw = (amount * CTF_DECIMAL_FACTOR).round() as u128;
        U256::from(raw)
    }

    fn generate_salt() -> u64 {
        let mut rng = rand::thread_rng();
        rng.gen::<u32>() as u64
    }

    fn l2_request(
        &self,
        builder: reqwest::RequestBuilder,
        method: &str,
        path: &str,
        body: Option<&str>,
    ) -> Result<reqwest::RequestBuilder> {
        let headers = build_l2_headers(
            &self.config.api_key,
            &self.config.api_secret,
            &self.config.api_passphrase,
            &self.signer_address(),
            method,
            path,
            body,
        )?;
        Ok(headers.apply(builder))
    }

    // --- API Key Derivation ---

    pub async fn derive_api_key(&self) -> Result<ApiCredentials> {
        let timestamp = Self::timestamp();
        let nonce = U256::ZERO;

        let auth = ClobAuthData {
            address: self.signer.address(),
            timestamp: timestamp.clone(),
            nonce,
        };

        let signature = sign_clob_auth(&self.signer, &auth).await?;

        let l1 = L1Headers {
            poly_address: self.signer_address(),
            poly_signature: signature,
            poly_timestamp: timestamp,
            poly_nonce: "0".to_string(),
        };

        let resp = l1
            .apply(self.http.get(format!("{CLOB_API}/auth/derive-api-key")))
            .send()
            .await?
            .error_for_status()?
            .json::<ApiCredentials>()
            .await?;

        Ok(resp)
    }

    // --- Fee Rate ---

    pub async fn get_fee_rate_bps(&self, token_id: &str) -> Result<u32> {
        let path = format!("/fee-rate?token_id={token_id}");
        let url = format!("{CLOB_API}{path}");

        let builder = self.http.get(&url);
        let builder = self.l2_request(builder, "GET", &path, None)?;

        let resp: FeeRateResponse = builder.send().await?.error_for_status()?.json().await?;

        let bps = match resp.fee_rate_bps {
            Some(serde_json::Value::Number(n)) => n.as_u64().unwrap_or(0) as u32,
            Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0),
            _ => 0,
        };

        Ok(bps)
    }

    // --- Order Placement ---

    pub async fn create_and_post_order(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
        side: Side,
        order_type: OrderType,
        fee_rate_bps: u32,
    ) -> Result<PostOrderResponse> {
        let sig_type: u8 = 2; // Gnosis Safe

        // Round price to tick size (0.01) and size to 2 decimals.
        // Micro-amounts are computed directly without intermediate rounding
        // to match the CLOB API's expected precision (6 decimal places).
        let price = (price * 100.0).round() / 100.0;
        let size = (size * 100.0).round() / 100.0;

        let (maker_amount, taker_amount) = match side {
            Side::Buy => {
                // maker_amount = price * size, rounded at micro-amount level (6 decimals)
                let maker_raw = (price * size * CTF_DECIMAL_FACTOR).round() as u128;
                let taker_raw = (size * CTF_DECIMAL_FACTOR).round() as u128;
                (U256::from(maker_raw), U256::from(taker_raw))
            }
            Side::Sell => {
                let maker_raw = (size * CTF_DECIMAL_FACTOR).round() as u128;
                let taker_raw = (price * size * CTF_DECIMAL_FACTOR).round() as u128;
                (U256::from(maker_raw), U256::from(taker_raw))
            }
        };

        let salt = Self::generate_salt();
        let taker_addr: Address = ZERO_ADDRESS.parse().unwrap();

        let order_data = OrderData {
            salt: U256::from(salt),
            maker: self.config.funder,
            signer: self.signer.address(),
            taker: taker_addr,
            token_id: U256::from_str_radix(token_id, 10)
                .or_else(|_| {
                    let stripped = token_id.strip_prefix("0x").unwrap_or(token_id);
                    U256::from_str_radix(stripped, 16)
                })
                .context("invalid token_id")?,
            maker_amount,
            taker_amount,
            expiration: U256::ZERO,
            nonce: U256::ZERO,
            fee_rate_bps: U256::from(fee_rate_bps),
            side: side.as_u8(),
            signature_type: sig_type,
        };

        // Try signing with neg_risk=true first (sports/neg-risk markets are the common case).
        // If the API returns "invalid signature", retry with neg_risk=false (standard exchange).
        let signature = sign_order(&self.signer, &order_data, true).await?;

        let make_clob_order = |sig: String, _neg_risk_flag: bool| ClobOrder {
            salt,
            maker: format!("{}", self.config.funder),
            signer: self.signer_address(),
            taker: ZERO_ADDRESS.to_string(),
            token_id: token_id.to_string(),
            // JSON body uses raw micro-amounts as integer strings ("5450000"), same as EIP712
            maker_amount: maker_amount.to_string(),
            taker_amount: taker_amount.to_string(),
            expiration: "0".to_string(),
            nonce: "0".to_string(),
            fee_rate_bps: fee_rate_bps.to_string(),
            side: side.to_string(),
            signature_type: sig_type,
            signature: sig,
        };


        // Post order
        let path = "/order";

        let post_order = |clob_order: ClobOrder| {
            let req = PostOrderRequest {
                order: clob_order,
                owner: self.config.api_key.clone(),
                order_type: order_type.to_string(),
                post_only: false,
            };
            serde_json::to_string(&req)
        };

        let body = post_order(make_clob_order(signature, false))?;
        debug!("POST /order body: {}", &body[..body.len().min(500)]);

        let builder = self.http.post(format!("{CLOB_API}{path}"));
        let builder = self.l2_request(builder, "POST", path, Some(&body))?;
        let resp = builder
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;

        let status = resp.status();
        let text = resp.text().await?;

        // If invalid signature, retry with opposite neg_risk setting (standard exchange)
        if !status.is_success() && text.contains("invalid signature") {
            debug!("retrying order with standard exchange (neg_risk=false)");
            let sig2 = sign_order(&self.signer, &order_data, false).await?;
            let body2 = post_order(make_clob_order(sig2, true))?;
            let builder2 = self.http.post(format!("{CLOB_API}{path}"));
            let builder2 = self.l2_request(builder2, "POST", path, Some(&body2))?;
            let resp2 = builder2
                .header("Content-Type", "application/json")
                .body(body2)
                .send()
                .await?;
            let status2 = resp2.status();
            let text2 = resp2.text().await?;
            if !status2.is_success() {
                warn!("POST /order failed ({status2}): {text2}");
                anyhow::bail!("POST /order failed: {status2} {text2}");
            }
            let parsed: PostOrderResponse = serde_json::from_str(&text2)
                .with_context(|| format!("failed to parse order response: {text2}"))?;
            if parsed.is_rejected() {
                warn!("Order rejected: {}", parsed.skipped.as_deref().unwrap_or("unknown"));
            }
            return Ok(parsed);
        }

        if !status.is_success() {
            warn!("POST /order failed ({status}): {text}");
            anyhow::bail!("POST /order failed: {status} {text}");
        }

        let parsed: PostOrderResponse = serde_json::from_str(&text)
            .with_context(|| format!("failed to parse order response: {text}"))?;

        if parsed.is_rejected() {
            warn!("Order rejected: {}", parsed.skipped.as_deref().unwrap_or("unknown"));
        }

        // Log raw response to diagnose size_matched availability
        debug!("CLOB response: size_matched={:?} order_id={:?} status={:?}",
               parsed.size_matched, parsed.effective_id(), parsed.status);

        Ok(parsed)
    }

    // --- Data API: Public Trades Feed ---

    pub async fn get_recent_trades(&self, limit: u32) -> Result<Vec<DataApiTrade>> {
        let url = format!("{DATA_API}/trades?limit={limit}");
        let trades: Vec<DataApiTrade> = self
            .http
            .get(&url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(trades)
    }

    pub async fn get_recent_trades_offset(&self, limit: u32, offset: u32) -> Result<Vec<DataApiTrade>> {
        let url = format!("{DATA_API}/trades?limit={limit}&offset={offset}");
        let trades: Vec<DataApiTrade> = self
            .http
            .get(&url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(trades)
    }

    pub async fn get_trades_for_wallet(&self, wallet: &str, limit: u32) -> Result<Vec<DataApiTrade>> {
        // Query both maker and taker — wallets can be either depending on order type
        let mut all_trades = Vec::new();

        // Maker trades
        let url = format!("{DATA_API}/trades?maker={wallet}&limit={limit}");
        if let Ok(resp) = self.http.get(&url).send().await {
            if let Ok(trades) = resp.json::<Vec<DataApiTrade>>().await {
                all_trades.extend(trades);
            }
        }

        // Taker trades (critical: FOK orders make the wallet a taker)
        let url = format!("{DATA_API}/trades?taker={wallet}&limit={limit}");
        if let Ok(resp) = self.http.get(&url).send().await {
            if let Ok(trades) = resp.json::<Vec<DataApiTrade>>().await {
                all_trades.extend(trades);
            }
        }

        // Sort by timestamp descending (most recent first)
        all_trades.sort_by(|a, b| {
            b.timestamp_secs().unwrap_or(0).cmp(&a.timestamp_secs().unwrap_or(0))
        });

        // Dedup by trade ID
        let mut seen = std::collections::HashSet::new();
        all_trades.retain(|t| {
            t.trade_id().map_or(true, |id| seen.insert(id))
        });

        Ok(all_trades)
    }

    // --- Per-wallet Activity (DEPRECATED: unreliable, use get_wallet_positions instead) ---

    pub async fn get_wallet_activity(&self, address: &str, limit: u32) -> Result<Vec<WalletActivity>> {
        let url = format!("{DATA_API}/activity?user={address}&limit={limit}");
        let resp = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        Ok(resp)
    }

    // --- Per-wallet Positions (reliable, snapshot-diff for copy trading) ---

    pub async fn get_wallet_positions(&self, address: &str, limit: u32) -> Result<Vec<WalletPosition>> {
        // Paginate to get ALL positions above threshold (not just first page)
        let mut all: Vec<WalletPosition> = Vec::new();
        let mut offset: u32 = 0;
        let page_size = limit.min(500);

        loop {
            let url = format!(
                "{DATA_API}/positions?user={address}&limit={page_size}&sizeThreshold=0.01&sortBy=CURRENT&sortOrder=desc&offset={offset}"
            );
            let page: Vec<WalletPosition> = self.http.get(&url).send().await?.error_for_status()?.json().await?;
            let count = page.len() as u32;
            all.extend(page);

            if count < page_size {
                break; // last page
            }
            offset += page_size;

            // Safety: max 20 pages (10,000 positions)
            if offset >= 10_000 {
                break;
            }
        }

        Ok(all)
    }

    // --- Portfolio Value ---

    /// Get total value of open positions from data-api /value endpoint.
    /// Returns positions value in USDC (does NOT include cash).
    pub async fn get_positions_value(&self, address: &str) -> Result<f64> {
        let url = format!("{DATA_API}/value?user={address}");
        let resp: serde_json::Value = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        // Response: [{"user": "0x...", "value": 123.45}]
        let value = resp.as_array()
            .and_then(|arr| arr.first())
            .and_then(|obj| obj.get("value"))
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        Ok(value)
    }

    // --- Order Status ---

    /// Get order status from CLOB API.
    /// Returns (status, size_matched) where status is one of:
    /// ORDER_STATUS_LIVE, ORDER_STATUS_MATCHED, ORDER_STATUS_CANCELED,
    /// ORDER_STATUS_INVALID, ORDER_STATUS_CANCELED_MARKET_RESOLVED
    pub async fn get_order_status(&self, order_id: &str) -> Result<(String, f64)> {
        let path = format!("/order/{order_id}");
        let req = self.l2_request(
            self.http.get(format!("{CLOB_API}{path}")),
            "GET",
            &path,
            None,
        )?;
        let resp: serde_json::Value = req.send().await?.error_for_status()?.json().await?;
        let status = resp.get("status")
            .and_then(|v| v.as_str())
            .unwrap_or("UNKNOWN")
            .to_string();
        let size_matched = resp.get("size_matched")
            .and_then(|v| v.as_str())
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.0);
        Ok((status, size_matched))
    }

    // --- Current best ask price ---

    /// Fetch the current best ask for a token from the CLOB orderbook.
    /// Returns the lowest available ask price. Falls back to Err if no liquidity.
    pub async fn get_best_ask(&self, token_id: &str) -> Result<f64> {
        let url = format!("{CLOB_API}/book?token_id={token_id}");
        let book: OrderBook = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        book.best_ask().ok_or_else(|| anyhow::anyhow!("no asks in orderbook for {token_id}"))
    }

    pub async fn get_best_bid(&self, token_id: &str) -> Result<f64> {
        let url = format!("{CLOB_API}/book?token_id={token_id}");
        let book: OrderBook = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        book.best_bid().ok_or_else(|| anyhow::anyhow!("no bids in orderbook for {token_id}"))
    }

    // --- Market resolution status ---

    /// Check if a market has resolved and return the winning outcome name.
    pub async fn get_market_info(&self, condition_id: &str) -> Result<Option<GammaMarketStatus>> {
        let url = format!("{GAMMA_API}/markets?condition_ids={condition_id}");
        let resp: Vec<GammaMarketStatus> = self
            .http
            .get(&url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(resp.into_iter().next())
    }

    // --- Market Discovery ---

    pub async fn find_market_tokens(
        &self,
        condition_id: &str,
    ) -> Result<(String, String)> {
        let url = format!("{CLOB_API}/markets/{condition_id}");
        let resp: ClobMarketResponse = self
            .http
            .get(&url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;

        let tokens = resp.tokens.context("no tokens in market")?;
        let mut yes_token = String::new();
        let mut no_token = String::new();

        for t in &tokens {
            let outcome = t.outcome.as_deref().unwrap_or("");
            let token_id = t.token_id.as_deref().unwrap_or("");
            match outcome {
                "Yes" => yes_token = token_id.to_string(),
                "No" => no_token = token_id.to_string(),
                _ => {}
            }
        }

        if yes_token.is_empty() || no_token.is_empty() {
            anyhow::bail!("could not find Yes/No tokens for condition {condition_id}");
        }

        Ok((yes_token, no_token))
    }

    // --- Sports Market Search ---

    pub async fn search_sports_events(&self, tag: &str) -> Result<Vec<GammaSportsEvent>> {
        let url = format!(
            "{GAMMA_API}/events?active=true&closed=false&tag={tag}&limit=100"
        );
        let events: Vec<GammaSportsEvent> = self
            .http
            .get(&url)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(events)
    }

    // --- On-chain USDC balance ---

    /// Fetch live USDC.e balance of the funder wallet from Polygon RPC.
    /// Uses eth_call balanceOf on the USDC.e contract (6 decimals).
    pub async fn get_usdc_balance(&self) -> Result<f64> {
        const USDC_CONTRACT: &str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
        const POLYGON_RPC: &str = "https://polygon-bor-rpc.publicnode.com";

        let addr = format!("{}", self.config.funder);
        let padded = format!("{:0>64}", addr.trim_start_matches("0x").to_lowercase());
        let data = format!("0x70a08231{}", padded);

        let payload = serde_json::json!({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": data}, "latest"],
            "id": 1
        });

        let resp: serde_json::Value = self
            .http
            .post(POLYGON_RPC)
            .json(&payload)
            .send()
            .await?
            .json()
            .await?;

        let hex = resp["result"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("no result from RPC"))?;
        let hex = hex.trim_start_matches("0x");
        let raw = u128::from_str_radix(hex, 16).unwrap_or(0);
        Ok(raw as f64 / 1_000_000.0)
    }

    // --- Redeem resolved positions ---

    /// Redeem winning conditional tokens back to USDC after market resolution.
    pub async fn redeem_position(&self, condition_id: &str) -> Result<()> {
        let path = "/redeem";
        let body = serde_json::json!({ "conditionId": condition_id }).to_string();
        let builder = self.http.post(format!("{CLOB_API}{path}"));
        let builder = self.l2_request(builder, "POST", path, Some(&body))?;
        let resp = builder
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;

        let status = resp.status();
        let text = resp.text().await?;
        if !status.is_success() {
            anyhow::bail!("redeem failed ({status}): {text}");
        }
        Ok(())
    }

    pub fn is_dry_run(&self) -> bool {
        self.config.dry_run
    }
}

/// API credentials returned from derive-api-key
#[derive(Debug, Clone, serde::Deserialize)]
pub struct ApiCredentials {
    #[serde(rename = "apiKey")]
    pub api_key: String,
    pub secret: String,
    pub passphrase: String,
}
