use anyhow::Result;
use serde::Deserialize;
use std::collections::HashMap;
use tracing::warn;

const DEFAULT_SUBGRAPH_URL: &str = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions-subgraph/0.0.7/gn";

pub struct SubgraphClient {
    http: reqwest::Client,
    url: String,
    delay_ms: u64,
}

#[derive(Debug, Clone)]
pub struct SubgraphHolder {
    pub user: String,
    pub balance: f64,
    pub outcome_index: u8,
}

#[derive(Deserialize)]
struct GqlResponse {
    data: Option<GqlData>,
    errors: Option<Vec<GqlError>>,
}

#[derive(Deserialize)]
struct GqlData {
    #[serde(rename = "userBalances")]
    user_balances: Vec<GqlUserBalance>,
}

#[derive(Deserialize)]
struct GqlUserBalance {
    user: String,
    balance: String,
    asset: GqlAsset,
}

#[derive(Deserialize)]
struct GqlAsset {
    #[serde(rename = "outcomeIndex")]
    outcome_index: String,
}

#[derive(Deserialize)]
struct GqlError {
    message: String,
}

impl SubgraphClient {
    pub fn new(url: Option<&str>, delay_ms: u64) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .expect("http client"),
            url: url.unwrap_or(DEFAULT_SUBGRAPH_URL).to_string(),
            delay_ms,
        }
    }

    pub async fn top_holders(&self, condition_id: &str, n: u32) -> Result<Vec<SubgraphHolder>> {
        let query = format!(
            r#"{{ userBalances(first: {n}, orderBy: balance, orderDirection: desc, where: {{ asset_: {{ condition: "{condition_id}" }} }}) {{ user balance asset {{ outcomeIndex }} }} }}"#
        );

        let body = serde_json::json!({ "query": query });

        let resp: GqlResponse = self.http
            .post(&self.url)
            .header("Content-Type", "application/json")
            .header("User-Agent", "Bottie/1")
            .json(&body)
            .send()
            .await?
            .json()
            .await?;

        if let Some(errors) = &resp.errors {
            let msg = errors.iter().map(|e| e.message.as_str()).collect::<Vec<_>>().join("; ");
            warn!("subgraph error for {}: {}", &condition_id[..condition_id.len().min(20)], msg);
            return Ok(vec![]);
        }

        let holders = match resp.data {
            Some(data) => data.user_balances.into_iter().map(|ub| {
                let balance = ub.balance.parse::<f64>().unwrap_or(0.0) / 1e6;
                let outcome_index = ub.asset.outcome_index.parse::<u8>().unwrap_or(0);
                SubgraphHolder {
                    user: ub.user,
                    balance,
                    outcome_index,
                }
            }).collect(),
            None => vec![],
        };

        if self.delay_ms > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(self.delay_ms)).await;
        }

        Ok(holders)
    }

    /// Fetch top holders for multiple condition IDs. Returns map of cid -> holders.
    pub async fn top_holders_batch(
        &self,
        condition_ids: &[String],
        n: u32,
    ) -> HashMap<String, Vec<SubgraphHolder>> {
        let mut result = HashMap::new();
        for cid in condition_ids {
            match self.top_holders(cid, n).await {
                Ok(holders) => { result.insert(cid.clone(), holders); }
                Err(e) => {
                    warn!("subgraph fetch failed for {}: {}", &cid[..cid.len().min(20)], e);
                }
            }
        }
        result
    }
}
