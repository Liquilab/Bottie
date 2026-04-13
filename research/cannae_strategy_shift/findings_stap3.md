# Stap 3 — Hauptbet-leg verschuiving over tijd (findings)

**Datum:** 2026-04-07
**Script:** `stap3_hauptbet_timeline.py`
**Output:** `/tmp/stap3.out`

---

## Headline

**NBA's strategie-transitie startte 2026-02-16 (W08) en was af 2026-03-09 (W11). Voetbal heeft geen vergelijkbare shift.**

---

## NBA — wekelijkse hauptbet-distributie

| ISO-week | n games | win | spread | ou | %WD | %SOB |
|---|---|---|---|---|---|---|
| 2026-W02 (jan 5-11) | 28 | **100%** | 0% | 0% | 100% | 0% |
| 2026-W03 (jan 12-18) | 49 | **100%** | 0% | 0% | 100% | 0% |
| 2026-W04 (jan 19-25) | 45 | **100%** | 0% | 0% | 100% | 0% |
| 2026-W05 (jan 26-feb 1) | 45 | **100%** | 0% | 0% | 100% | 0% |
| 2026-W06 (feb 2-8) | 49 | **100%** | 0% | 0% | 100% | 0% |
| 2026-W07 (feb 9-15) | 31 | **100%** | 0% | 0% | 100% | 0% |
| **2026-W08 (feb 16-22)** | **35** | **77%** | **9%** | **14%** | **77%** | **23%** ⚡ |
| 2026-W09 (feb 23-mar 1) | 50 | 60% | 22% | 18% | 60% | 40% |
| 2026-W10 (mar 2-8) | 52 | 60% | 17% | 23% | 60% | 40% |
| **2026-W11 (mar 9-15)** | **52** | **46%** | 35% | 19% | **46%** | **54%** ✅ |
| 2026-W12 (mar 16-22, partial) | 32 | 47% | 28% | 25% | 47% | 53% |

**De kanteling is een 4-weken transitie:** W07 → W11 (feb 15 → mar 15). Niet "vorige week", niet "incidenteel", maar **structureel en compleet**. NBA is nu permanent een mixed-leg portefeuille.

```
NBA hauptbet evolutie:
W02-W07  ████████████████████████████████████████████ WIN-only (100%)
W08      ██████████████████████████████████████░░░░░░ ←── eerste shift
W09-W10  ██████████████████████████░░░░░░░░░░░░░░░░░░
W11-W12  ████████████████████░░░░░░░░░░░░░░░░░░░░░░░░ ←── stabiel ~46% win
```

---

## Voetbal — geen shift

| ISO-week | n | %WD | %SOB |
|---|---|---|---|
| 2026-W02 | 106 | 81% | 19% |
| 2026-W03 | 189 | 66% | 34% |
| 2026-W04 | 269 | 69% | 31% |
| 2026-W05 | 422 | 52% | 48% |
| 2026-W06 | 389 | 52% | 48% |
| 2026-W07 | 368 | 51% | 49% |
| 2026-W08 | 340 | 51% | 49% |
| 2026-W09 | 404 | 50% | 50% |
| 2026-W10 | 320 | 52% | 48% |
| 2026-W11 | 385 | 55% | 45% |
| 2026-W12 | 94 | 55% | 45% |

Voetbal stabiliseerde **al begin februari** (W05) op ~50/50 win+draw vs spread+ou+btts. Sindsdien zit het binnen 50-55% W+D — pure ruis. Geen recente shift.

---

## Wat dit betekent

### 1. De hypothese van "afgelopen 7 dagen shift" is verkeerd geframed
De Instituto-Defensa anekdote (06-04, hauptbet OU >$6K) leek te suggereren dat Cannae net van strategie was gewisseld. Maar de realiteit is dat **de NBA-shift al 6 weken oud is** en vóór 2026-03-15 al gestabiliseerd. Wat er gisteren in voetbal gebeurde is consistent met de al-stabiele 50/50-mix die voetbal sinds februari heeft.

### 2. De NBA-toxische zone uit de oude analyse maakt nu zin
Memory `project_nba_4k_gate.md` zegt: "NBA $2K-$4K was toxic, ROI -28%". Dat dataveld liep tot ~begin maart. **Tijdens W08-W10 was Cannae's NBA-strategie midden in de transitie** — chaos in z'n eigen sizing tussen oud (ML-only) en nieuw (mixed). Niet verwonderlijk dat het slecht presteerde voor copiers in die periode.

Vanaf W11 (mar 9) is de strategie gestabiliseerd. **De $4K gate (re-enable van vandaag) zou nu juist het beste werken op data van W11+** — niet op de chaos van W08-W10.

### 3. Bottie's huidige NBA-gate semantiek (na fix `0a076b7`)
- Oude gate: `total_game_cv >= $4K`
- Nieuwe gate (na fix): `copyable_leg_cv (alleen ML) >= $4K`

Sinds W11 is de hauptbet-leg in **54% van NBA-games géén ML maar spread/ou**. Dat betekent: bij games waar Cannae heel veel conviction heeft (dus juist de games die we willen kopiëren) is z'n ML-stake vaak relatief klein, omdat de echte conviction in de spread/ou-leg zit. **De per-leg gate filtert nu precies de high-conviction-games eruit.** Dit is bijna zeker schadelijk voor NBA copying.

### 4. Twee actiebeslissingen worden urgent

**A. NBA gate herzien (urgent):** óf oude semantiek terugbrengen (`total_game_cv`), óf de gate splitsen: `total_game_cv >= $X` AND `ml_cv > 0`. Stap 4 (H3 testen op Bottie's eigen NBA-trades) bevestigt dit kwantitatief, maar de retoriek is al sterk.

**B. Spread + OU copying voor NBA inschakelen:** elke week sinds W09 is 40-54% van Cannae's NBA-conviction in spread/ou. We laten dat allemaal liggen. Gegeven dat Cannae's NBA-spread Wilson LB +51% en NBA-ou Wilson LB +52% (Stap 2) — dit is laaghangend fruit dat we 6 weken hebben gemist.

---

## Volgende stap

**Stap 4** is nu nog dringender: cross-reference Bottie's NBA-trades sinds 2026-04-01 (T5 POSITIONS coverage) tegen Cannae's `total_game_cv` vs `copyable_leg_cv`. Als `total_game_cv` meetbaar beter correleert met Bottie's PnL → **revert de fix `0a076b7`** en herstel oude gate-semantiek.
