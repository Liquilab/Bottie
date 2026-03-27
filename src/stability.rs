use crate::clob::types::WalletPosition;

/// A game ready for trading via the T-30/T-5 scheduler.
pub struct StableGame {
    pub event_slug: String,
    pub positions: Vec<WalletPosition>,
    pub source_wallet: String,
    pub source_name: String,
}
