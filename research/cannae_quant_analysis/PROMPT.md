# Cannae Quant Analysis — Volledig Onderzoeksplan

**Doel:** Bouw een herhaalbaar analyse-systeem dat Cannae's edge exact decomposed, per market type / league / timing kwantificeert, en automatisch drift detecteert als zijn performance verandert.

**Output:** `research/cannae_quant_analysis/report.json` + `research/cannae_quant_analysis/history/YYYY-MM-DD.json`

---

## Fase 1: Data Pipeline (eenmalig + dagelijks herhaalbaar)

### 1.1 Primaire datasource: `/activity` endpoint

```
GET https://data-api.polymarket.com/activity?user={CANNAE_FUNDER}&type=trade&limit=500&offset={n}
GET https://data-api.polymarket.com/activity?user={CANNAE_FUNDER}&type=redeem&limit=500&offset={n}
```

**KRITIEK:** Dit is de ENIGE betrouwbare datasource.
- `type=trade` + `side=BUY` = alle buys met price, size, conditionId, eventSlug, title, timestamp
- `type=redeem` = bevestigde WINNERS (conditionId matcht met trade)
- `positions` API met `curPrice <= 0.05` = bevestigde LOSERS
- NOOIT `closed-positions` gebruiken (survivorship bias — toont alleen winners)

**API limiet:** offset max ~3500. Pagineer tot error. Sla alles op in `data/cannae_activity_raw.jsonl` (append-only, dedup op transactionHash).

**Cannae funder adres:** Haal ALTIJD uit VPS config.yaml, nooit hardcoden.

### 1.2 Verrijking: Gamma API voor game metadata

Per unieke `eventSlug` uit de activity data:
```
GET https://gamma-api.polymarket.com/events?slug={eventSlug}
```

Sla op: `startDate`, `endDate`, `volume`, `liquidity`, `outcomes[]`, `resolved`, `winner`. Dit geeft ons:
- Game start time (voor timing analyse)
- Market resolution outcome (ground truth)
- Market volume (voor efficiëntie analyse)

Cache in `data/cannae_events_cache.json` (event slugs veranderen niet).

### 1.3 Verrijking: onze eigen trades

Uit VPS `data/trades.jsonl`: filter op `copy_wallet` = Cannae adres. Dit geeft:
- Onze entry price vs Cannae's entry price (copy delay cost)
- Welke Cannae trades we WEL vs NIET gekopieerd hebben (miss rate)

---

## Fase 2: Analyse Modules

Elk module produceert een sectie in `report.json`. Alle modules werken op dezelfde dataset.

### 2.1 Edge Decompositie

**Vraag:** Is Cannae's alpha marktselectie of prijsvoordeel?

Per resolved bet:
- `entry_price` = Cannae's gemiddelde koopprijs (gewogen uit trades)
- `close_price` = marktprijs vlak voor resolution (uit Gamma API of laatste trade timestamp)
- `fair_value` = 1.0 als gewonnen, 0.0 als verloren

Bereken:
- **Selection edge** = `fair_value - close_price` (hij kiest de goede kant)
- **Timing edge** = `close_price - entry_price` (hij koopt goedkoper dan de markt)
- **Total edge** = `fair_value - entry_price`

Aggregeer per market type en per league. Output: tabel met selection_edge en timing_edge per categorie.

### 2.2 Performance per Market Type

Per market type (win, ou, spread, draw, btts):
- Aantal resolved bets
- Wins / Losses
- Win Rate (%) met Wilson 95% CI
- ROI (%) met bootstrap CI
- Totale PnL ($)
- Gemiddelde inzet ($)

**Methode:**
1. Groepeer `/activity` trades per `conditionId`
2. Match `conditionId` tegen redeems → WIN
3. Match `conditionId` tegen positions met curPrice <= 0.05 → LOSS
4. Classificeer market type uit `title` field:
   - "Spread" in title → spread
   - "O/U" in title → ou
   - "draw" of "end in a draw" → draw
   - "Both Teams" of "BTTS" → btts
   - Anders → win

### 2.3 Performance per League

Zelfde metrics als 2.2 maar gegroepeerd op league (eerste segment van eventSlug: `nba-xxx` → `nba`).

### 2.4 Sizing als Signaal

Split resolved bets in 4 kwartielen op `usdcSize`:
- Q1 (kleinste 25%), Q2, Q3, Q4 (grootste 25%)
- Meet WR en ROI per kwartiel
- Als Q4 >> Q1 → sizing is een betrouwbaar confidence signaal
- Bereken correlatie coefficient (Spearman) tussen bet size en win/loss

### 2.5 Timing Analyse

Per BUY trade: bereken `hours_before_start` = `game_start_time - trade_timestamp`.

Buckets:
- `>24h`, `12-24h`, `6-12h`, `2-6h`, `1-2h`, `30min-1h`, `<30min`

Per bucket: WR, ROI, gemiddelde entry price.

**Impact:** Als edge geconcentreerd is in >6h bucket, dan verliest onze T-30 scheduler een groot deel van de edge.

### 2.6 Leg Correlatie per Game

Per game (eventSlug) met 2+ resolved legs:
- Tel: alle legs gewonnen, mix (sommige won/verloren), alle verloren
- Bereken Pearson correlatie tussen leg outcomes (1=win, 0=loss)
- Als correlatie > 0.7 → legs zijn sterk gecorreleerd, meer legs ≠ meer diversificatie
- Als correlatie < 0.3 → legs zijn onafhankelijk, elke leg voegt alpha toe

### 2.7 Edge Decay (rolling window)

Sorteer resolved bets op timestamp. Bereken rolling metrics:
- 30-bets rolling WR
- 30-bets rolling ROI
- Plot trend (of bewaar datapunten in JSON voor later plotten)

Check: is er een statistische daling (Mann-Kendall trend test)?

### 2.8 Copy Delay Impact

Koppel onze trades (uit `trades.jsonl`) aan Cannae's trades (uit `/activity`) op basis van:
- Zelfde `conditionId` OF zelfde `eventSlug` + zelfde `outcome`

Per matched pair:
- `delay_seconds` = onze timestamp - Cannae's timestamp
- `price_slippage` = onze price - Cannae's price
- `pnl_impact` = slippage * onze shares

Aggregeer: totale slippage cost, gemiddelde delay, worst case delay.

### 2.9 Miss Rate

Welke Cannae bets kopiëren we NIET en waarom?

- Pak alle Cannae bets op leagues in onze config
- Match tegen onze trades op conditionId
- Niet-gekopieerde bets: categoriseer reden (league filter, min_price filter, timing miss, dedup, max_open_bets)
- WR van gemiste bets vs gekopieerde bets — missen we de goede of de slechte?

---

## Fase 3: Ongoing Monitoring (dagelijks draaien)

### 3.1 Daily Snapshot

Draai dagelijks (cron of handmatig):
1. Fetch nieuwe activity records (append to raw JSONL, dedup op transactionHash)
2. Fetch nieuwe event metadata (update cache)
3. Run alle analyse modules
4. Sla op als `history/YYYY-MM-DD.json`

### 3.2 Drift Alerts

Vergelijk vandaag's metrics met 7-dagen en 30-dagen gemiddelden:

| Metric | Alert als |
|--------|----------|
| WR (7d rolling) | < 70% (was 85%) |
| ROI (7d rolling) | < 20% (was 61%) |
| Trades per dag | < 5 of > 100 (anomalie) |
| Nieuwe league focus | >30% trades in league die niet in onze config zit |
| Sizing shift | Mediaan bet size verandert >50% |
| Timing shift | Gemiddelde hours_before_start verandert >3h |

Output: `alerts[]` array in daily report. Als alert → log WARNING.

### 3.3 Strategie Aanbevelingen

Op basis van de data, genereer automatisch:
- **League aanbeveling:** "NBA toevoegen? 79% WR, $153K PnL, maar niet in config"
- **Market type aanbeveling:** "O/U als 2e leg? +87% ROI"
- **Max legs aanbeveling:** "3 legs ipv 2? Legs zijn onafhankelijk (correlatie=0.2)"
- **Timing aanbeveling:** "T-30 is te laat. 60% van edge zit in >6h bucket"

---

## Fase 4: Output Format

### report.json structuur

```json
{
  "generated_at": "2026-03-24T13:00:00Z",
  "data_range": {"from": "2026-01-15", "to": "2026-03-24"},
  "total_bets_analyzed": 174,
  "overall": {
    "wr": 0.851, "roi": 0.608, "pnl": 234707,
    "wr_ci_95": [0.79, 0.90],
    "wilson_ci": [0.79, 0.90]
  },
  "by_market_type": { ... },
  "by_league": { ... },
  "edge_decomposition": {
    "selection_edge_avg": 0.15,
    "timing_edge_avg": 0.08
  },
  "sizing_signal": {
    "q1_wr": 0.78, "q4_wr": 0.92,
    "spearman_r": 0.31, "p_value": 0.002
  },
  "timing": {
    "buckets": { ">24h": {"wr": 0.88, "roi": 0.55}, ... }
  },
  "leg_correlation": {
    "pearson_r": 0.45,
    "all_win_pct": 0.60, "mixed_pct": 0.30, "all_loss_pct": 0.10
  },
  "edge_decay": {
    "trend": "stable",
    "mann_kendall_p": 0.45,
    "rolling_30": [ ... ]
  },
  "copy_delay": {
    "avg_delay_seconds": 180,
    "avg_slippage_pct": 2.3,
    "total_slippage_cost": 456
  },
  "miss_rate": {
    "total_cannae_bets_in_our_leagues": 120,
    "we_copied": 85,
    "we_missed": 35,
    "missed_wr": 0.80,
    "copied_wr": 0.86
  },
  "alerts": [],
  "recommendations": []
}
```

---

## Uitvoering

### Script: `research/cannae_quant_analysis/analyze.py`

```
python3 research/cannae_quant_analysis/analyze.py
```

- Draait op VPS (heeft toegang tot config.yaml + trades.jsonl)
- Schrijft `report.json` + `history/YYYY-MM-DD.json`
- Idempotent: kan meerdere keren per dag draaien
- Geen dependencies buiten standaard Python 3.12 + httpx

### Cron (optioneel, later)

```
0 8 * * * cd /opt/bottie && python3 research/cannae_quant_analysis/analyze.py
```

---

## Regels

1. **NOOIT `closed-positions` gebruiken** — survivorship bias. Alleen `/activity` type=redeem voor winners.
2. **Cannae adres uit config.yaml** — nooit hardcoden.
3. **Dedup op transactionHash** — activity endpoint kan duplicates geven bij paginering.
4. **Wilson CI voor win rates** — niet naïeve percentages bij kleine samples.
5. **Geen speculatieve conclusies** — als data insufficient is, zeg dat. Minimum 30 bets per categorie voor conclusies.
6. **Append-only raw data** — nooit overschrijven, altijd bijschrijven. Dedup bij analyse, niet bij opslag.
7. **Event cache = immutable** — resolved events veranderen niet. Alleen nieuwe events toevoegen.
