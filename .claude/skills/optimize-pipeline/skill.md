---
name: optimize-pipeline
description: "Analyseer en optimaliseer de copy trading pipeline voor snelheid en efficiëntie"
---

# /optimize-pipeline — Workflow Optimizer

Analyseert de trading pipeline (detect → evaluate → execute) voor bottlenecks. Elke seconde sneller = meer edge.

**Gebaseerd op:** agency-agents/testing-workflow-optimizer (aangepast voor trading bot)

**Status: GEPARKEERD** — Poll cycle bottleneck is bekend. Pas relevant bij pipeline wijzigingen.

---

## Gebruik

```
/optimize-pipeline          — Volledige pipeline analyse
/optimize-pipeline latency  — Latency per stap meten
/optimize-pipeline skips    — Waarom worden trades geskipt?
```

---

## Stap 1: Pipeline Stappen Mappen

```
1. POLL: Fetch /positions per wallet (elke 15s cycle)
   ├── Rate limit delay per wallet
   ├── API response time
   └── N wallets × delay = totale cycle time

2. DETECT: Snapshot-diff voor nieuwe posities
   ├── Price drift check (>10% = skip)
   └── First-poll seeding (skip)

3. EVALUATE: Consensus + confidence berekening
   ├── Recent bets aggregatie
   ├── Wallet win rate lookup
   └── Sport multiplier

4. FILTER: Pre-execution checks
   ├── Min price check
   ├── Event dedup
   ├── Position dedup
   ├── Cooldown (5 min)
   ├── Resolution days check
   ├── "Up or down" filter
   └── Slippage check (25%)

5. SIZE: Position sizing
   ├── Copy base size (3% bankroll)
   ├── Min bet ($3.50)
   └── Max bet (5%)

6. RISK: Risk management
   ├── Bankroll check
   ├── Daily loss limit
   ├── Max open bets
   └── Per-wallet limit

7. EXECUTE: Order placement
   ├── Fee rate fetch
   ├── EIP-712 signing
   ├── CLOB API call
   └── Fill confirmation
```

---

## Stap 2: Latency Meten

```bash
# Totale poll cycle time
ssh root@45.76.38.183 'journalctl -u bottie --since "10 min ago" --no-pager 2>/dev/null | grep "POLL COMPLETE" | tail -5'

# Hoe lang duurt een volledige wallet poll cycle?
# Met N wallets en delay_per_wallet = min(60000/N, 2000)ms
# N=18 wallets → delay = min(3333, 2000) = 2000ms per wallet → 36s cycle
```

### Bottleneck Analyse

| Stap | Verwachte latency | Meetbaar? | Optimaliseerbaar? |
|------|-------------------|-----------|-------------------|
| Poll cycle | ~36s (18 wallets × 2s) | Ja (POLL COMPLETE logs) | Ja: parallel polls, minder wallets |
| API response | ~200-500ms | Nee (niet gelogd) | Nee (Polymarket bepaalt) |
| Snapshot-diff | <1ms | Nee | Nee (al snel) |
| Consensus calc | <1ms | Nee | Nee |
| Order placement | ~1-3s | Ja (FILLED logs) | Beperkt |

**Grootste bottleneck:** Poll cycle time. Met 18 wallets duurt een volledige scan ~36s. Een wallet die handelt wordt gemiddeld na 18s gedetecteerd.

---

## Stap 3: Skip Analyse

```bash
# Waarom worden trades geskipt? (afgelopen 6 uur)
ssh root@45.76.38.183 'journalctl -u bottie --since "6 hours ago" --no-pager 2>/dev/null | grep "SKIP:" | sed "s/.*SKIP: //" | cut -d" " -f1-5 | sort | uniq -c | sort -rn | head -15'

# Waarom worden trades rejected?
ssh root@45.76.38.183 'journalctl -u bottie --since "6 hours ago" --no-pager 2>/dev/null | grep "RISK REJECTED:" | sed "s/.*RISK REJECTED: //" | cut -d" " -f1-5 | sort | uniq -c | sort -rn | head -10'
```

### Skip Diagnose

| Skip reden | Normaal? | Actie als te veel |
|------------|----------|-------------------|
| already open position | Ja | Normaal — dedup werkt |
| price moved too much | Soms | Check drift threshold (nu 10%) |
| event slug conflict | Ja | Event dedup werkt |
| cooldown | Soms | 5 min misschien te lang? |
| min_price | Ja na change | Verwacht na min_price=0.20 |
| max_open_bets | Probleem | Te veel open → capital locked |
| daily_loss_limit | Probleem | Bot stopt met handelen |

---

## Stap 4: Optimalisatie Aanbevelingen

```markdown
# Pipeline Optimalisatie — [datum]

## Huidige Performance
- Poll cycle: ~XXs (XX wallets × XXs delay)
- Gemiddelde detectie-latency: ~XXs
- Skip rate: XX% van signalen
- Belangrijkste skip reden: [reden] (XX%)

## Bottlenecks
1. [Bottleneck]: [impact] → [oplossing]
2. ...

## Quick Wins (geen code changes)
- [Config aanpassing]: [verwacht effect]

## Structurele Verbeteringen (code changes)
- [Wijziging]: [verwacht effect] → via /experiment
```

---

## Regels

| DO | DON'T |
|----|-------|
| Meet voordat je optimaliseert | Aannames over bottlenecks |
| Focus op detectie-snelheid | Micro-optimalisaties die niets uitmaken |
| Skip analyse voor inzicht | Skips als "fout" behandelen |
| Verbeteringen via /experiment | Blind tunen |
