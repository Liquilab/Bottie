use base64::engine::general_purpose::{STANDARD, URL_SAFE};
use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

pub fn build_hmac_signature(
    api_secret: &str,
    timestamp: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> anyhow::Result<String> {
    let secret_bytes = STANDARD
        .decode(api_secret)
        .or_else(|_| URL_SAFE.decode(api_secret))?;

    let mut message = format!("{timestamp}{method}{path}");
    if let Some(b) = body {
        if !b.is_empty() {
            message.push_str(b);
        }
    }

    let mut mac =
        HmacSha256::new_from_slice(&secret_bytes).map_err(|e| anyhow::anyhow!("HMAC key error: {e}"))?;
    mac.update(message.as_bytes());
    let result = mac.finalize().into_bytes();

    Ok(URL_SAFE.encode(result))
}

#[derive(Debug, Clone)]
pub struct L2Headers {
    pub poly_address: String,
    pub poly_signature: String,
    pub poly_timestamp: String,
    pub poly_api_key: String,
    pub poly_passphrase: String,
}

pub fn build_l2_headers(
    api_key: &str,
    api_secret: &str,
    api_passphrase: &str,
    signer_address: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> anyhow::Result<L2Headers> {
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        .to_string();

    let signature = build_hmac_signature(api_secret, &timestamp, method, path, body)?;

    Ok(L2Headers {
        poly_address: signer_address.to_string(),
        poly_signature: signature,
        poly_timestamp: timestamp,
        poly_api_key: api_key.to_string(),
        poly_passphrase: api_passphrase.to_string(),
    })
}

impl L2Headers {
    pub fn apply(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        builder
            .header("POLY_ADDRESS", &self.poly_address)
            .header("POLY_SIGNATURE", &self.poly_signature)
            .header("POLY_TIMESTAMP", &self.poly_timestamp)
            .header("POLY_API_KEY", &self.poly_api_key)
            .header("POLY_PASSPHRASE", &self.poly_passphrase)
    }
}

#[derive(Debug, Clone)]
pub struct L1Headers {
    pub poly_address: String,
    pub poly_signature: String,
    pub poly_timestamp: String,
    pub poly_nonce: String,
}

impl L1Headers {
    pub fn apply(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        builder
            .header("POLY_ADDRESS", &self.poly_address)
            .header("POLY_SIGNATURE", &self.poly_signature)
            .header("POLY_TIMESTAMP", &self.poly_timestamp)
            .header("POLY_NONCE", &self.poly_nonce)
    }
}
