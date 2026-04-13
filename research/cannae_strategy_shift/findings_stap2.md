# Stap 2 — Cannae per-leg ROI baseline (findings)

**Datum:** 2026-04-07
**Script:** `analyze_per_leg.py`
**Output:** `/tmp/cannae_perleg.out`

---

## Datasets

| Bron | Periode | n bets | Leg-resolutie |
|---|---|---|---|
| `cannae_closed_full.csv` (lokaal) | 2026-01-08 → 2026-03-20 | 16,761 | Per outcome-rij |
| PM `/activity?type=trade+redeem` (fresh) | 2026-03-21 → 2026-04-07 | 136 | Per `conditionId` (REDEEM event is condition-level, sentinel `outcomeIndex=999`) |

**API-cap:** PM `/activity` weigert offset > 3500 (HTTP 400). Gevolg: het recente venster bevat alleen de laatste ~3500 trade-events; bij ~140 trades/dag betekent dat goede coverage voor de afgelopen ~17 dagen, maar 2360 trades resolveerden in slechts 255 unieke conditions waarvan 136 met PnL te bouwen waren. Het recente venster is dus statistisch te klein voor harde uitspraken per (sport, leg).

---

## Hoofdbevindingen

### 🚨 H1 BEVESTIGD voor NBA (sterk), VERWORPEN voor voetbal

**Hauptbet-leg distributie per sport (% van games waar leg X de grootste bet kreeg):**

| Sport | Periode | win | draw | spread | ou | btts |
|---|---|---|---|---|---|---|
| **NBA** | lang (jan-mar 12) | **85%** | – | 7% | 7% | – |
| **NBA** | week 03-13..03-20 | **45%** ⚠️ | – | **31%** ⚠️ | **23%** ⚠️ | – |
| **NBA** | recent 03-21..04-07 (n=3 games, anekdotisch) | 67% | – | 33% | 0% | – |
| Voetbal | lang | 48% | 7% | 4% | 35% | 6% |
| Voetbal | week 03-13..03-20 | 46% | 9% | 9% | 33% | 3% |
| Voetbal | recent 03-21..04-07 | 47% | 7% | 2% | 31% | 13% |

**NBA hauptbet zakt van 85% → 45% win in één week** (delta 40pp). Spread + OU samen stijgen van 14% → 54%. Dit is een kwantitatief bewezen strategie-shift, gebeurd in week 03-13..03-20 — niet pas vorige week.

Voetbal: hauptbet-mix is **stabiel**. Geen shift waarneembaar.

### H2 BEVESTIGD voor NBA — leg-ROI's draaien dramatisch

**NBA per-leg ROI (CSV):**

| Leg | Lang (n) | ROI lang | Week 03-13..03-20 (n) | ROI week | Δ |
|---|---|---|---|---|---|
| win | 764 | **−0.2%** | 112 | **+15.7%** | **+15.9pp** |
| spread | 486 | +7.9% | 214 | **+27.8%** | +19.9pp |
| ou | 658 | +6.3% | 268 | **+24.2%** | +17.9pp |
| **NBA totaal** | 1,908 | +2.0% | 594 | **+21.4%** | +19.4pp |

Wilson LB van NBA totaal recent: 48.7% — net niet boven 50%, maar de ROI-kop (+21.4% op n=594) is robuust.

**Voetbal per-leg ROI:** Volatiel maar geen vergelijkbare shift. Spread ging juist achteruit (week ROI −9.4%, n=315). OU draaide van +1.3% naar +14.1%, BTTS +14.0%, draw 60→55% — kleine variatie binnen ruis.

### H3 — nog niet getest in deze stap

Stap 4 vergt cross-referencing met Bottie's eigen trades + T5 POSITIONS — separate vervolgstap.

---

## Caveat: survivorship bias in football WIN/DRAW

CSV toont voetbal `win` WR = **99.9%** (n=3371) en `draw` WR = **99.9%** (n=1455). Dit is statistisch onmogelijk en wijst op bias: waarschijnlijk bevat de CSV alleen rijen voor outcomes die op-chain geredeemd zijn, dus alleen winnende posities op binaire markten. NBA en NHL `win` zitten op realistische 52-57% WR, dus de bias is voetbal-specifiek (mogelijk veroorzaakt door hoe de oorspronkelijke fetch is gebouwd).

Concreet: ROI-cijfers voor voetbal `win`/`draw` zijn **niet vergelijkbaar** met de andere legs. NBA-cijfers zijn wél betrouwbaar. Voor de hauptbet-distributie maakt de bias minder uit — die telt unieke games en voor een hauptbet-toewijzing maakt het niet uit of de bet won of niet (alleen of hij geboekt is).

Dit is dezelfde bias die `regime_report.md` van 2026-03-24 al noemt.

---

## Conclusie

1. **NBA:** Cannae's strategie is **week 03-13..03-20 al gekanteld**. Hauptbet shift van win → spread+ou is dramatisch en kwantitatief bevestigd. NBA-totaal-ROI klimt van +2% naar +21%. De Instituto-Defensa anekdote (06-04, hauptbet OU >$6K) past in een trend die 3+ weken bezig is.
2. **Voetbal:** Hauptbet-mix is **niet** verschoven. De hypothese dat Cannae globaal van win/draw naar OU/spread is gegaan klopt niet voor voetbal.
3. **Implicatie voor Bottie:**
   - **NBA-gate semantiek (per-leg ML, na fix `0a076b7`)** mist nu het signaal dat Cannae's totale game-conviction biedt. Als 55% van NBA-games hauptbet=spread/ou is, dan vertelt z'n ML-stake alleen niet veel over conviction in dat spel.
   - **Volgende stap H3 testen op Bottie's eigen NBA-trades (Stap 4)** wordt cruciaal: correleert `total_game_cv` (oude gate) beter met Bottie's success dan `copyable_leg_cv` (huidige gate)? Als ja → revert/heroverweeg `0a076b7`.
   - **Nieuwe vraag:** kunnen we OU- en spread-legs in NBA *gaan kopiëren*? Wilson LB +52% voor spread en +52% voor ou over lange historie zijn solide. Dat is buiten scope van deze stap maar wel het natuurlijke vervolg van bevinding 1.

---

## Stap 1 → Stap 2 doorlooppunten

- ✅ Data-bronnen werken (CSV + API).
- ⚠️ Recent venster te klein voor harde uitspraken per leg; de signal komt uit week 03-13..03-20 in CSV — geen verdere fresh-fetch nodig om H1 te ondersteunen.
- ⚠️ Voetbal-WR survivorship bias bekend; ROI-cijfers voor `win`/`draw` voetbal niet bruikbaar.

**Volgende beslissing voor user:** door naar **Stap 3** (hauptbet-shift over tijd, dag-resolutie grafiek) of direct naar **Stap 4** (H3 op Bottie data)?
