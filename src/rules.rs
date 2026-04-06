//! SSOT Pilaar 1 — Inkoopregels.
//!
//! Loads declarative trading rules from data/ssot/rules.yaml and exposes
//! them as a global singleton. Replaces hardcoded checks in main.rs and
//! scheduler.rs so that rule changes live in one place.

use serde::Deserialize;
use std::sync::OnceLock;
use tracing::info;

#[derive(Debug, Clone, Deserialize)]
pub struct RulesConfig {
    pub schema_version: u32,
    pub rules_version: String,
    pub last_updated: String,
    pub instance_id: String,
    pub win_yes_ban: WinYesBan,
    pub allowed_game_lines: Vec<String>,
    pub forbidden_slug_suffixes: Vec<String>,
    pub price_band: PriceBand,
    pub nba_min_price: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WinYesBan {
    pub enabled: bool,
    pub flip_to_opp_no: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PriceBand {
    pub win_no: Band,
    pub draw_no: Band,
    pub draw_yes: Band,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Band {
    pub min: Option<f64>,
    pub max: Option<f64>,
}

static RULES: OnceLock<RulesConfig> = OnceLock::new();

/// Load rules from disk. Panics if file missing or invalid — SSOT must exist.
pub fn load(path: &str) -> &'static RulesConfig {
    let raw = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("SSOT rules file missing at {}: {}", path, e));
    let cfg: RulesConfig = serde_yaml::from_str(&raw)
        .unwrap_or_else(|e| panic!("SSOT rules file invalid at {}: {}", path, e));
    info!(
        "SSOT RULES LOADED: version={} instance={} schema={} updated={}",
        cfg.rules_version, cfg.instance_id, cfg.schema_version, cfg.last_updated
    );
    RULES.set(cfg).expect("rules already loaded");
    RULES.get().unwrap()
}

/// Access the global rules. Panics if load() was never called.
pub fn global() -> &'static RulesConfig {
    RULES.get().expect("rules::load() was never called")
}

/// Check if a slug is forbidden (ends with any forbidden suffix).
pub fn is_forbidden_slug(slug: &str) -> bool {
    global()
        .forbidden_slug_suffixes
        .iter()
        .any(|suf| slug.ends_with(suf))
}
