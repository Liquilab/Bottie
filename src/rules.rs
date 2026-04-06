//! SSOT Pilaar 1 — Inkoopregels.
//!
//! Loads declarative trading rules from data/ssot/rules.yaml and exposes
//! them as a global singleton. Replaces hardcoded checks in main.rs and
//! scheduler.rs so that rule changes live in one place.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::OnceLock;
use tracing::info;

/// Schema version this binary expects. Bump when required fields change.
pub const SCHEMA_VERSION: u32 = 1;

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

/// Load rules from disk. Panics if file missing, invalid, wrong schema,
/// or `instance_id` mismatches the `BOTTIE_INSTANCE` env var.
///
/// The panic is intentional — systemd will log and exit. This is safer
/// than silently falling back to hardcoded defaults.
pub fn load(path: &str) {
    // Resolve to absolute path for unambiguous logging / postmortem.
    let abs_path = Path::new(path)
        .canonicalize()
        .unwrap_or_else(|e| panic!("SSOT rules file missing at {}: {}", path, e));

    let raw = std::fs::read_to_string(&abs_path)
        .unwrap_or_else(|e| panic!("SSOT rules file unreadable at {}: {}", abs_path.display(), e));

    // Hash BEFORE parse so we can attribute a parse failure to an exact file content.
    let sha = Sha256::digest(raw.as_bytes());
    let sha_hex: String = sha.iter().map(|b| format!("{:02x}", b)).collect();
    let sha_short = &sha_hex[..12];

    let cfg: RulesConfig = serde_yaml::from_str(&raw)
        .unwrap_or_else(|e| panic!(
            "SSOT rules file invalid at {} (sha256={}): {}",
            abs_path.display(), sha_short, e
        ));

    // Schema-version guard — binary-vs-file drift detection.
    if cfg.schema_version != SCHEMA_VERSION {
        panic!(
            "SSOT rules schema mismatch at {}: file says schema={}, binary expects schema={}",
            abs_path.display(), cfg.schema_version, SCHEMA_VERSION
        );
    }

    // Instance-scope guard — enforces Cannae/GIYN separation at load time.
    // Each systemd unit MUST set BOTTIE_INSTANCE=cannae or =giyn.
    let expected_instance = std::env::var("BOTTIE_INSTANCE").unwrap_or_else(|_| panic!(
        "BOTTIE_INSTANCE env var not set — cannot validate rules.yaml instance_id. \
         Set BOTTIE_INSTANCE=cannae (or =giyn) in the systemd unit."
    ));
    if cfg.instance_id != expected_instance {
        panic!(
            "SSOT rules instance mismatch at {}: file instance_id={:?} but BOTTIE_INSTANCE={:?}. \
             This would apply the wrong instance's rules — refusing to boot.",
            abs_path.display(), cfg.instance_id, expected_instance
        );
    }

    info!(
        "SSOT RULES LOADED: version={} instance={} schema={} updated={} path={} sha256={}",
        cfg.rules_version,
        cfg.instance_id,
        cfg.schema_version,
        cfg.last_updated,
        abs_path.display(),
        sha_short,
    );

    RULES.set(cfg).expect("rules already loaded");
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
