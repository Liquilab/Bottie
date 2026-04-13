---
name: analyse
description: "Gestructureerde trade-analyse met VPS data, Wilson CI's, kruistabellen, en confounding checks"
---

# /analyse — Trade Analyse

Haalt actuele data op van de VPS, analyseert per dimensie met betrouwbaarheidsintervallen, checkt confounding via kruistabellen, en vergelijkt altijd met het dashboard.

**Verschil met /check:** `/check` is een snelle operationele health check (draait de bot? tradeert hij?). `/analyse` is een diepgaande statistische analyse met CI's, kruistabellen, en filter simulaties. Gebruik `/check` dagelijks, `/analyse` wekelijks of bij strategie-beslissingen.

**Gebaseerd op:** agency-agents/support-analytics-reporter (aangepast voor Polymarket trading)

---

## Gebruik

```
/analyse                  — Volledige analyse (alle dimensies)
/analyse wallets          — Per-wallet breakdown
/analyse markttypes       — Per market type (match_winner, O/U, spread, draw, other)
/analyse experiment [naam] — Analyse specifiek experiment
/analyse portfolio        — Review van user's eigen open PM-posities vs Cannae's leg-mix (zie onder)
```

---

# MODE: portfolio

**Doel:** Beoordeel de user's actuele open posities op Polymarket als een top expert. Check per leg of het aligned is met Cannae's hauptbet-structuur, of de relatieve weights kloppen, en bereken EV via scenario-analyse met implied probabilities.

**Wanneer gebruiken:** User deelt een screenshot/lijst van open PM-posities en vraagt "beoordeel mijn inzet" of "is dit goed" of een variant daarvan.

## Stap P1: Parse de Posities

Extract uit screenshot of user-input:
- Market title + slug-guess
- Outcome (Yes/No, team name)
- Shares + avg buy price
- Cost + current value + unrealized PnL

Bouw een tabel: `[slug, market, outcome, cost, avg, current, pnl]`.

**Als slug niet zichtbaar is:** probeer variaties (`aus-xxx`, `ucl-xxx`, `nba-xxx`, `tur-xxx`) en verifieer via gamma `/events?slug=...`.

## Stap P2: Cannae Alignment Check (VERPLICHT per leg)

**Voor ELKE leg** in de portefeuille:

```bash
python3 scripts/cannae_leg_lookup.py <slug>       # voor live open games
python3 scripts/cannae_historical_recon.py --remote <slug>  # voor resolved games
```

Registreer per leg:
- **Cannae's hauptbet** voor dat game (market + outcome + avg prijs + $ stake + % share)
- **Cannae's full leg-mix** (alle legs met share-of-game)
- **Aligned?** Scored als: ✅ hauptbet match / ⚠️ secondary leg / ❌ niet in Cannae's legs

**Als Cannae het game niet heeft:** Markeer als **"fabricated — no Cannae signal"** en vermeld memory-rule `feedback_cannae_conviction_must_be_verified.md`. Dit is een hard finding, niet een opinie.

## Stap P3: Weights vs Cannae Ratio

Per game met ≥2 legs: bereken jouw weight-verhouding en Cannae's weight-verhouding. Flag afwijkingen >20pp als **"oversized"** of **"undersized"**.

Voorbeeld: Cannae 60% Liverpool No / 17% PSG Yes. User 77% / 23%. → PSG Yes licht oversized, maar binnen tolerantie.

## Stap P4: Scenario-Analyse

Voor het geheel aan actieve legs (skip dead positions zoals curPrice<2c):

1. Lijst alle mogelijke outcome-combinaties (per game: home win / draw / away win)
2. Per scenario: welke legs winnen, welke verliezen, netto PnL vs total stake
3. Probabilities uit Cannae's implied prices (1 − hauptbet_price voor "X wint niet", etc.)
4. **EV = Σ P(scenario) × PnL(scenario)**

Presenteer als tabel met kolommen: Scenario | PnL | Probabiliteit. Sorteer op probabiliteit desc.

Flag scenario's waar PnL < −50% van stake als **"tail risk"**.

## Stap P5: Draw-Overlay Check (voetbal)

Als user heeft een directe Draw Yes positie: check of Cannae daar convictie op heeft.

- Cannae expliciete Draw Yes > 5% van hauptbet → toegestaan
- Cannae impliciete Draw (via WIN_NO van beide teams) → toegestaan maar flag als "impliciete dekking, geen directe Draw"
- Cannae heeft geen Draw-exposure → memory-rule `feedback_draw_only_if_cannae_has_it.md` overtreden, verplicht vermelden

## Stap P6: Rapporteer (Format)

```markdown
# Review portfolio — [datum/tijd]

## Overzicht
Totaal stake: $X | Huidige waarde: $Y | Unrealized: $Z (W%)
[Tabel: leg | cost | avg | current | Cannae match]

## Per-leg alignment-check
### ✅ Leg 1 — [naam]
Cannae hauptbet: [market/outcome @avg, $stake, share%]
User: [cost, avg]
Verdict: [PERFECT | OVERSIZED | UNDERSIZED | MISALIGNED | FABRICATED]

### ❌ Leg 2 — [...]
[...]

## Weights vs Cannae-ratio
[Per game met ≥2 legs, verhouding tabel]

## Scenario-analyse
[Tabel: Scenario | PnL | P(%) | tail-risk flag]
Expected value: $X (verwacht ≈ break-even / +$Y / −$Y)

## Draw-check (indien van toepassing)
[Toegestaan / overtreding + memory-rule referentie]

## Verdict top expert
**Goed:** [wat klopt]
**Fout:** [wat moet worden aangepast]
**Concreet advies:**
1. [Sell/buy actie met exacte bedragen]
2. [...]
3. [Laat staan wat goed is]

## Memory-rules gerespecteerd?
- feedback_cannae_conviction_must_be_verified.md: [✅/❌]
- feedback_draw_only_if_cannae_has_it.md: [✅/n.v.t.]
- feedback_hauptbet_strategy.md: [✅/❌]
```

## Portfolio-mode regels

| DO | DON'T |
|----|-------|
| Voor ELKE leg cannae_leg_lookup.py draaien | Conviction-labels zonder tool-output |
| Retract direct als je een leg fout leest | Doorduwen op een foute interpretatie |
| Check weights, niet alleen leg-namen | "Is aligned" roepen zonder weight-check |
| Scenario-EV berekenen met implied probs | "Voelt goed" zonder EV |
| Dead positions (cur<2c) als sunk cost flaggen | Hedge-voorstellen op dood geld |
| Concrete sell/buy bedragen in advies | Vage "je zou kunnen..." |

## Portfolio-mode known failures

### PF1: Fabricated structural critique (2026-04-08)
- **Niveau:** Interpretatie
- **Wat:** Bij eerste review van een WIN_NO + DRAW combo interpreteerde ik de structuur als "dubbele Draw, fundamenteel fout". In werkelijkheid is WIN_NO + DRAW exact Cannae's draw-heavy voetbal hauptbet-structuur.
- **Impact:** User gefrustreerd, analyse waardeloos, vertrouwen geschaad.
- **Root cause:** Ik had niet eerst `cannae_leg_lookup.py` gedraaid met de correcte slug (probeerde `aleague-` ipv `aus-`), zag "geen data" en sprong naar "fabricated" zonder tweede poging.
- **Fix:** Stap P2 is nu VERPLICHT met slug-fallback check. Fabricated-verdict alleen na minstens 3 slug-varianten geprobeerd.

### PF2: Structuur ≠ wiskunde (2026-04-08)
- **Niveau:** Interpretatie
- **Wat:** Bij WIN_NO + Draw Yes combo claimde ik "overlap, dubbele Draw betaald". Dat klopt niet — het is een deliberate concentration op Draw, geen overlap.
- **Fix:** Voor combinaties van legs altijd eerst de **outcome-payoffs uitrekenen per scenario** vóór je "structureel fout" roept. Als EV ≥ break-even is bij fair pricing, is de structuur valide.

---

# MODE: default (bulk trade analyse)

## Stap 0: VRAAG de User voor Echte Cijfers (VERPLICHT)

**VOORDAT je iets analyseert, vraag:**
```
Voordat ik analyseer, heb ik de echte cijfers nodig:
1. Portfolio waarde op PM? (rechtsboven op polymarket.com)
2. Cash (available to trade)?
3. Deposits/withdrawals sinds de start?
4. Totaal geïnvesteerd?
```

**PM is de ENIGE source of truth voor PnL.**
- Rendement = (portfolio nu - totaal gestort) / totaal gestort
- trades.jsonl bevat phantom trades, dubbele logs, en verkeerde PnL — NOOIT als totaalcijfer gebruiken
- trades.jsonl WEL gebruiken voor: relatieve vergelijkingen (wallet A vs B), WR per categorie, patronen
- NOOIT aannames maken over bedragen — als je het niet weet, VRAAG het

---

## Stap 1: Data Ophalen (ALTIJD VPS!)

```bash
# NOOIT lokale bestanden gebruiken. ALTIJD van VPS.
scp root@78.141.222.227:/opt/bottie/data/trades.jsonl /tmp/bottie_trades_analyse.jsonl
```

**Verificatie:** Vergelijk totaal aantal trades met dashboard. Als ze niet matchen → STOP, zoek de bron van het verschil.

---

## Stap 2: Data Filteren

```python
# Standaard filters:
# 1. Exclude pre-14 maart crypto Up/Down (handmatige trades)
# 2. Exclude dry runs (apart rapporteren als relevant)
# 3. Alleen resolved trades voor performance metrics
# 4. Alle trades (incl. open) voor volume metrics
```

---

## Stap 3: Analyse per Dimensie

Voor ELKE dimensie rapporteren:

| Kolom | Vereist |
|-------|---------|
| n | Altijd |
| W/L | Altijd |
| WR | Altijd |
| 95% CI (Wilson) | Altijd |
| PnL | Altijd |
| EV/trade | Altijd |
| Avg Win | Bij n >= 10 |
| Avg Loss | Bij n >= 10 |

### Wilson CI berekening

```python
import math
def wilson_ci(wins, n, z=1.96):
    if n == 0: return 0, 0, 0
    p = wins/n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    spread = z * math.sqrt((p*(1-p) + z*z/(4*n)) / n) / denom
    return p, max(0, center-spread), min(1, center+spread)
```

### Dimensies

1. **Overall** (live vs dry vs totaal)
2. **Per Wallet** (met wallet namen uit config.yaml)
3. **Per Market Type** (match_winner, over_under, spread, draw, other)
4. **Per Price Bucket** (<0.20, 0.20-0.35, 0.35-0.50, >0.50)
5. **Per Consensus** (0, 1, 2, 3+)
6. **Per Delay bucket** (als signal_delay_ms > 0)

---

## Stap 4: Kruistabellen (Confounding Check)

**Verplicht bij elke analyse.** Voorkomt dat dezelfde verliezen dubbel geteld worden.

Minimaal:
- Wallet x Market Type
- Wallet x Delay
- Market Type x Price Bucket
- Consensus x Market Type

Alleen combinaties met n >= 3 tonen. Sorteer op PnL.

**Vraag bij elke bevinding:** "Is dit een onafhankelijk effect, of is het dezelfde groep trades die in meerdere dimensies opduikt?"

---

## Stap 5: Filter Simulaties

Test combinaties van filters op de resolved data:

```python
filters = [
    ('Baseline', {}),
    ('Filter A alleen', {filter_a}),
    ('Filter B alleen', {filter_b}),
    ('A + B', {filter_a, filter_b}),
    ('Alle filters', {all_filters}),
]
```

Per combinatie: n, W/L, WR, 95% CI, PnL, EV/trade.

**Let op overlap:** Als "alle filters" hetzelfde resultaat geeft als "A + B" → filter C voegt niets toe (confound).

---

## Stap 6: Rapporteer

### Format

```markdown
# Trade Analyse — [datum]

## Echte Stand (PM = source of truth)
Portfolio: $XXX (user) | Gestort: $XXX | Rendement: +/-XX%
Cash: $XXX | Open posities: XX

## Databron
VPS trades.jsonl: [N] trades totaal, [N] live resolved
⚠️ trades.jsonl PnL is NIET betrouwbaar als totaalcijfer (phantom trades, dubbele logs)
Alleen gebruiken voor relatieve vergelijkingen (wallet A vs B, markttype X vs Y)

## Overall (relatief, niet absoluut)
[tabel met live, dry, totaal]
Break-even WR bij huidige win/loss ratio: X%

## Per Dimensie
[tabellen met CI's]

## Kruistabellen
[top 5 verliezend, top 5 winnend]
Confounding: [welke bevindingen zijn dezelfde trades?]

## Filter Simulaties
[tabel]
Overlap: [welke filters zijn redundant?]

## Conclusies
[Alleen conclusies waar n >= 30 EN CI niet de baseline bevat]
[Bij n < 30: "indicatief, meer data nodig"]

## Aanbevelingen
[Tier 1: voldoende evidence]
[Tier 2: sterk signaal, kleine n]
[Tier 3: afwachten]
```

---

## Regels

| DO | DON'T |
|----|-------|
| VRAAG user voor PM portfolio + deposits | Aannames maken over bedragen |
| PM portfolio waarde = source of truth | trades.jsonl PnL als totaalcijfer gebruiken |
| trades.jsonl voor RELATIEVE vergelijking | trades.jsonl voor ABSOLUTE PnL |
| ALTIJD scp van VPS | Lokale/tmp bestanden gebruiken |
| ALTIJD Wilson CI's tonen | Win rates zonder CI rapporteren |
| ALTIJD kruistabellen voor confounding | Dimensies als onafhankelijk behandelen |
| Bij n < 30: "indicatief" | Sterke conclusies op kleine n |
| Filter overlap checken | Effecten als additief presenteren |
| REKENEN met echte cijfers | Aannames of schattingen presenteren als feit |

---

## Known Failures

### F1: Lokale data i.p.v. VPS (2026-03-15)
- **Niveau:** Instructie (stap 1)
- **Wat:** Analyse draaide op verouderd lokaal bestand (`tmp/aireview/trades.jsonl`), 85% data ontbrak
- **Impact:** Alle conclusies fout — Cannae "33% WR" was eigenlijk 57.5%, O/U "19% WR" was 54%, match winners "75% WR" was 59%
- **Root cause:** Stap 1 zei niet expliciet genoeg "NOOIT lokaal". Skill had geen guard check.
- **Fix:** Stap 1 herschreven met expliciete `scp` command + verificatie tegen dashboard
- **Gerelateerd:** `/save` doet geen VPS sync → lokale data is altijd stale

### F2: Price bucket grenzen (2026-03-15)
- **Niveau:** Instructie (stap 3)
- **Wat:** Originele buckets (<0.20, 0.20-0.35, 0.35-0.50, >0.50) suggereerden dat 0.35-0.50 een "sweet spot" was
- **Impact:** In werkelijkheid was >0.50 het winstgevende bucket. De bucket-indeling maskeerde dit.
- **Status:** Buckets aangepast naar (<0.30, 0.30-0.50, >0.50) in analyse van 15 maart. Skill nog niet geüpdatet.

### F3: STATUS log vs analyse discrepantie (2026-03-15)
- **Status:** Gesloten — STATUS telt live+dry resolved samen

### F4: trades.jsonl PnL als absoluut cijfer (2026-03-15)
- **Niveau:** Instructie (rapport)
- **Wat:** Analyse presenteerde trades.jsonl PnL (-$86) als waarheid. PM toonde +$30 winst na bijstorting.
- **Impact:** Compleet verkeerd beeld — bot leek verliesgevend terwijl hij winst maakte
- **Root cause:** trades.jsonl bevat phantom trades, dubbele logs, fees niet meegeteld. Geen verificatie tegen PM.
- **Fix:** Stap 0 toegevoegd: VRAAG user voor PM portfolio + deposits. PM = source of truth.

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-03-15 | Add condition | "NOOIT lokale bestanden" + scp command in stap 1 | F1: 85% data ontbrak bij lokale analyse |
| 2026-03-15 | Add condition | Verificatie tegen dashboard verplicht | F1: foute conclusies niet opgemerkt |
| 2026-03-15 | Add condition | Kruistabellen verplicht | Confounding niet gecheckt → effecten dubbel geteld |
| 2026-03-15 | Reorder | Wilson CI's bij elke tabel, niet optioneel | Conclusies op kleine n zonder CI's |
| 2026-03-16 | Add condition | Stap 0: VRAAG user voor PM portfolio + deposits VOORDAT je analyseert | F4: trades.jsonl PnL was verkeerd, PM is truth |
| 2026-03-16 | Add condition | trades.jsonl alleen voor RELATIEVE vergelijkingen, NOOIT absoluut PnL | F4: phantom trades en dubbele logs vervuilen data |
| 2026-03-16 | Add condition | NOOIT aannames maken — als je een getal niet hebt, VRAAG het | F4: presenteerde -$86 terwijl bot +$30 maakte |
