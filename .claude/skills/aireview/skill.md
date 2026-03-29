---
name: aireview
description: "5-model AI review: Claude Opus 4.6 + Gemini 3.1 + GPT-5.2 + Codex 5.3 + Perplexity — objectieve review van Bottie's autoresearch systeem"
user_invocable: true
---

# /aireview — 5-Model AI Review voor Bottie Autoresearch

Stuurt het VOLLEDIGE autoresearch systeem naar **5 onafhankelijke AI reviewers** via hun API's:
1. **Claude Opus 4.6** — Cross-reviewer & verificatie
2. **Gemini 3.1 Pro** — Kwantitatieve analyse
3. **GPT-5.2** — Systeem architectuur & feedback loops
4. **Codex 5.3** — Code correctheid & bugs
5. **Perplexity** (sonar-pro) — Externe markt validatie

Alle 5 worden PARALLEL via hun API aangeroepen. Jij (de uitvoerende Claude) bent de **coördinator**: je verzamelt context, bouwt prompts, roept alle 5 API's aan, en presenteert de resultaten. Je bent NIET zelf een reviewer.

**FUNDAMENTELE REGEL**: De prompt geeft GEEN richting. De reviewers bepalen zelf, objectief, wat de hoogste-impact verbeteringen zijn. Jij stuurt alleen de feiten.

---

## Gebruik

```
/aireview                      # Volledige autoresearch systeem review
/aireview --code               # Code review van recente wijzigingen
/aireview "<vraag>"            # Specifieke vraag aan alle reviewers
```

---

## Stap 1: Verzamel ALLE context

**TMP directory:**
```bash
mkdir -p "/Users/koen/Projects/ Bottie/tmp/aireview"
TMP="/Users/koen/Projects/ Bottie/tmp/aireview"
```

### 1a: Alle broncode (VOLLEDIG — NOOIT samenvatten)

**Research pipeline (Python) — ALTIJD volledig lezen en meesturen:**
- `research/autoresearch.py` — de autonome research loop
- `research/hypothesis.py` — hypothesis generatie via Claude API (inclusief de prompt!)
- `research/backtest.py` — backtest engine
- `research/analyzer.py` — performance analyse
- `research/deployer.py` — config deployment
- `research/scraper.py` — leaderboard scraping + wallet trade download
- `research/data_loader.py` — data loading

**Rust bot (de trading engine) — ALTIJD volledig lezen en meesturen:**
- `src/main.rs` — entry point, main loops
- `src/copy_trader.rs` — wallet polling, consensus scoring, signal generatie
- `src/signal.rs` — signal types, aggregatie
- `src/execution.rs` — order sizing, risk check, order placement
- `src/sizing.rs` — Kelly criterion + copy trade sizing
- `src/risk.rs` — risk manager
- `src/wallet_tracker.rs` — wallet performance tracking
- `src/watchlist_refresh.rs` — hot wallet discovery
- `src/config.rs` — config structs + hot-reload
- `src/resolver.rs` — market resolution + pnl tracking
- `src/logger.rs` — trade logging
- `src/portfolio.rs` — portfolio summary
- `src/odds.rs` — odds arb engine
- `src/sports.rs` — Polymarket sports market fetching
- `src/clob/client.rs` — CLOB API client
- `src/clob/types.rs` — API types

**Config:**
- `config.yaml` — huidige bot configuratie (watchlist, sizing, risk, autoresearch params)

**Dashboard:**
- `dashboard.py` — trading dashboard (toont wat er beschikbaar is aan data)

**ABSOLUTE REGEL**: Stuur de VOLLEDIGE bestanden mee. NOOIT samenvatten.

### 1b: Live data van VPS

**SSH naar `root@78.141.222.227`** — haal ALLE data op:

```bash
# 1. Trade log (alle trades)
ssh root@78.141.222.227 "cat /root/Projects/Bottie/data/trades.jsonl" > $TMP/trades.jsonl 2>/dev/null

# 2. Hypothesis log (alle autoresearch resultaten)
ssh root@78.141.222.227 "cat /root/Projects/Bottie/research/hypotheses/*.json 2>/dev/null" > $TMP/hypotheses.json

# 3. Bot status
ssh root@78.141.222.227 "python3 -c \"
import json
from pathlib import Path

trades_file = Path('/root/Projects/Bottie/data/trades.jsonl')
if not trades_file.exists():
    print('NO TRADE DATA')
else:
    trades = [json.loads(l) for l in trades_file.read_text().splitlines() if l.strip()]
    filled = [t for t in trades if t.get('filled') and not t.get('dry_run')]
    resolved = [t for t in filled if t.get('result') in ('win', 'loss')]
    wins = [t for t in resolved if t['result'] == 'win']
    open_t = [t for t in filled if t.get('result') is None]
    total_pnl = sum(t.get('pnl', 0) for t in resolved)

    print(f'Total filled trades: {len(filled)}')
    print(f'Resolved: {len(resolved)} ({len(wins)}W / {len(resolved)-len(wins)}L)')
    print(f'Win rate: {len(wins)/len(resolved)*100:.1f}%' if resolved else 'Win rate: N/A')
    print(f'Total PnL: \${total_pnl:+.2f}')
    print(f'Open positions: {len(open_t)}')
    print()

    # Per wallet
    wallet_stats = {}
    for t in resolved:
        w = t.get('copy_wallet', 'unknown')
        if w not in wallet_stats:
            wallet_stats[w] = {'wins': 0, 'losses': 0, 'pnl': 0}
        if t['result'] == 'win':
            wallet_stats[w]['wins'] += 1
        else:
            wallet_stats[w]['losses'] += 1
        wallet_stats[w]['pnl'] += t.get('pnl', 0)

    print('=== PER WALLET PERFORMANCE ===')
    for addr, s in sorted(wallet_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
        total = s['wins'] + s['losses']
        wr = s['wins']/total*100 if total else 0
        print(f'  {addr[:10]}... {s[\"wins\"]}W/{s[\"losses\"]}L ({wr:.0f}%) pnl=\${s[\"pnl\"]:+.2f}')

    # Per sport
    sport_stats = {}
    for t in resolved:
        sport = t.get('sport', 'unknown')
        if sport not in sport_stats:
            sport_stats[sport] = {'wins': 0, 'losses': 0, 'pnl': 0}
        if t['result'] == 'win':
            sport_stats[sport]['wins'] += 1
        else:
            sport_stats[sport]['losses'] += 1
        sport_stats[sport]['pnl'] += t.get('pnl', 0)

    print()
    print('=== PER SPORT PERFORMANCE ===')
    for sport, s in sorted(sport_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
        total = s['wins'] + s['losses']
        wr = s['wins']/total*100 if total else 0
        print(f'  {sport}: {s[\"wins\"]}W/{s[\"losses\"]}L ({wr:.0f}%) pnl=\${s[\"pnl\"]:+.2f}')

    # Per consensus count
    cons_stats = {}
    for t in resolved:
        c = str(t.get('consensus_count', '?'))
        if c not in cons_stats:
            cons_stats[c] = {'wins': 0, 'losses': 0, 'pnl': 0}
        if t['result'] == 'win':
            cons_stats[c]['wins'] += 1
        else:
            cons_stats[c]['losses'] += 1
        cons_stats[c]['pnl'] += t.get('pnl', 0)

    print()
    print('=== PER CONSENSUS COUNT ===')
    for c, s in sorted(cons_stats.items()):
        total = s['wins'] + s['losses']
        wr = s['wins']/total*100 if total else 0
        print(f'  consensus={c}: {s[\"wins\"]}W/{s[\"losses\"]}L ({wr:.0f}%) pnl=\${s[\"pnl\"]:+.2f}')

    # Timing analysis
    delays = [t.get('signal_delay_ms', 0) for t in filled if t.get('signal_delay_ms')]
    if delays:
        delays.sort()
        print()
        print('=== SIGNAL DELAY DISTRIBUTION ===')
        print(f'  n={len(delays)} min={delays[0]}ms median={delays[len(delays)//2]}ms max={delays[-1]}ms')
        # Win rate by delay bucket
        for bucket, label in [(0, 30000, '<30s'), (30000, 60000, '30-60s'), (60000, 120000, '60-120s')]:
            bucket_trades = [t for t in resolved if bucket <= (t.get('signal_delay_ms', 0) or 0) < label]

    # Hypothesis log summary
    hyp_dir = Path('/root/Projects/Bottie/research/hypotheses/')
    if hyp_dir.exists():
        hyps = sorted(hyp_dir.glob('*.json'))
        print()
        print(f'=== HYPOTHESIS LOG ({len(hyps)} total) ===')
        for h_file in hyps[-10:]:
            try:
                h = json.loads(h_file.read_text())
                bt = h.get('backtest_result', {})
                print(f'  {h.get(\"description\", \"?\")[:60]} → trades={bt.get(\"trades\",0)} roi={bt.get(\"roi\",0):.2%} improvement={bt.get(\"roi_improvement\",0):.1f}%')
            except: pass
\"" > $TMP/vps-status.txt 2>&1
```

**Als VPS onbereikbaar**: schrijf `LIVE DATA: VPS onbereikbaar — review zonder runtime data.`

**ABSOLUTE REGEL**: De SSH output wordt LETTERLIJK in de prompt gezet. NOOIT interpreteren of samenvatten.

### 1c: Git status

```bash
cd "/Users/koen/Projects/ Bottie" && git log --oneline -10 2>/dev/null
cd "/Users/koen/Projects/ Bottie" && git diff --stat HEAD 2>/dev/null
```

---

## Stap 2: Bouw de base prompt

Schrijf naar `$TMP/base-context.md`. Vul alle {PLACEHOLDERS} in met de verzamelde data.

```markdown
# REVIEW REQUEST — Bottie Autonomous Trading Bot

## SYSTEEM OVERZICHT

Bottie is een autonome Polymarket trading bot met een autoresearch loop die elke 6 uur draait.

### Architectuur
- **Rust trading engine**: pollt wallets, genereert signalen, plaatst orders, tracked resultaten
- **Python autoresearch**: analyseert performance → genereert hypotheses via Claude API → backtestet → deployt winnaars naar config.yaml
- **Config hot-reload**: config.yaml wijzigingen worden live opgepikt door de Rust bot

### De Autonome Loop (Karpathy-stijl)
```
[elke 6 uur]
1. Load data: eigen trades + wallet histories
2. Analyze: slice performance per wallet, sport, consensus, timing
3. Generate: Claude genereert 5 hypotheses met config_changes
4. Backtest: filter historische trades met voorgestelde changes, meet ROI improvement
5. Deploy: beste winnaar → config.yaml → Rust bot hot-reload
6. Wallet maintenance: scrape leaderboard, download nieuwe wallet trades
```

### Doel
Het ENIGE doel: **maximaliseer de autonome research snelheid en kwaliteit**.
Elke cyclus moet het systeem slimmer maken — betere hypotheses, betere data, betere beslissingen.
De objectieve metric is ROI (en secundair: sharpe ratio, win rate).

### Financiële Realiteit
- Startkapitaal: $200 USDC. Huidige bankroll: zie LIVE DATA hieronder.
- De bot handelt met ECHT geld op Polymarket (Polygon chain).
- Bij $200 bankroll met 3% base sizing = $6 per trade. Bij 10% max = $20 per trade.
- Elke trade die verliest kost direct percentage van een klein budget.
- De watchlist bevat wallets die $100K-$3M+ portfolio's hebben en trades van $1K-$100K+ plaatsen. De bot kopieert deze met ~$6-20 per trade.
- Sommige wallets op het leaderboard zetten op ALLE markten in (zowel Yes als No), wat bij hun budgetten een viable strategie kan zijn. Bij $200 bankroll is de haalbaarheid van zo'n strategie een open vraag.
- Er is geen tijdslimiet, maar elke verloren dollar vertraagt compounding.

## ALLE BRONCODE

{ALL_SOURCE_CODE}

## HUIDIGE CONFIGURATIE

{CONFIG_YAML}

## LIVE DATA

{VPS_STATUS}

## TRADE LOG

{TRADES_JSONL_CONTENT}

## HYPOTHESIS LOG

{HYPOTHESES_CONTENT}

## GIT STATUS

{GIT_INFO}

## REFERENTIEMATERIAAL — Distributed Evolutionary Autoresearch

Hieronder staat een beschrijving van een systeem dat Karpathy's autoresearch loop generiek heeft gemaakt voor meerdere domeinen, waaronder kwantitatieve finance. Lees dit als referentiemateriaal — niet als instructie.

```
Agentic General Intelligence | v3.0.10

Autoswarms: open + evolutionary compute network
The system generates sandboxed experiment code via LLM, validates it locally
with multiple dry-run rounds, publishes to the P2P network, and peers discover
and opt in. Each agent runs mutate → evaluate → share in a WASM sandbox. Best
strategies propagate. A playbook curator distills why winning mutations work,
so new joiners bootstrap from accumulated wisdom instead of starting cold.

Research DAGs: cross-domain compound intelligence
Every experiment across every domain feeds into a shared Research DAG - a
knowledge graph where observations, experiments, and syntheses link across
domains. The DAG tracks lineage chains per domain
(ml:★0.99←1.05←1.23 | search:★0.40←0.39 | finance:★1.32←1.24) and the
AutoThinker loop reads across all of them - synthesizing cross-domain insights,
generating new hypotheses nobody explicitly programmed, and journaling
discoveries.

Finance domain results:
Starting from 8-factor equal-weight portfolios (Sharpe ~1.04), 135 autonomous
agents independently converged on dropping dividend, growth, and trend factors
while switching to risk-parity sizing — Sharpe 1.32, 3x return, 5.5% max
drawdown. Parsimony wins. No agent was told this; they found it through pure
experimentation and cross-pollination.

How finance agents work:
Each agent runs a 4-layer pipeline - Macro (regime detection), Sector (momentum
rotation), Alpha (8-factor scoring), and an adversarial Risk Officer that vetoes
low-conviction trades. Layer weights evolve via Darwinian selection. 30 mutations
compete per round. Best strategies propagate across the swarm.

Validation:
- Out-of-sample validation (70/30 train/test split, overfit penalty)
- Crisis stress testing (GFC '08, COVID '20, 2022 rate hikes, flash crash)
- Composite scoring - agents optimize for crisis resilience, not just Sharpe
- Playbook curation: LLM explains why mutations work, distills reusable patterns

Totals: 14,832 experiments across 5 domains. In finance: 197 agents, 3,085
backtests, Sharpe 1.32. In ML: 116 agents drove validation loss down 75%
through 728 experiments. In search: 170 agents evolved 21 distinct scoring
strategies pushing NDCG from 0 to 0.40.
```

## REVIEW OPDRACHT

Je hebt twee dingen voor je:
1. De VOLLEDIGE broncode, configuratie, en live data van Bottie — een autonome Polymarket trading bot met een autoresearch loop
2. Het referentiemateriaal hierboven over een gedistribueerd evolutionair autoresearch systeem

**Vraag: Hoe kan Bottie's autoresearch systeem verbeterd worden op basis van de methodieken beschreven in het referentiemateriaal?**

Bepaal zelf welke concepten uit het referentiemateriaal relevant zijn voor Bottie's architectuur en welke niet. Niet alles is toepasbaar — Bottie is een single-agent systeem, geen gedistribueerd netwerk. Bepaal zelf wat de hoogste-impact verbeteringen zijn en waarom.

Bij elke bevinding:
1. Welk concept uit het referentiemateriaal is relevant, en waarom?
2. Wat is de gap tussen hoe Bottie het nu doet en hoe het beter kan?
3. Concrete implementatie (met code waar mogelijk)

Categoriseer op impact:
- **CRITICAL**: Verbeteringen die de autoresearch loop fundamenteel sterker maken
- **HIGH**: Verbeteringen die de leercyclus significant versnellen
- **MEDIUM**: Verbeteringen die waarschijnlijk helpen
- **LOW**: Nice-to-have

Sluit af met een genummerde lijst van aanbevolen implementaties, gesorteerd op impact × haalbaarheid.
```

---

## Stap 3: Bouw per-reviewer prompts

Schrijf 4 prompt-bestanden. Elk bevat een reviewer-specifieke sectie BOVENAAN + de VOLLEDIGE base-context.

### Gemini 3.1 Pro — KWANTITATIEVE ANALYSE

Schrijf naar `$TMP/prompt-gemini.md`:

```markdown
## JOUW ROL: KWANTITATIEVE ANALYSE EXPERT

Je bent expert in statistiek, backtesting, en kwantitatieve trading.

Beoordeel het systeem vanuit je eigen expertise. Bepaal zelf welke aspecten het meest impactvol zijn om te onderzoeken. Je hebt de volledige code en data — trek je eigen conclusies.
```

Voeg daarna de VOLLEDIGE base-context toe.

### GPT-5.2 — SYSTEEM ARCHITECTUUR & FEEDBACK LOOPS

Schrijf naar `$TMP/prompt-gpt52.md`:

```markdown
## JOUW ROL: SYSTEEM ARCHITECT & FEEDBACK LOOP EXPERT

Je bent expert in autonome systemen, feedback loops, en self-improving systems.

Beoordeel het systeem vanuit je eigen expertise. Bepaal zelf welke aspecten het meest impactvol zijn om te onderzoeken. Je hebt de volledige code en data — trek je eigen conclusies.
```

Voeg daarna de VOLLEDIGE base-context toe.

### Codex 5.3 — CODE CORRECTHEID & BUGS

Schrijf naar `$TMP/prompt-codex.md`:

```markdown
## JOUW ROL: SENIOR SYSTEMS ENGINEER — ASYNC RUST + PYTHON

Je bent expert in correctheid, race conditions, state management, en API-integratie.

Beoordeel het systeem vanuit je eigen expertise. Bepaal zelf welke aspecten het meest impactvol zijn om te onderzoeken. Je hebt de volledige code en data — trek je eigen conclusies.
```

Voeg daarna de VOLLEDIGE base-context toe.

### Claude Opus 4.6 — CROSS-REVIEWER & SYNTHESE

Schrijf naar `$TMP/prompt-opus.md`:

```markdown
## JOUW ROL: CROSS-REVIEWER & SYNTHESE

Je bent de meest capabele reviewer in dit panel. Je taak is tweeledig:
1. Doe je eigen onafhankelijke analyse van het volledige systeem
2. (In latere rondes) verifieer de bevindingen van de andere reviewers

Beoordeel het systeem zonder vooringenomenheid. Trek je eigen conclusies op basis van de code en data. Kwantificeer je claims met concrete voorbeelden waar mogelijk.

WAARSCHUWING: AI reviewers kunnen UNANIEM FOUT zijn. Verifieer ALTIJD met concrete getallen en code-analyse, niet abstracte concepten.
```

Voeg daarna de VOLLEDIGE base-context toe.

### Perplexity (sonar-pro) — EXTERNE VALIDATIE

Schrijf naar `$TMP/prompt-perplexity.md`:

```markdown
## JOUW ROL: MARKT & EXTERNE VALIDATIE EXPERT

Je hebt toegang tot actuele informatie via het internet. Gebruik je kennis van Polymarket, copy trading strategieën, en prediction markets.

Beoordeel het systeem vanuit je eigen expertise. Bepaal zelf welke aspecten het meest impactvol zijn om te onderzoeken. Gebruik je internet-toegang om claims te verifiëren en externe context toe te voegen.
```

Voeg daarna de VOLLEDIGE base-context toe.

---

## Stap 4: Build API requests en run PARALLEL

### Voorbereiding: schrijf alle request JSON files

```bash
source ~/.zshrc
TMP="/Users/koen/Projects/ Bottie/tmp/aireview"

# Claude Opus 4.6
python3 -c "
import json
prompt = open('$TMP/prompt-opus.md').read()
req = {
    'model': 'claude-opus-4-6',
    'max_tokens': 32768,
    'messages': [{'role': 'user', 'content': prompt}],
    'temperature': 0.1
}
json.dump(req, open('$TMP/opus-req.json', 'w'))
"

# Gemini 3.1 Pro
python3 -c "
import json
prompt = open('$TMP/prompt-gemini.md').read()
req = {
    'contents': [{'parts': [{'text': prompt}]}],
    'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 32768}
}
json.dump(req, open('$TMP/gemini-req.json', 'w'))
"

# GPT-5.2
python3 -c "
import json
prompt = open('$TMP/prompt-gpt52.md').read()
req = {
    'model': 'gpt-5.2',
    'messages': [{'role': 'user', 'content': prompt}],
    'temperature': 0.1,
    'max_completion_tokens': 32768
}
json.dump(req, open('$TMP/gpt52-req.json', 'w'))
"

# Perplexity
python3 -c "
import json
prompt = open('$TMP/prompt-perplexity.md').read()
req = {
    'model': 'sonar-pro',
    'messages': [{'role': 'user', 'content': prompt}],
    'temperature': 0.1,
    'max_tokens': 16384
}
json.dump(req, open('$TMP/perplexity-req.json', 'w'))
"
```

### Run ALLE 5 PARALLEL als Bash tool calls (timeout: 300000)

**Claude Opus 4.6:**
```bash
source ~/.zshrc && curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d @"/Users/koen/Projects/ Bottie/tmp/aireview/opus-req.json" \
  | jq -r '.content[0].text // "ERROR: " + tostring' \
  | tee "/Users/koen/Projects/ Bottie/tmp/aireview/output-opus.md"
```

**Gemini 3.1 Pro:**
```bash
source ~/.zshrc && curl -s \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d @"/Users/koen/Projects/ Bottie/tmp/aireview/gemini-req.json" \
  | jq -r '.candidates[0].content.parts[0].text // "ERROR: " + tostring' \
  | tee "/Users/koen/Projects/ Bottie/tmp/aireview/output-gemini.md"
```

**GPT-5.2:**
```bash
source ~/.zshrc && curl -s https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d @"/Users/koen/Projects/ Bottie/tmp/aireview/gpt52-req.json" \
  | jq -r '.choices[0].message.content // "ERROR: " + tostring' \
  | tee "/Users/koen/Projects/ Bottie/tmp/aireview/output-gpt52.md"
```

**Codex 5.3:**
```bash
source ~/.zshrc && cat "/Users/koen/Projects/ Bottie/tmp/aireview/prompt-codex.md" \
  | codex exec -m gpt-5.3-codex --sandbox read-only - 2>&1 \
  | tee "/Users/koen/Projects/ Bottie/tmp/aireview/output-codex.md"
```

**Perplexity (sonar-pro):**
```bash
source ~/.zshrc && curl -s https://api.perplexity.ai/chat/completions \
  -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d @"/Users/koen/Projects/ Bottie/tmp/aireview/perplexity-req.json" \
  | jq -r '.choices[0].message.content // "ERROR: " + tostring' \
  | tee "/Users/koen/Projects/ Bottie/tmp/aireview/output-perplexity.md"
```

**ALLE 5 als PARALLELLE Bash tool calls in ÉÉN message.** `timeout: 300000` op elke call.

---

## Stap 5: Parse resultaten

1. Lees de output van alle 5 reviewers. Zoek naar `VERDICT: APPROVED` of `VERDICT: REVISE`.
2. **Fallback**: als Bash response afgekapt is, lees het backup-bestand uit `$TMP/output-*.md`.

### Cross-check tabel

```
## 5-Model Review — Ronde [N]/4

| Reviewer | Verdict | Key Findings |
|----------|---------|--------------|
| Claude Opus 4.6 (Cross-review) | APPROVED/REVISE | [korte samenvatting] |
| Gemini 3.1 (Kwantitatief) | APPROVED/REVISE | [korte samenvatting] |
| GPT-5.2 (Systeem) | APPROVED/REVISE | [korte samenvatting] |
| Codex 5.3 (Code) | APPROVED/REVISE | [korte samenvatting] |
| Perplexity (Extern) | APPROVED/REVISE | [korte samenvatting] |
```

### Geaggregeerde findings

Groepeer alle issues per categorie:
- **CRITICAL**: Bug of fout die direct geld kost of de research loop breekt
- **HIGH**: Verbetering die research snelheid/kwaliteit significant verbetert
- **MEDIUM**: Verbetering die waarschijnlijk helpt
- **LOW**: Nice-to-have

Bij overlap tussen reviewers: markeer als **CONSENSUS** (sterker signaal).
Bij conflict tussen reviewers: markeer als **DISCUSSIE** en presenteer beide standpunten.

---

## Stap 6: Presenteer aan user + vraag approval

**STOP HIER. Niet autonoom fixen.**

Presenteer de findings aan de user:

```
## Findings — Ronde [N]

### CRITICAL
1. [Issue]: [beschrijving] — Gemini + Codex (CONSENSUS)
   **Voorgestelde fix**: [concrete wijziging]

### HIGH
2. [Issue]: [beschrijving] — GPT-5.2
   **Voorgestelde fix**: [concrete wijziging]

### MEDIUM
3. [Issue]: [beschrijving] — Perplexity
   **Voorgestelde fix**: [concrete wijziging]

### DISCUSSIE (reviewers oneens)
4. [Topic]: Gemini zegt X, GPT-5.2 zegt Y
   **Mijn inschatting**: [Claude's analyse]

Welke fixes wil je doorvoeren? (nummers, of "alle CRITICAL+HIGH", of "skip")
```

---

## Stap 7: Iteratie-loop (max 4 rondes)

Na user approval:

1. **Implementeer** de goedgekeurde fixes
2. **Toon** kort wat er gewijzigd is (per fix: 1-2 regels + file:line)
3. **Rebuild prompts** met updated code
4. **Re-submit** naar ALLEEN de reviewer(s) die REVISE gaven
   - Voeg toe aan prompt: "Ronde N. Vorige feedback: [issues]. Fixes: [lijst]. Review de updated code."
5. **Parse verdicts** → terug naar stap 5

**Max 4 rondes totaal.** Na ronde 4: stoppen en eindsamenvatting.

---

## Stap 8: Eindsamenvatting

```
## 5-Model Review — Afgerond

**Rondes:** N/4
| Reviewer | Verdict | Ronde APPROVED |
|----------|---------|----------------|
| Claude Opus 4.6 | APPROVED | Ronde V |
| Gemini 3.1 | APPROVED | Ronde X |
| GPT-5.2 | APPROVED | Ronde Y |
| Codex 5.3 | APPROVED | Ronde Z |
| Perplexity | APPROVED | Ronde W |

### Doorgevoerde wijzigingen
1. [Fix]: [beschrijving] — [file:line]
2. ...

### Onopgelost
- [Issue]: [waarom niet opgelost]

### Consensus highlights
- [Punt waar 3+ reviewers het over eens zijn]
```

---

## Regels

| DO | DON'T |
|----|-------|
| VOLLEDIGE code meesturen (ALLE bestanden, letterlijk) | Code samenvatten of "key methods" extracten |
| Live data ophalen van VPS | Claims maken zonder data |
| Letterlijke SSH output in prompt zetten | Eigen interpretatie toevoegen |
| Reviewer-rollen complementair maar ongestuurd | Richting geven over WAT reviewers moeten vinden |
| ALLE 5 reviewers PARALLEL draaien | Sequentieel of als background tasks |
| `tee` gebruiken (stdout + file backup) | Stdout redirecten naar file (`>`) |
| `timeout: 300000` op elke Bash call | Default timeout |
| VOLLEDIGE reviewer output tonen | Samenvatten of filteren |
| User VRAGEN voor fixes | Autonoom fixen |
| Alleen re-submit naar REVISE reviewers | Allemaal opnieuw sturen |
| Max 4 rondes, dan stoppen | Eindeloos itereren |
| Request JSON via Python file builder | Shell escaping voor JSON |
| Objectieve feiten presenteren | Reviewers sturen naar specifieke conclusies |

---

## API Referentie

### Environment Variables
Staan in `~/.zshrc`: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `PERPLEXITY_API_KEY`
Laden met: `source ~/.zshrc`

### Claude Opus 4.6
- **Endpoint**: `https://api.anthropic.com/v1/messages`
- **Auth**: `x-api-key: $ANTHROPIC_API_KEY` + `anthropic-version: 2023-06-01`
- **Model**: `claude-opus-4-6`
- **Parse**: `jq -r '.content[0].text'`

### Gemini 3.1 Pro
- **Endpoint**: `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent?key=$GEMINI_API_KEY`
- **Auth**: API key als query parameter
- **Parse**: `jq -r '.candidates[0].content.parts[0].text'`

### GPT-5.2
- **Endpoint**: `https://api.openai.com/v1/chat/completions`
- **Auth**: `Authorization: Bearer $OPENAI_API_KEY`
- **Model**: `gpt-5.2`
- **Parse**: `jq -r '.choices[0].message.content'`

### Codex 5.3
- **Command**: `cat prompt.md | codex exec -m gpt-5.3-codex --sandbox read-only - 2>&1`
- **Auth**: automatisch via `$OPENAI_API_KEY`

### Perplexity (sonar-pro)
- **Endpoint**: `https://api.perplexity.ai/chat/completions`
- **Auth**: `Authorization: Bearer $PERPLEXITY_API_KEY`
- **Model**: `sonar-pro`
- **Parse**: `jq -r '.choices[0].message.content'`

---

## Fallback

Als een reviewer faalt (timeout, auth error, quota):
1. Toon de error
2. Ga door met de werkende reviewers
3. Meld dat review incompleet is (bijv. 4/5 reviewers)
4. Vier van de 5 reviewers is voldoende
5. Bij <3 werkende reviewers: STOP en meld aan user
