use std::time::{SystemTime, UNIX_EPOCH};

use alloy_primitives::{Address, U256};
use alloy_signer_local::PrivateKeySigner;
use anyhow::{Context, Result};
use rand::Rng;
use reqwest::Client;
use tracing::{debug, info, warn};

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

        let bps = match resp.base_fee {
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
        self.create_and_post_order_inner(token_id, price, size, side, order_type, fee_rate_bps, false).await
    }

    pub async fn create_and_post_order_post_only(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
        side: Side,
        order_type: OrderType,
        fee_rate_bps: u32,
    ) -> Result<PostOrderResponse> {
        self.create_and_post_order_inner(token_id, price, size, side, order_type, fee_rate_bps, true).await
    }

    async fn create_and_post_order_inner(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
        side: Side,
        order_type: OrderType,
        fee_rate_bps: u32,
        post_only: bool,
    ) -> Result<PostOrderResponse> {
        let sig_type: u8 = 2; // Gnosis Safe

        // Round price to tick size (0.01 = 1 cent).
        // The CLOB computes price from maker_amount/taker_amount.
        // To ensure the ratio lands on a tick, compute amounts from price_cents.
        let price_cents = (price * 100.0).ceil() as u64; // e.g. 57ct
        let price = price_cents as f64 / 100.0; // clean 0.57
        let size = (size * 100.0).floor() / 100.0; // round down shares

        let (maker_amount, taker_amount) = match side {
            Side::Buy => {
                // For a BUY at price P for S shares:
                // maker_amount (USDC) = price_cents * size_micro / 100
                // taker_amount (shares) = size_micro
                // This guarantees maker/taker = price exactly.
                let size_micro = (size * CTF_DECIMAL_FACTOR).round() as u128;
                let maker_micro = (price_cents as u128) * size_micro / 100;
                (U256::from(maker_micro), U256::from(size_micro))
            }
            Side::Sell => {
                let size_micro = (size * CTF_DECIMAL_FACTOR).round() as u128;
                let maker_micro = size_micro; // selling shares
                let taker_micro = (price_cents as u128) * size_micro / 100;
                (U256::from(maker_micro), U256::from(taker_micro))
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
                post_only,
            };
            serde_json::to_string(&req)
        };

        let body = post_order(make_clob_order(signature, false))?;
        debug!("POST /order body: {}", &body[..body.len().min(500)]);

        let (status, text) = {
            let mut last_status = reqwest::StatusCode::default();
            let mut last_text = String::new();
            for attempt in 0..4u32 {
                let builder = self.http.post(format!("{CLOB_API}{path}"));
                let builder = self.l2_request(builder, "POST", path, Some(&body))?;
                let resp = builder
                    .header("Content-Type", "application/json")
                    .body(body.clone())
                    .send()
                    .await?;
                last_status = resp.status();
                last_text = resp.text().await?;
                if last_status.is_success() || (last_status.as_u16() != 425 && last_status.as_u16() != 429) {
                    break;
                }
                let wait_secs = (attempt + 1) * 2;
                warn!("POST /order got {last_status}, retry {}/{} after {wait_secs}s", attempt + 1, 3);
                tokio::time::sleep(std::time::Duration::from_secs(wait_secs as u64)).await;
            }
            (last_status, last_text)
        };

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

        // Log raw response — visible in production for diagnosing NOT FILLED
        if !parsed.is_filled() {
            warn!("CLOB RAW: {}", text);
        } else {
            info!("CLOB response: size_matched={:?} order_id={:?} status={:?}",
                   parsed.size_matched, parsed.effective_id(), parsed.status);
        }

        Ok(parsed)
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
                "{DATA_API}/positions?user={address}&limit={page_size}&sizeThreshold=0.1&sortBy=CURRENT&sortOrder=desc&offset={offset}"
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

    /// Fetch (condition_id, event_slug) pairs from /closed-positions.
    /// Used by sync to resolve slugs of resolved markets (where /positions no
    /// longer has the row). Paginates with a 20-page safety cap.
    pub async fn get_closed_position_slugs(&self, address: &str) -> Result<Vec<(String, String)>> {
        let mut out: Vec<(String, String)> = Vec::new();
        let mut offset: u32 = 0;
        let page_size: u32 = 50;
        for _ in 0..20 {
            let url = format!(
                "{DATA_API}/closed-positions?user={address}&limit={page_size}&offset={offset}"
            );
            let page: serde_json::Value = self.http.get(&url).send().await?.error_for_status()?.json().await?;
            let arr = match page.as_array() {
                Some(a) => a,
                None => break,
            };
            if arr.is_empty() {
                break;
            }
            for row in arr {
                let cid = row.get("conditionId").and_then(|v| v.as_str()).unwrap_or("");
                let slug = row.get("eventSlug").and_then(|v| v.as_str()).unwrap_or("");
                if !cid.is_empty() && !slug.is_empty() {
                    out.push((cid.to_string(), slug.trim_end_matches("-more-markets").to_string()));
                }
            }
            if (arr.len() as u32) < page_size {
                break;
            }
            offset += page_size;
        }
        Ok(out)
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

    // --- Cancel order ---

    pub async fn cancel_order(&self, order_id: &str) -> Result<()> {
        let path = "/order";
        let body = serde_json::json!({ "orderID": order_id }).to_string();
        let req = self.l2_request(
            self.http.delete(format!("{CLOB_API}{path}")),
            "DELETE",
            path,
            Some(&body),
        )?;
        req.header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    // --- Current best ask price ---

    /// Fetch the current best ask for a token from the CLOB orderbook.
    /// Returns the lowest available ask price. Falls back to Err if no liquidity.
    pub async fn get_best_ask(&self, token_id: &str) -> Result<f64> {
        let url = format!("{CLOB_API}/book?token_id={token_id}");
        let book: OrderBook = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        book.best_ask().ok_or_else(|| anyhow::anyhow!("no asks in orderbook for {token_id}"))
    }

    /// Returns (best_ask_price, shares_at_best_ask)
    pub async fn get_best_ask_with_depth(&self, token_id: &str) -> Result<(f64, f64)> {
        let url = format!("{CLOB_API}/book?token_id={token_id}");
        let book: OrderBook = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        book.best_ask_with_depth().ok_or_else(|| anyhow::anyhow!("no asks in orderbook for {token_id}"))
    }

    pub async fn get_best_bid(&self, token_id: &str) -> Result<f64> {
        let url = format!("{CLOB_API}/book?token_id={token_id}");
        let book: OrderBook = self.http.get(&url).send().await?.error_for_status()?.json().await?;
        book.best_bid().ok_or_else(|| anyhow::anyhow!("no bids in orderbook for {token_id}"))
    }

    /// Fetch full orderbook for a token.
    pub async fn get_orderbook(&self, token_id: &str) -> Result<OrderBook> {
        let url = format!("{CLOB_API}/book?token_id={token_id}");
        Ok(self.http.get(&url).send().await?.error_for_status()?.json().await?)
    }

    // --- Market resolution status ---

    /// Check if a market has resolved and return the winning outcome name.
    /// Uses CLOB API (authoritative for resolution) with Gamma API fallback.
    pub async fn get_market_info(&self, condition_id: &str) -> Result<Option<GammaMarketStatus>> {
        // Primary: CLOB API — always has correct condition_id mapping
        let clob_url = format!("{CLOB_API}/markets/{condition_id}");
        match self.http.get(&clob_url).send().await {
            Ok(resp) if resp.status().is_success() => {
                if let Ok(clob) = resp.json::<ClobMarketStatusResponse>().await {
                    return Ok(Some(clob.into_gamma_status()));
                }
            }
            _ => {}
        }

        // Fallback: Gamma API (works for some older markets)
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

    // --- Sports Market Search ---

    pub async fn search_sports_events(&self, tag_slug: &str) -> Result<Vec<GammaSportsEvent>> {
        let now = chrono::Utc::now();
        let end_date_min = now.format("%Y-%m-%dT%H:%M:%SZ");
        // Paginate: soccer alone has 500+ events, limit=100 missed bra/es2/efa leagues.
        let mut all_events: Vec<GammaSportsEvent> = Vec::new();
        let mut offset = 0usize;
        const PAGE: usize = 500;
        loop {
            let url = format!(
                "{GAMMA_API}/events?active=true&closed=false&tag_slug={tag_slug}&end_date_min={end_date_min}&limit={PAGE}&offset={offset}"
            );
            let batch: Vec<GammaSportsEvent> = self
                .http
                .get(&url)
                .send()
                .await?
                .error_for_status()?
                .json()
                .await?;
            let n = batch.len();
            all_events.extend(batch);
            if n < PAGE {
                break;
            }
            offset += PAGE;
        }
        Ok(all_events)
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
