# Council — BTC 5M @ $8/side 24u P&L projectie

**Datum:** 2026-04-16
**Vraag:** Klopt mijn 24u P&L projectie (worst 2% −$250, mediaan +$500, best 5% +$4K, maand +$8-15K) bij bankroll $1,428?

---

## Vijf adviseurs

### 1. De Contrariaan — "De projectie heeft drie fatale fouten"

- **Tail is 2.4× te optimistisch.** HARVESTER had 1 dag −$7,257 op $17K in 34 dagen = 3% frequentie (niet 2%), en schaal naar jou = −$600 (niet −$250).
- **Mediaan is survivorship bias.** HARVESTER deed 3.5%/dag ROI. Jij projecteert 35%/dag = 10× HARVESTER efficiency op 12× minder kapitaal. Onmogelijk.
- **FATALE FOUT:** 137 fills × $8 = $1,096 capital churn/dag op $1,428 bankroll = **77% utilization**. HARVESTER had $17K buffer. Jij bent **12× undercapitalized** voor dezelfde strategie.
- **Rekensom:** 137 fills × 6% blended WR × 1c edge = $0.64/dag gross. Waar komen die honderden dollars vandaan?

### 2. De Eerste Principes Denker — "Je optimaliseert de verkeerde variabele"

- Vrager vraagt mediaan P&L. Onderliggende vraag = Kelly / risk-of-ruin.
- P(ruin in 30d) bij huidige setup = **15-30%**.
- Exposure/bankroll > 70% = hefboom op een 5-6% WR systeem.
- 5.6-7.1% WR = **adverse selection edge**, niet directionele voorspelling.
- Juiste vraag: "Wat is max sizing waarbij P(ruin 90d) < 5% EN groei > 0?" Antwoord: **$3-5/side, niet $8.**

### 3. De Expansionist — "Je denkt te klein"

- Vrager plant maand +$8-15K; HARVESTER template = 100× ($200 → $21K in 34d).
- Hefbomen: (1) compound autoscaler → dag-30 $16K, dag-60 $180K. (2) Parallelle markten ETH/SOL/XRP/DOGE = 4×. (3) 15m/1h timeframes lagere competitie. (4) Tier-4/Tier-0 experiment. (5) Margin/lending = 2× effective bankroll.
- Blinde vlek: vrager denkt in dag-PnL, HARVESTER dacht in exponent.

### 4. De Buitenstaander — "Dit is gokken met decimalen"

- Jargon ongedefinieerd: "1c/2c/3c", "T-60s", "GIYN", "HARVESTER".
- Aannames ongetoetst: HARVESTER = representatief (survivorship), 34 dagen = ruis, "-$250 worst" is fantasie.
- Alarmbellen: 35%/dag = loterij. 560-1050%/maand = miljardair binnen jaar als waar.
- Cruciale vraag: kun je emotioneel een −$600 dag om 05:00 's nachts aan?
- **Eerlijke framing:** dit is gestructureerd gokken met edge-hypothese. Noem het zo.

### 5. De Uitvoerder — "Meet eerst, projecteer daarna"

**Actieplan 48u:**
1. CSV logging vandaag: net PnL per 4h, fills/uur. Triggers: fills<80/24h = edge droogt op, PnL<+$300/72h = projectie fout, stop bij 2 opeenvolgende dagen <−$150.
2. A/B test $8 vs $5 over 24h. Je hebt fill-data op $5 (137/24h, 53% T-60), op $8 NIET.
3. Hardcode stop-loss: daily PnL <−$300 → auto-pause, bankroll <$1,000 → auto-disable.

---

## Peer Review — anoniem

**Sterkst: C (Contrariaan)** — unanimiem. Enige antwoord met **falsifieerbare rekensom** tegen echte benchmark.

**Grootste blinde vlek: E (Expansionist)** — 4/5 reviewers. Adviseert leverage + compound + 4× parallelle markten op een strategie **waarvan de edge-hypothese nog niet bewezen is**. Schaalt mogelijk negatieve EV exponentieel op. "Gevaarlijkst van de vijf."

**Wat ALLEN misten (universele blinde vlek):**

> **Niemand eiste de bron van "$500/dag mediaan".**
> Is dit backtest, paper, extrapolatie van HARVESTER, of live? Zonder dat is elke discussie (C's falsificatie, B's Kelly, A's triggers, D's scepsis, E's leverage) **gebouwd op een ongevalideerd getal**.
>
> **De eigen bot draait al — live fill-log staat op VPS.**
> Geen enkele adviseur vroeg om die data. Ze debatteerden HARVESTER-analogie terwijl de ground truth in `/opt/bottie-test/data/fivemin_bot/trades.jsonl` en `journalctl -u fivemin-bot` staat.
>
> **Eerste vraag had moeten zijn: "Wat zegt MIJN live data?"** niet "klopt de projectie?"

---

## Chairman Synthese

### Consensus
De projectie is mathematisch inconsistent met HARVESTER als referentie. 77% kapitaal-utilization op $1,428 bankroll is gevaarlijk. Tail risk is 2.4× onderschat. Mediaan is 10× HARVESTER efficiency = ordegrootte-fout.

### Kernconflict
C (stop en valideer) vs E (scale aggressief). **C wint**. Je kunt geen strategie opschalen waarvan de edge niet is bewezen. E's hefbomen zijn pas relevant na 30 dagen empirische +EV bevestiging.

### Blinde vlek
Alle adviseurs (inclusief ik als chairman eerst) debatteerden aannames in plaats van ze te valideren. De enige juiste volgende stap is het eigen live trade-log ontginnen.

### Verdict
**De projectie is wrong. Niet een beetje — een factor 10 op mediaan, een factor 2.4 op tail.**

Sizing op $8/side met $1,428 bankroll is **boven full Kelly** en genereert vermoedelijk negatieve geometrische groei zodra de eerste −$600 dag komt (P~3% per dag = verwacht binnen 33 dagen).

### Verdere stappen
1. **Nu: bet-size terug naar $5/side.** $8 was op basis van een projectie die niet klopt.
2. **Morgen: 72h CSV + per-trade logging** met win rate, edge per tier, fill timing.
3. **Week 1-2: meet empirische daily P&L distributie.** Dan pas opnieuw projecteren met *jouw* getallen, niet HARVESTER's.
4. **Pas scale verhogen na:** 14 dagen +EV EN bankroll > $2,500 EN P(ruin) bereken < 5%.

### Eerste stap vandaag
Revert `BET_USD_PER_SIDE = 5.00`, herstart fivemin-bot. Tijdens dit schrijven draait $8 al — elke minuut kost potentieel geld op ongevalideerde math.

---

**Metadata**
- Vraag: P&L projectie validatie
- Datum: 2026-04-16
- 5 adviseurs + 5 peer reviewers (10 agents totaal)
- Unanimiteit op "sterkst": C
- Unanimiteit op "blinde vlek": niemand vroeg naar live data source
