# [RP-xxx] Multi-crypto 5M Intelligence Dashboard — M0 t/m M2

**Team:** RustedPoly
**Priority:** High
**Effort:** 4 sessies (M0: 1, M1: 2, M2: 1)
**Depends on:** —
**Blocks:** live deploy ETH/XRP/SOL ($2 @ 1c), DOGE/HYPE/BNB analyse

---

## Context

HV (`0x7da07b2a8b009a406198677debda46ad651b6be2`) is onze baseline-vergelijker voor 5M crypto. We willen:
1. Dagelijks geüpdatet dashboard met historische HV- + bot-performance
2. Vergelijking HV vs onze bot per window
3. Counterfactual: wat als we andere tiers/coins hadden gedaan

Deze ticket dekt **M0-M2**: descriptive + comparatieve laag. Predictive (M4) en cron-deploy (M5) zijn aparte tickets.

## Scope

**In:** BTC, ETH, SOL, XRP, DOGE, HYPE, BNB 5M-windows, 30 dagen rolling.
**Out:** predictive forecasting, ML, auto-sizing, cron.

## Data-bronnen
- `data-api.polymarket.com/activity?user=<funder>` — HV + onze bot trades
- `clob.polymarket.com/markets/<cid>` — resolution + token IDs
- `clob.polymarket.com/prices-history?market=<tid>&startTs=X&endTs=X+300` — per-window min/max (window-scoped, `interval=1h` is broken voor 5m markets)
- `api.binance.com/api/v3/klines` — 5m OHLCV per coin (σ berekening)

## Milestones & Exit-criteria

### M0 — Proof of concept (1 sessie)
- [ ] Scope: BTC only, 1 dag data
- [ ] Pipeline: pull → windows → resolve → prices → analytics
- [ ] Schema-contract tussen fasen (`@dataclass` met validate)
- [ ] PID-lock, atomic writes, ThreadPool voor CLOB calls
- [ ] Dashboard sectie onder huidige bot-KPIs toont gisteren's HV BTC P&L
- **Exit:** HV P&L uit script ± 1% van PM-profielpagina "Closed Positions" tab

### M1 — Scale naar 7 coins × 30d (2 sessies)
- [ ] Coin-lijst: BTC, ETH, SOL, XRP, DOGE, HYPE, BNB
- [ ] Daily aggregates: per-dag per-coin wins/losses/cost/revenue/pnl/roi
- [ ] Weekday × uur heatmap data
- [ ] Binance klines integratie: σ_5m, σ_24h per coin per dag
- [ ] Dashboard tab "Historisch 30d" met:
  - Daily grid (datum × coin × pnl + σ kolom)
  - Cumulative P&L curve per coin
  - Weekday heatmap
- **Exit:** coverage ≥ 95% windows resolved, dashboard rendert < 2s

### M2 — HV vs Bot join (1 sessie)
- [ ] Pull onze bot activity (funder `0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42`)
- [ ] Per-window join: `{slug, hv_pnl, bot_pnl, delta}`
- [ ] Delta-rapport: windows waar HV wint en bot mist (en omgekeerd)
- [ ] Dashboard tab "HV vs Bot":
  - Cumulative delta curve (30d)
  - Top-10 windows HV > bot (grootste gemiste winst)
  - Top-10 windows bot > HV (grootste vermeden verlies)
  - Daily delta tabel
- **Exit:** manual spot-check 10 windows, delta klopt exact

## Architectuur

```
scripts/dashboard_pipeline.py
 ├─ phase_pull_hv        (data-api activity, incremental)
 ├─ phase_pull_bot       (data-api activity, incremental)
 ├─ phase_discover_markets  (alle 7 coins via gamma-api)
 ├─ phase_resolve        (CLOB /markets, ThreadPool)
 ├─ phase_prices         (CLOB prices-history, window-scoped ThreadPool)
 ├─ phase_klines         (Binance, incremental 30d)
 ├─ phase_analytics      (compute descriptive + join + counterfactual)
 └─ output: data/analytics.json (atomic write)

dashboard.py (extend bestaand)
 └─ nieuwe sectie onder huidige bot-KPIs, leest data/analytics.json
```

## Regels / Guardrails (lessen uit prutsessie 2026-04-18)

- **Single-instance PID lock** (`data/.dashboard_pipeline.lock`), stale cleanup op startup
- **Atomic writes** (tmp + rename) voor élke output file
- **Checkpoint** elke 200/500 ops, resumable
- **Schema validatie** tussen fasen — fail-fast bij missende keys
- **ThreadPoolExecutor(max_workers=10)** voor CLOB calls
- **Smoke test op 1 dag** vóór 30d scale
- **SIGTERM/SIGINT handlers** die lock opruimen
- **Prices-history**: alleen `startTs=window_start&endTs=window_start+300`, nooit `interval=1h`
- **Per-phase stats** log (rows processed, errors, latency)

## Acceptance tests

- [ ] Pipeline draait tweemaal achtereen zonder fout, tweede run hergebruikt cache
- [ ] Kill mid-run → restart → geen data-verlies
- [ ] Dashboard toont correct > 95% windows met prijsdata
- [ ] HV 7d P&L uit ons script ± 1% van PM profielpagina
- [ ] Onze bot 7d P&L uit script == bot's eigen STATUS log
- [ ] Delta-tabel reproduceerbaar: zelfde input → zelfde output

## Open vragen
- Huidige `dashboard.py` is 186 KB — is er een module-boundary om schoon uit te breiden, of moet M1 een `dashboard_intel.py` als aparte include zijn?

## Follow-up tickets (niet in deze scope)
- [RP-xxx+1] M3 — Counterfactual tier ladders (1c/2c/3c/5c/7c/10c)
- [RP-xxx+2] M4 — Predictive layer (heuristiek → feature store → ML challenger)
- [RP-xxx+3] M5 — VPS cron 00:05 UTC + alerting
- [RP-xxx+4] Deploy ETH/XRP/SOL live @ $2 @ 1c (depends on M0-M2 validatie)
