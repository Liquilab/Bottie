# Bottie Knowledge Base — Architectuur & Configuratie

> Gegenereerd: 2026-03-22 | Bron: Rust source code + VPS productiedata
> Doel: Complete referentie voor chats die GEEN toegang hebben tot code of VPS.

---

## SECTIE 1: ORDER EXECUTION

### Via welke API plaatst Bottie orders?

Bottie gebruikt de **Polymarket CLOB API** (Central Limit Order Book).

**Bestand:** `/Users/koen/Projects/ Bottie/src/config.rs` regel 23
```rust
pub const CLOB_API: &str = "https://clob.polymarket.com";
```

Orders worden geplaatst via `POST /order` op de CLOB API.

**Bestand:** `/Users/koen/Projects/ Bottie/src/clob/client.rs` regel 253-268
```rust
let path = "/order";
// ...
let builder = self.http.post(format!("{CLOB_API}{path}"));
let builder = self.l2_request(builder, "POST", path, Some(&body))?;
```

Daarnaast worden drie andere API's gebruikt:
- **Gamma API** (`https://gamma-api.polymarket.com`) — markt metadata, resolution status
- **Data API** (`https://data-api.polymarket.com`) — wallet posities, trades, portfolio value
- **Polygon RPC** (`https://polygon-bor-rpc.publicnode.com`) — on-chain USDC balance

### Welk order type gebruikt Bottie voor BUYS?

**GTC (Good Till Cancelled)** — het order blijft in het orderbook staan totdat het vult.

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 360-369
```rust
// Place GTC order (sits in orderbook until filled — avoids FOK kills on low liquidity)
info!(
    "EXECUTE: {} {} {:.0} shares @ {:.3} = ${:.2} | edge={:.1}% | {}",
    side, signal.market_title, size, exec_price, size_usdc, signal.edge_pct, signal_source
);
let resp = match self
    .client
    .create_and_post_order(&signal.token_id, exec_price, size, side, OrderType::GTC, fee_bps)
    .await
```

Reden: FOK (Fill or Kill) wordt gedood op markten met lage liquiditeit. GTC blijft staan en vult zodra er matching aanbod is.

### Hoe wordt de entry price bepaald?

**Best ask - 1 cent** (maker order, geen taker fee).

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 178-197
```rust
let exec_price = if !signal.token_id.is_empty() {
    match self.client.get_best_ask(&signal.token_id).await {
        Ok(ask) if ask > 0.0 && ask < 1.0 => {
            if ask > signal.price * 1.25 {
                // SKIP: price moved too much
                return Ok(false);
            }
            // Buy at ask - 1ct for maker rebate (GTC sits in book)
            (ask - 0.01_f64).max(0.02)
        }
        _ => signal.price, // fall back to signal price
    }
} else {
    signal.price
};
```

Stappen:
1. Haal best ask op uit orderbook (`GET /book?token_id=`)
2. Als ask > 125% van signal price: SKIP (prijs te ver bewogen)
3. Anders: koop op `ask - 0.01` (minimum 0.02)
4. Fallback naar signal price als orderbook niet beschikbaar is

### Welk order type gebruikt Bottie voor SELLS / take-profit?

**GTC** via `create_and_post_order` met `Side::Sell`.

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 429-436
```rust
match client.create_and_post_order(
    &token_id,
    sell_price,
    shares,
    crate::clob::client::Side::Sell,
    crate::clob::client::OrderType::GTC,
    fee_bps,
).await {
```

**LET OP:** Take-profit is momenteel UITGESCHAKELD in productie. De code staat er, maar is gecommentarieerd.

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 43-44
```rust
// Take-profit DISABLED — need data lake analysis first (RUS-234)
// let tp_config = config.read().await.take_profit.clone();
```

### Hoe wordt de sell price bepaald?

Bij take-profit: **best bid** (maker order op de biedkant).

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 407-419
```rust
let best_bid = match client.get_best_bid(&token_id).await {
    Ok(b) => b,
    Err(_) => continue,
};
// ...
let sell_price = best_bid;
```

### Wat is de fee structuur?

Fees worden per token_id opgehaald via de CLOB API en gecached in een HashMap.

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 289-303
```rust
let fee_bps = match self.fee_cache.get(&signal.token_id) {
    Some(bps) => *bps,
    None => {
        match self.client.get_fee_rate_bps(&signal.token_id).await {
            Ok(bps) => {
                self.fee_cache.insert(signal.token_id.clone(), bps);
                bps
            }
            Err(e) => {
                warn!("fee-rate lookup failed: {} — using 0, will retry on error", e);
                0 // Don't cache — next attempt will retry
            }
        }
    }
};
```

**Bestand:** `/Users/koen/Projects/ Bottie/src/clob/client.rs` regel 156-172
```rust
pub async fn get_fee_rate_bps(&self, token_id: &str) -> Result<u32> {
    let path = format!("/fee-rate?token_id={token_id}");
    let url = format!("{CLOB_API}{path}");
```

Retry-mechanisme bij verkeerde fee: als de API een foutmelding geeft met de correcte fee, wordt de order opnieuw geplaatst met de juiste fee.

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 376-405
```rust
if let Some(idx) = err_str.find("taker fee: ")
    .or_else(|| err_str.find("maker fee: "))
{
    // Parse correct fee from error message, retry
    let fee_str = &err_str[idx + 11..];
    // ...
    info!("retrying with fee={} (was {})", correct_fee, fee_bps);
```

Sport markten: makers betalen 0% fee + rebate. Takers betalen max ~0.44%.

### Wat is de signing flow?

**Bestand:** `/Users/koen/Projects/ Bottie/src/clob/client.rs` regel 185-232

1. **sig_type = 2** (Gnosis Safe)
2. **maker = funder address** (0x9f23..., het Polymarket proxy wallet)
3. **signer = wallet address** (het EOA dat tekent)
4. **neg_risk = true** eerst geprobeerd (sports/neg-risk markten zijn standaard)
5. Bij "invalid signature" error: retry met **neg_risk = false** (standard exchange)

```rust
let sig_type: u8 = 2; // Gnosis Safe
// ...
// Try signing with neg_risk=true first
let signature = sign_order(&self.signer, &order_data, true).await?;
```

**Bestand:** `/Users/koen/Projects/ Bottie/src/signing/eip712.rs` regel 49-64
De domain separator bevat het exchange contract adres:
- neg_risk=true: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- neg_risk=false: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`

Chain ID = 137 (Polygon).

L2 authenticatie gebruikt HMAC-SHA256 headers:
**Bestand:** `/Users/koen/Projects/ Bottie/src/signing/hmac_auth.rs` regel 43-67
Headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY, POLY_PASSPHRASE.

### Wat gebeurt er als een order NIET vult?

GTC orders blijven in het orderbook staan. De bot controleert fill-status via de CLOB response.

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 415-438
```rust
// GTC orders sit in the orderbook and may not fill instantly.
// Trust the CLOB response (is_filled checks size_matched > 0).
// The resolver phantom-sync (every 5 min) catches false positives
// by checking actual PM positions.

if filled {
    // ...
    info!("FILLED: ...");
} else {
    let reason = resp.skipped.as_deref().unwrap_or(
        resp.error_msg.as_deref().unwrap_or("unknown")
    );
    warn!("NOT FILLED: {} | reason={}", signal.market_title, reason);
}
```

Niet-gevulde orders worden NIET gelogd in trades.jsonl (regel 441-442: `if filled {`).

**Stale orders (>6h LIVE)** worden als phantom gemarkeerd door de phantom-sync:

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 546-556
```rust
if trade_age_mins > 360 {
    info!("STALE ORDER: {} | {}min old, still LIVE — marking phantom",
        &trade.market_title, trade_age_mins);
    trade.result = Some("phantom".to_string());
```

### Hoe werkt de attempted map?

De `attempted` HashSet in Executor voorkomt dat dezelfde trade ooit opnieuw wordt geprobeerd in dezelfde sessie.

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 17-21, 58-60, 169-174
```rust
/// Once a conditionId+outcome+side is attempted, it is NEVER retried.
attempted: std::collections::HashSet<String>,

fn attempt_key(condition_id: &str, outcome: &str, side: &str) -> String {
    format!("{}:{}:{}", condition_id, outcome.to_lowercase(), side)
}

// Skip if we already attempted this market+outcome this session
let attempt_key = Self::attempt_key(&signal.condition_id, &signal.outcome, &signal.side);
if !self.attempted.insert(attempt_key) {
    return Ok(false);
}
```

Bij startup wordt de map gevuld vanuit **live PM posities** (niet trade log):

**Bestand:** `/Users/koen/Projects/ Bottie/src/execution.rs` regel 37-56
```rust
pub fn seed_from_positions(&mut self, positions: &[WalletPosition]) {
    for pos in positions {
        if pos.size_f64() < 0.01 { continue; }
        let key = Self::attempt_key(cid, outcome, "BUY");
        if self.attempted.insert(key) { count += 1; }
    }
}
```

### Wat is de flow van signaal -> order -> fill?

Complete flow:

1. **CopyTrader::poll()** — pollt Cannae posities via Data API `/positions`
2. **StabilityTracker::update()** — groupeert posities per event_slug, checkt stabiliteit
3. **StabilityTracker::drain_stable()** — geeft games vrij die stabiel zijn geweest voor `stability_window_minutes`
4. **execute_stable_game()** — bouwt AggregatedSignal per leg, past hauptbet filter toe, max_legs limiet
5. **Executor::execute_with_game_context()** — entry point voor execution
6. **Filters in execute_inner():**
   - Crypto "up or down" filter
   - Price boundary filter (min/max price)
   - Live PM position dedup (check per condition_id)
   - Attempted map dedup
   - Best ask ophalen, price drift check (>25% = skip)
   - Market resolution time check (max_resolution_days, min 30 min tot einde)
7. **Sizing:** `sizing::copy_trade_size()` met tiered game budget
8. **Risk check:** `risk.check_trade_with_context()` (bankroll, daily loss, open bets, per-wallet limit)
9. **Fee ophalen:** cached per token_id, retry bij API fout
10. **Order plaatsen:** `client.create_and_post_order()` met GTC
11. **Signing:** EIP-712 met neg_risk=true (retry met false bij invalid signature)
12. **Response:** `is_filled()` check op size_matched > 0
13. **Logging:** alleen FILLED trades naar trades.jsonl
14. **Risk update:** `risk.record_trade_opened_with_context()`

---

## SECTIE 2: COPY TRADING

### Hoe detecteert Bottie nieuwe Cannae posities?

Via snapshot-diff op de **Data API `/positions`** endpoint.

**Bestand:** `/Users/koen/Projects/ Bottie/src/clob/client.rs` regel 396-422
```rust
pub async fn get_wallet_positions(&self, address: &str, limit: u32) -> Result<Vec<WalletPosition>> {
    // Paginate to get ALL positions above threshold
    let url = format!(
        "{DATA_API}/positions?user={address}&limit={page_size}&sizeThreshold=0.01&sortBy=CURRENT&sortOrder=desc&offset={offset}"
    );
```

**Polling interval:** configureerbaar, standaard **15 seconden** (VPS config: `poll_interval_seconds: 15`).

Tiered polling:
- **Hot tier** (signaal in laatste `window_minutes`): elke cyclus
- **Warm tier** (rest): elke `warm_poll_interval_seconds / poll_interval_seconds` cycli

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 106-128, 471-495

### Wat is een "snapshot" en hoe wordt het verschil berekend?

Per wallet wordt een HashMap `position_key -> shares` opgeslagen als vorige snapshot.

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 177-232
```rust
// Build current snapshot: position_key -> size
let mut current_snapshot: HashMap<String, f64> = HashMap::new();
for pos in positions {
    let cur = pos.cur_price_f64();
    if cur <= 0.01 || cur >= 0.99 { continue; } // resolved
    current_snapshot.insert(key, pos.size_f64());
}

// Detect new or increased positions
let is_new = match prev {
    None => true, // first poll — treat all as new
    Some(prev_snap) => {
        let prev_size = prev_snap.get(&key).copied().unwrap_or(0.0);
        cur_size > prev_size * 1.05 // >5% increase = new buy
    }
};
```

Position key = `conditionId:outcome`.

### Hoe werkt het Hauptbet filter?

Per conditionId wordt de positie met de grootste **initialValue (USDC)** gekozen. Kleinere posities op dezelfde conditionId worden geskipt als hedge.

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 193-258
```rust
// Hauptbet filter: per conditionId, find the largest USDC position.
let mut max_usdc_per_condition: HashMap<String, f64> = HashMap::new();
for pos in positions {
    let usdc = pos.initial_value_f64();
    let entry = max_usdc_per_condition.entry(cid).or_insert(0.0);
    if usdc > *entry { *entry = usdc; }
}

// ...
if usdc < max_for_cond {
    continue; // hedge / smaller bet — skip
}
```

Dit wordt ook toegepast in `execute_stable_game()`:

**Bestand:** `/Users/koen/Projects/ Bottie/src/main.rs` regel 473-501
```rust
let mut best_per_condition: HashMap<String, &WalletPosition> = HashMap::new();
for pos in &game.positions {
    // Keep largest per conditionId
}
// Sort by USDC size descending, apply max_legs limit
game_legs.sort_by(|a, b| b.initial_value_f64().partial_cmp(&a.initial_value_f64()));
if max_legs > 0 {
    game_legs.truncate(max_legs);
}
```

VPS config: `max_legs_per_event: 1` — alleen de grootste leg per event.

### Hoe worden resolved markets gefilterd?

Posities met `curPrice <= 0.01` of `curPrice >= 0.99` worden als resolved beschouwd en uitgesloten.

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 185-189, 219-222
```rust
let cur = pos.cur_price_f64();
if cur <= 0.01 || cur >= 0.99 {
    continue; // already resolved — exclude from snapshot
}
```

### Hoe werkt de seeding/catch-up bij bot restart?

Bij startup worden drie dingen geseeded:

1. **Executor attempted map** — vanuit live PM posities (blokkeert duplicaten)
2. **Stability tracker emitted set** — event_slugs die we al bezitten
3. **Risk manager open_bets** — vanuit trades.jsonl (per-wallet + per-sport counters)

**Bestand:** `/Users/koen/Projects/ Bottie/src/main.rs` regel 298-320
```rust
let funder = client.funder_address();
match client.get_wallet_positions(&funder, 500).await {
    Ok(positions) => {
        executor.seed_from_positions(&positions);
        stability_tracker.seed_emitted(our_events);
    }
}
```

**Bestand:** `/Users/koen/Projects/ Bottie/src/main.rs` regel 142-161
```rust
let open_trades: Vec<_> = existing.iter()
    .filter(|t| t.filled && t.result.is_none() && !t.dry_run)
    .collect();
for t in &open_trades {
    r.record_trade_opened_with_context(0.0, t.copy_wallet.as_deref(), &t.sport);
}
```

Bij eerste poll van een wallet: alle posities worden als "nieuw" behandeld, maar de attempted map voorkomt duplicaten.

### Wat is de stability_window en stability_threshold?

De **StabilityTracker** wacht tot Cannae's GTC orders volledig gevuld zijn voordat de bot kopieert.

**Bestand:** `/Users/koen/Projects/ Bottie/src/stability.rs` regel 8-16, 56-183

- **stability_window_minutes**: hoe lang posities stabiel moeten zijn (VPS: **30 minuten**)
- **stability_threshold_pct**: maximale % verandering in shares per leg om als stabiel te gelden (VPS: **5.0%**)

Workflow:
1. Nieuwe Cannae posities worden per event_slug gegroepeerd
2. Per poll-cyclus wordt een snapshot vergeleken met de vorige
3. Als ALLE legs < threshold% veranderd zijn: `stable_since` wordt gezet
4. Als `now - stable_since >= window_minutes`: game wordt vrijgegeven voor trading
5. Abandoned na 4 uur zonder stabilisatie
6. Eenmaal geemit, nooit opnieuw geemit

```rust
if prev > 0.0 {
    let change_pct = ((curr - prev) / prev).abs() * 100.0;
    if change_pct > threshold_pct {
        is_stable = false;
        break;
    }
}
```

### Hoe werkt de max_delay_seconds check?

`max_delay_seconds` is geconfigureerd op **60** (VPS config) maar wordt NIET actief gebruikt in de huidige code flow. De `signal_delay_ms` wordt hardcoded op 15000 (1 poll cycle) in copy_trader.rs regel 275:

```rust
let signal_delay_ms: u64 = 15_000; // one poll cycle as conservative estimate
```

De waarde wordt meegegeven in het signaal maar er is geen expliciete check die trades blokkeert op basis van delay in de huidige code. Het veld wordt wel opgeslagen in trades.jsonl.

### Hoe werkt het consensus mechanisme?

Consensus groepeert bets per **event_slug** (niet per conditionId). Zo tellen moneyline, O/U en spread op hetzelfde event als consensus.

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 315-357, 507-547

1. Bets worden opgeslagen in `recent_bets: HashMap<consensus_key, Vec<RecentBet>>`
2. Bets ouder dan `window_minutes` worden gepruned
3. Consensus score groepeert per outcome en vindt de majority side
4. Alleen de majority side telt als consensus
5. Minority side wordt geskipt (`outcome_matches_majority`)

VPS config:
- `min_traders: 1` — solo signalen zijn toegestaan
- `window_minutes: 60` — consensus window is 60 minuten
- `multiplier_2: 1.0` — geen multiplier bij 2 wallets
- `multiplier_3plus: 1.0` — geen multiplier bij 3+ wallets

Met huidige configuratie (1 wallet, Cannae) is consensus effectief uitgeschakeld.

### Welke market types worden gefilterd?

Per wallet configureerbaar via `market_types` in de watchlist.

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 290-296, 582-597
```rust
if !wallet_market_types.is_empty() {
    let detected = Self::detect_market_type(&title);
    if !wallet_market_types.iter().any(|mt| mt == &detected) {
        continue;
    }
}
```

Market type detectie op basis van title:
- `"win"` — title bevat "win on" of "win the"
- `"ou"` — title bevat "O/U"
- `"spread"` — title bevat "Spread"
- `"draw"` — title bevat "draw"
- `"ml"` — title bevat "vs." en geen van bovenstaande
- `"other"` — geen match

VPS config voor Cannae: `market_types: ["win"]` — alleen "Will X win" markten.

Globaal filter in execution.rs:
- Crypto "up or down" markten worden geblokkeerd (regel 110-115)
- Price boundary: `min_price: 0.05`, `max_price: 0.95` (VPS config)

### Welke leagues worden gefilterd?

Per wallet configureerbaar via `leagues` in de watchlist.

**Bestand:** `/Users/koen/Projects/ Bottie/src/copy_trader.rs` regel 303-313
```rust
let slug_for_league = raw_slug_for_league.trim_end_matches("-more-markets");
if !wallet_leagues.is_empty() {
    let league_prefix = slug_for_league.split('-').next().unwrap_or("");
    if !wallet_leagues.iter().any(|l| l == league_prefix) {
        continue;
    }
}
```

VPS config Cannae leagues:
```yaml
leagues: ["epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por",
          "bra", "itc", "ere", "es2", "bl2", "sea", "elc", "mex", "fr2", "aus",
          "spl", "efl", "tur"]
```

De league prefix wordt uit de `event_slug` gehaald (eerste deel voor het streepje).

Het league filter wordt ook toegepast in `execute_stable_game()`:
**Bestand:** `/Users/koen/Projects/ Bottie/src/main.rs` regel 463-471

---

## SECTIE 3: SIZING

### Hoe werken de tiers?

Tiered sizing schaalt mee met Cannae's overtuiging (game size). Grotere Cannae posities = hogere allocatie.

**Bestand:** `/Users/koen/Projects/ Bottie/src/sizing.rs` regel 89-105
```rust
// Calibrated on 1550 on-chain games (P50=$1.3K, P75=$5K, P90=$15K).
//   < $1.3K  -> 1% of bankroll (bottom 50%)
//   $1.3-5K  -> 1.5% (P50-P75)
//   $5-15K   -> 2%   (P75-P90)
//   > $15K   -> 3%   (top 10%)
let game_budget_pct = if cannae_game_total_usdc >= 15_000.0 {
    0.03
} else if cannae_game_total_usdc >= 5_000.0 {
    0.02
} else if cannae_game_total_usdc >= 1_300.0 {
    0.015
} else {
    0.01
};
let game_budget = bankroll * game_budget_pct;
```

Daarna proportioneel: `our_shares = cannae_shares * (game_budget / cannae_game_total_usdc)`

Floor: minimaal $2.50 per bet.
PM minimum: minimaal 5 shares.

### Wat is portfolio_reference_usdc en hoe wordt het gebruikt?

**Bestand:** `/Users/koen/Projects/ Bottie/src/config.rs` regel 238-242
```rust
/// Reference portfolio value for tiered copy-trade sizing (cash + positions).
/// If 0, falls back to live bankroll (cash only).
#[serde(default)]
pub portfolio_reference_usdc: f64,
```

VPS config: `portfolio_reference_usdc: 800`

NIET GEVONDEN IN CODE: `portfolio_reference_usdc` wordt gedefinieerd in config maar wordt NIET gelezen in de sizing functies. De sizing gebruikt `risk.bankroll()` als input, niet `portfolio_reference_usdc`. Dit veld lijkt ongebruikt in de huidige code.

### Wat is max_bet_pct en hoe interacteert het met de tiers?

**Bestand:** `/Users/koen/Projects/ Bottie/src/sizing.rs` regel 47-48
```rust
let max_bet = bankroll * config.max_bet_pct / 100.0;
let size_usdc = (bankroll * safe_fractional).min(max_bet);
```

VPS config: `max_bet_pct: 3.0` (3% van bankroll).

Dit wordt alleen gebruikt in `kelly_size()` (fallback sizing), NIET in `copy_trade_size()`. De tiered sizing in `copy_trade_size()` heeft zijn eigen plafonds via de game_budget_pct (1-3%).

### Hoe wordt de bankroll bepaald?

Via on-chain USDC balance + posities waarde (Data API `/value`).

**Bestand:** `/Users/koen/Projects/ Bottie/src/sync.rs` regel 146-162
```rust
pub async fn sync_bankroll(client: &ClobClient) -> Result<f64> {
    let cash = client.get_usdc_balance().await?;
    let positions_value = match client.get_positions_value(&funder).await {
        Ok(v) => v,
        Err(e) => 0.0,
    };
    let total = cash + positions_value;
}
```

USDC balance via Polygon RPC `eth_call balanceOf` op USDC.e contract (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`).

**Bestand:** `/Users/koen/Projects/ Bottie/src/clob/client.rs` regel 556-586

Positions value via Data API: `GET /value?user={address}`

**Bestand:** `/Users/koen/Projects/ Bottie/src/clob/client.rs` regel 428-438

Sync frequentie: elke **20 poll cycli** (~5 minuten bij 15s interval).

**Bestand:** `/Users/koen/Projects/ Bottie/src/main.rs` regel 334-354

### Hoe wordt copy_base_size_pct gebruikt?

VPS config: `copy_base_size_pct: 20.0`

NIET GEVONDEN IN CODE: `copy_base_size_pct` wordt gedefinieerd in `SizingConfig` maar wordt NIET gelezen in de sizing functies (`kelly_size` en `copy_trade_size`). Het is een oud veld dat is vervangen door de tiered sizing.

### Zijn er aparte tiers voor moneyline vs draw vs andere market types?

Nee. De tiered sizing in `copy_trade_size()` differentieert NIET op market type. Alle types gebruiken dezelfde tier drempels op basis van Cannae's game totaal.

De Kelly sizing (`kelly_size()`) heeft wel een Kelly ceiling per price range, maar die differentieert ook niet op market type:
**Bestand:** `/Users/koen/Projects/ Bottie/src/sizing.rs` regel 37-44

---

## SECTIE 4: RISK MANAGEMENT

### Hoe werkt max_daily_loss_pct?

**Bestand:** `/Users/koen/Projects/ Bottie/src/risk.rs` regel 49-56
```rust
let max_daily_loss = self.initial_bankroll * self.config.max_daily_loss_pct / 100.0;
if self.daily_pnl < -max_daily_loss {
    return RiskDecision::Rejected(format!(
        "daily loss ${:.2} exceeds limit ${:.2}",
        self.daily_pnl.abs(), max_daily_loss
    ));
}
```

VPS config: `max_daily_loss_pct: 15.0` (15% van initial_bankroll op start van de dag).

"Loss" = negatieve PnL van resolved trades (win/loss/take_profit), opgeteld in `daily_pnl`.

Daily reset gebeurt om middernacht UTC:
**Bestand:** `/Users/koen/Projects/ Bottie/src/main.rs` regel 242-263

### Wat doet min_bankroll?

**Bestand:** `/Users/koen/Projects/ Bottie/src/risk.rs` regel 41-46
```rust
if self.bankroll < self.config.min_bankroll {
    return RiskDecision::Rejected(format!(
        "bankroll ${:.2} below minimum ${:.2}",
        self.bankroll, self.config.min_bankroll
    ));
}
```

VPS config: `min_bankroll: 50.0` — de bot stopt met nieuwe trades als bankroll < $50. Bestaande posities lopen gewoon door. De bot draait wel door (geen exit), maar alle nieuwe trades worden geweigerd.

### Hoe werkt max_open_bets?

**Bestand:** `/Users/koen/Projects/ Bottie/src/risk.rs` regel 58-64
```rust
if self.open_bets >= self.config.max_open_bets {
    return RiskDecision::Rejected(format!(
        "open bets {} at limit {}",
        self.open_bets, self.config.max_open_bets
    ));
}
```

VPS config: `max_open_bets: 200`

Open bets teller wordt bijgehouden via:
- `record_trade_opened_with_context()` (+1)
- `record_trade_closed_with_context()` (-1)
- `sync_full()` — herberekent uit trades.jsonl (elke ~5 min)

Per-wallet concentratielimiet: `MAX_OPEN_PER_WALLET = 100` (hardcoded, regel 24).

### Is er een circuit breaker?

Ja, impliciet: als `daily_pnl < -max_daily_loss` (15% van initial bankroll), worden ALLE nieuwe trades geweigerd. Dit is effectief een circuit breaker.

**Bestand:** `/Users/koen/Projects/ Bottie/src/risk.rs` regel 130-136
```rust
if self.daily_pnl < -(self.initial_bankroll * self.config.max_daily_loss_pct / 100.0) {
    warn!("DAILY LOSS LIMIT HIT: pnl=${:.2}, stopping trading", self.daily_pnl);
}
```

Daarnaast is er een **hard cap** van 10% van bankroll per individuele trade:
**Bestand:** `/Users/koen/Projects/ Bottie/src/risk.rs` regel 67-73
```rust
let max_bet = self.bankroll * 0.10; // 10% hard cap
if size_usdc > max_bet {
    return RiskDecision::Rejected(..);
}
```

### Hoe wordt de portfolio waarde bepaald?

**Cash + posities** = totaal portfolio.

**Bestand:** `/Users/koen/Projects/ Bottie/src/sync.rs` regel 159
```rust
let total = cash + positions_value;
```

Cash = on-chain USDC.e balance (via Polygon RPC).
Positions = Data API `/value` endpoint (geeft huidige marktwaarde van alle open posities).

---

## SECTIE 5: TAKE PROFIT

### Is take-profit actief?

**Nee, take-profit is UITGESCHAKELD in productie.**

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 43-44
```rust
// Take-profit DISABLED — need data lake analysis first (RUS-234)
// let tp_config = config.read().await.take_profit.clone();
// take_profit_check(&client, &logger, &risk, &tp_config, &mut tp_cooldown).await;
```

VPS config zegt wel `take_profit.enabled: true`, maar de code roept de functie niet aan.

### Hoe werkt het? (wanneer het actief zou zijn)

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 290-327

Tiered thresholds op basis van entry price:
- Entry < 0.30: sell bij best_bid >= 0.90
- Entry 0.30-0.50: sell bij best_bid >= 0.85
- Entry 0.50-0.70: sell bij best_bid >= 0.88
- Entry 0.70-0.83: sell bij best_bid >= 0.93

Extra rules:
- ALTIJD verkopen bij `safety_threshold` (default 0.95)
- Verkopen als delta > 0.30 EN best_bid >= 0.80
- Draw markten: threshold - 0.03 (meer volatiel)

### Wat is min_delta en safety_threshold?

**Bestand:** `/Users/koen/Projects/ Bottie/src/config.rs` regel 108-117

- `min_delta`: minimale positieve delta (bid - entry) voordat take-profit overweegt. Default: **0.05** (5ct).
- `safety_threshold`: altijd verkopen boven deze prijs. Default: **0.95** (95ct).

VPS config: `min_delta: 0.05`, `safety_threshold: 0.95`.

### Hoe wordt de sell order geplaatst?

GTC limit order op **best_bid** prijs (maker order).

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 418-436

Bij niet-fill: cooldown van 5 minuten op die token_id.

---

## SECTIE 6: LIVE STAAT (VPS)

### Service Status (2026-03-22 ~12:20 UTC)
| Service | Status |
|---------|--------|
| bottie.service | **active (running)** |
| dashboard.service | **active (running)** |
| bottie-dashboard.service | **active (running)** |
| bottie-research.service | inactive (dead) |

### Laatste Trades (trades.jsonl)
- **442 regels** in trades.jsonl
- Laatste trade: `2026-03-22T12:16:18` — Will CD Mirandes vs. Real Valladolid CF end in a draw? (No, 68ct, 22 shares, $14.97)

### Bot Status (uit logs)
```
STATUS: Trades: 333 | W/L: 202/131 | Win rate: 60.7% | PnL: $301.86
bankroll=$988.39 | daily_pnl=$0.00 | open=109
SYNC: cash=$296.57 + positions=$691.82 = portfolio=$988.39
```

### Actieve Cron Jobs
| Schedule | Script | Doel |
|----------|--------|------|
| `17 */6 * * *` | `research/validation/cannae_health.py` | Cannae strategie validatie |
| `43 */4 * * *` | `research/validation/bot_health.py` | Bot gezondheid check |
| `3 8 * * *` | `research/validation/report.py` | Dagelijks rapport |
| `7 3 * * *` | `research/wallet_scout/scan.py` | Wallet discovery scan |
| `13 4 * * *` | `research/wallet_scout/evaluate.py` | Wallet evaluatie |
| `37 4 * * *` | `research/wallet_scout/alert.py` | Wallet alert |
| `*/5 * * * *` | `scripts/ralph.sh` (RustedPoly) | Ralph copy trade monitor |

### Recente Log Activiteit
De bot draait stabiel. Recent geobserveerd:
- Stability tracker: events worden gedetecteerd, gevolgd en na ~30+ min vrijgegeven
- Resolver checkt regelmatig 12-74 open markten
- Signalen komen binnen van Cannae (enige wallet in watchlist)
- Voorbeeld: "STABILITY EXECUTE: es2-mir-vld-2026-03-22 — 1 legs" direct gevuld

---

## SECTIE 7: DATA FLOW

### Welke PM API endpoints gebruikt Bottie?

**CLOB API** (`https://clob.polymarket.com`):
| Endpoint | Functie | Bestand + Regel |
|----------|---------|-----------------|
| `POST /order` | Orders plaatsen (GTC/FOK/GTD) | `clob/client.rs:253` |
| `GET /order/{id}` | Order status ophalen | `clob/client.rs:447` |
| `GET /book?token_id=` | Orderbook (best ask/bid) | `clob/client.rs:471` |
| `GET /fee-rate?token_id=` | Fee rate per token | `clob/client.rs:157` |
| `GET /markets/{conditionId}` | Token IDs voor markt | `clob/client.rs:503` |
| `POST /redeem` | Winning tokens redeemen | `clob/client.rs:591` |
| `GET /auth/derive-api-key` | API key derivation | `clob/client.rs:144` |

**Data API** (`https://data-api.polymarket.com`):
| Endpoint | Functie | Bestand + Regel |
|----------|---------|-----------------|
| `GET /positions?user=` | Wallet posities (gepagineerd) | `clob/client.rs:403` |
| `GET /value?user=` | Portfolio waarde | `clob/client.rs:429` |
| `GET /trades?limit=` | Publieke trades feed | `clob/client.rs:327` |
| `GET /trades?maker=` / `?taker=` | Wallet trades | `clob/client.rs:352-366` |
| `GET /activity?user=` | Wallet activiteit (deprecated) | `clob/client.rs:388` |

**Gamma API** (`https://gamma-api.polymarket.com`):
| Endpoint | Functie | Bestand + Regel |
|----------|---------|-----------------|
| `GET /markets?condition_ids=` | Markt status + resolution | `clob/client.rs:486` |
| `GET /events?active=true&tag=` | Sports events zoeken | `clob/client.rs:537` |

**Polygon RPC** (`https://polygon-bor-rpc.publicnode.com`):
| Methode | Functie | Bestand + Regel |
|---------|---------|-----------------|
| `eth_call balanceOf` | USDC.e balance | `clob/client.rs:556` |

### Hoe wordt trades.jsonl geschreven?

**Bestand:** `/Users/koen/Projects/ Bottie/src/logger.rs` regel 145-175

Append-only JSONL (1 JSON object per regel). Alleen FILLED trades worden geschreven.

Velden per trade (TradeLog struct, `logger.rs` regel 10-55):
```
timestamp, token_id, condition_id, market_title, sport, side, outcome,
price, size_usdc, size_shares, signal_source, copy_wallet,
consensus_count, consensus_wallets, edge_pct, confidence, signal_delay_ms,
event_slug, order_id, filled, dry_run, result, pnl, resolved_at,
sell_price, actual_pnl, exit_type, strategy_version
```

`result` mogelijke waarden: `"win"`, `"loss"`, `"refund"`, `"take_profit"`, `"sold"`, `"phantom"`, of `null` (open).

De resolver herschrijft het HELE bestand bij resoluties via `rewrite_all()`:
**Bestand:** `/Users/koen/Projects/ Bottie/src/logger.rs` regel 263-307

### Wat zijn "phantom fills" en hoe herken je ze?

Phantom fills = trades die de bot als "FILLED" logde maar die nooit daadwerkelijk op Polymarket verschenen (CLOB API meldde fill maar order werd later geannuleerd).

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 499-639

Detectie via `sync_phantoms()` (elke 60 seconden):
1. Haal order status op via `GET /order/{id}`
2. Status mapping:
   - `ORDER_STATUS_LIVE` → wacht (order zit nog in book). >6h = mark als phantom.
   - `ORDER_STATUS_MATCHED` → echt gevuld. Check na >2h of positie nog op PM staat.
   - `ORDER_STATUS_CANCELED`, `INVALID`, `CANCELED_MARKET_RESOLVED` → phantom.

Phantom trades krijgen `result: "phantom"`, `pnl: 0.0`, `exit_type: "phantom"`.

### Hoe werkt de TradeLogger?

**Bestand:** `/Users/koen/Projects/ Bottie/src/logger.rs` regel 57-308

Drie in-memory caches voor snelle dedup:
- `open_positions: HashSet<String>` — condition_id:outcome van open posities
- `open_event_types: HashMap<String, String>` — event_slug -> market_type
- `open_event_wallets: HashMap<String, String>` — event_slug -> copy_wallet

Functies:
- `log()` — append trade + update caches
- `load_all()` — lees alle trades uit bestand
- `rewrite_all()` — herschrijf volledig bestand + rebuild caches
- `has_open_position()` — check per condition_id+outcome
- `has_any_open_on_event()` — check per event (via event_dedup_key)
- `has_conflicting_wallet_on_event()` — check per wallet per event

### Hoe wordt een trade als "resolved" gemarkeerd?

De **resolver_loop** in `resolver.rs` checkt periodiek:

**Bestand:** `/Users/koen/Projects/ Bottie/src/resolver.rs` regel 80-277

1. Laad alle trades uit trades.jsonl
2. Vind open posities (result is None, filled, niet dry-run)
3. Groepeer per condition_id
4. Smart scheduling: check interval gebaseerd op end_date:
   - Past end_date: elke 60s
   - < 1 uur: elke 2 min
   - < 1 dag: elke 5 min
   - < 3 dagen: elke 30 min
   - > 3 dagen: elke uur
5. Haal market status op via Gamma API (`/markets?condition_ids=`)
6. Als `winning_outcome()` een winner retourneert:
   - PnL berekening: win = `shares * (1 - entry_price)`, loss = `-size_usdc`
   - Update trades.jsonl via `rewrite_all()`
   - Update risk manager via `record_trade_closed_with_context()`
   - Update wallet tracker via `record_trade_result()`

Polymarket auto-redeemt winning posities na resolution (geen manual /redeem nodig).

---

## SECTIE 8: CONFIG VERSCHIL (VPS vs LOCAL)

**Resultaat: IDENTIEK**

De VPS config (`/opt/bottie/config.yaml`) en lokale config (`/Users/koen/Projects/ Bottie/config.yaml`) zijn volledig identiek. Geen verschil.

### VPS Config Samenvatting (source of truth)

```yaml
copy_trading:
  enabled: true
  poll_interval_seconds: 15
  watchlist:
    - Cannae (0x7ea5...) — weight 1.0, leagues 23 stuks, market_types ["win"],
      max_legs 1, price range 0.05-0.95
  consensus: min_traders 1, window 60min, multipliers 1.0/1.0
  max_delay_seconds: 60
  max_resolution_days: 3
  warm_poll_interval_seconds: 60
  batch_size: 8
  stability_window_minutes: 30
  stability_threshold_pct: 5.0

odds_arb: UITGESCHAKELD

sizing:
  kelly_fraction: 0.25
  max_bet_pct: 3.0
  copy_base_size_pct: 20.0 (ONGEBRUIKT)
  min_price: 0.05
  max_price: 0.95
  portfolio_reference_usdc: 800 (ONGEBRUIKT in sizing code)

take_profit:
  enabled: true (CONFIG), maar CODE is UITGESCHAKELD (gecommentarieerd)

risk:
  max_daily_loss_pct: 15.0
  min_bankroll: 50.0
  max_open_bets: 200

autoresearch: UITGESCHAKELD (service inactive)
```
