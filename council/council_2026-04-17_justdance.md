# LLM Council — justdance Edge Analyse
**Datum:** 2026-04-17
**Vraag:** Is de edge van justdance (cheap crypto milestone bets: 66.4% WR, +$2.5M PnL, N=696) kopieerbaar en winstgevend voor ons?
**Agents:** 5 adviseurs + 5 peer reviewers (Claude Opus 4.6)

---

## Adviseurs

### 1. De Contrariaan — NIET BOUWEN
696 trades zijn NIET onafhankelijk. "BTC reach $75K/80K/85K" winnen allemaal tegelijk bij een rally. Echte N = 30-50 onafhankelijke crypto-bewegingen. Met N=40 is 66% WR niet te onderscheiden van toeval (CI: 50-82%).

Bull market = alles-wint-tegelijk. +$2.5M PnL waarschijnlijk geconcentreerd in 3-4 goede maanden. Dip bets waarschijnlijk tijdens korte dips in bull trend.

234 verliezers × $25 = $5,850 in clusters = 38% drawdown op $1,319 bankroll.

**Advies:** Paper trade 3 maanden, tel onafhankelijke events.

### 2. Eerste Principes Denker — NIET BOUWEN
Verkeerde variabele. De vraag is "snelste weg $1.3K→$10K", niet "is justdance kopieerbaar".

- Timing is de edge, niet selectie. Niet kopieerbaar via polling.
- Sizing mismatch: $25/trade met 2-4% fee drag vs $3.5K/trade.
- Milestones resolven weken/maanden. $50-75/maand = 18+ maanden naar $10K.
- Focus op sports volume met bestaande infra.

### 3. De Expansionist — BOUWEN
Categorie-expansie: universele milestone-scanner. Patroon bestaat buiten crypto (social media, GDP, entertainment).

- Twee lagen: copy + independent scaling via eigen data (CoinGecko, on-chain).
- Kelly zegt $100+/trade (12% bankroll).
- De edge is goedkope milestones, niet justdance.

### 4. De Buitenstaander — NIET BOUWEN
Data heeft te veel gaten:
1. Invested = $0 → PnL onverifieerbaar
2. Recent $1-$120 vs $2.5M historisch
3. Dip 44% WR + $710K = fat tail winners
4. Geen timestamps
5. API cap survivorship

**Advies:** Fix data gaps eerst (on-chain, 50 trades handmatig).

### 5. De Uitvoerder — DRY-RUN
Edge real maar drie killers: slippage (38-42c vs 35c), bull market regime, schaal ($2-4K/jaar).

**Actieplan:**
1. Dry-run in Bottie (2 uur dev): log trades, meet slippage
2. Evalueer na 2 weken
3. Go live als Bottie strategy, niet aparte bot

---

## Peer Reviews (5× anoniem)

### Unaniem sterkst: D (Contrariaan)
Gecorreleerde outcomes is het dodelijke argument. Alle 5 reviewers noemen dit.

### Unaniem zwakst: A (Expansionist)
Negeert alle risico's, wil schalen op ongevalideerde data. "Gevaarlijk optimistisch."

### Wat alle vijf misten:
1. **Benchmark ontbreekt** — is +$2.5M beter dan BTC houden? In 300% bull run mogelijk underperformance risicogecorrigeerd.
2. **Entry timing** — justdance koopt bij 2-5c wanneer niemand kijkt. Wij zien het pas bij 15-20c. Timing-edge niet kopieerbaar (RN1-parallel).
3. **Data audit** — invested=$0, merged-position artifacts. $2.5M verdacht zonder on-chain verificatie.
4. **Opportunity cost** — elke dev-uur crypto = uur niet aan bewezen sports edge.
5. **Marktstructuur** — wie zit aan de andere kant? Systematisch kopen beweegt prijs.

---

## Chairman Synthese

**Consensus:** Edge is statistisch onbetrouwbaar en niet kopieerbaar (4/5 adviseurs, 5/5 reviewers).

**Kernconflict:** Expansionist vs. rest. Expansionist ziet categorie-kans, Contrariaan vernietigt statistische basis. Contrariaan heeft gelijk.

**Verdict: NIET BOUWEN**

Drie fatale problemen:
1. Gecorreleerde outcomes → echte N=30-50 → WR niet significant
2. Timing-edge niet kopieerbaar via polling (RN1-parallel)
3. Data-kwaliteit onvoldoende voor go/no-go

**Eerste stap (als je het toch wilt):**
Dry-run logger in Bottie: log justdance milestone trades, meet slippage, tel onafhankelijke events. 2 uur dev, 0 risico. Na 2 weken evalueren.

---

## Metadata
- Adviseurs: 5 (Contrariaan, Eerste Principes, Expansionist, Buitenstaander, Uitvoerder)
- Peer reviewers: 5 (anoniem, shuffle A-E)
- Model: Claude Opus 4.6
- Total agents: 10
