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
```

---

## Stap 1: Data Ophalen (ALTIJD VPS!)

```bash
# NOOIT lokale bestanden gebruiken. ALTIJD van VPS.
scp root@45.76.38.183:/opt/bottie/data/trades.jsonl /tmp/bottie_trades_analyse.jsonl
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

## Databron
VPS trades.jsonl: [N] trades totaal, [N] live resolved
Verificatie tegen dashboard: [OK / MISMATCH — details]

## Overall
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
| ALTIJD scp van VPS | Lokale/tmp bestanden gebruiken |
| ALTIJD Wilson CI's tonen | Win rates zonder CI rapporteren |
| ALTIJD kruistabellen voor confounding | Dimensies als onafhankelijk behandelen |
| ALTIJD vergelijken met dashboard | Alleen eigen cijfers vertrouwen |
| Live en dry apart rapporteren | Door elkaar mengen |
| Bij n < 30: "indicatief" | Sterke conclusies op kleine n |
| Filter overlap checken | Effecten als additief presenteren |

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
- **Niveau:** Tool (bot STATUS output)
- **Wat:** Bot STATUS: 523 trades / 50.7% WR / -$175. Analyse: 197 live resolved / 59% WR / -$27.49
- **Impact:** Onduidelijk welke cijfers de werkelijkheid zijn. Mogelijk telt STATUS crypto/dry runs/open positions mee.
- **Status:** Open — niet gereconcilieerd

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-03-15 | Add condition | "NOOIT lokale bestanden" + scp command in stap 1 | F1: 85% data ontbrak bij lokale analyse |
| 2026-03-15 | Add condition | Verificatie tegen dashboard verplicht | F1: foute conclusies niet opgemerkt |
| 2026-03-15 | Add condition | Kruistabellen verplicht | Confounding niet gecheckt → effecten dubbel geteld |
| 2026-03-15 | Reorder | Wilson CI's bij elke tabel, niet optioneel | Conclusies op kleine n zonder CI's |
