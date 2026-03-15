---
name: experiment
description: "Rigoureus experiment design en tracking voor elke strategie-wijziging aan Bottie"
---

# /experiment — Experiment Tracker

Elke wijziging aan Bottie's strategie is een experiment. Deze skill dwingt statistische discipline af: hypothese → sample size → success criteria → tracking → go/no-go beslissing.

**Gebaseerd op:** agency-agents/project-management-experiment-tracker (aangepast voor Polymarket trading)

---

## Gebruik

```
/experiment nieuw "min_price naar 0.30"     — Ontwerp nieuw experiment
/experiment status                           — Alle lopende experimenten
/experiment check [naam]                     — Resultaten evalueren
/experiment stop [naam]                      — Experiment stoppen (met reden)
```

---

## Stap 1: Experiment Ontwerp

Bij `/experiment nieuw "[beschrijving]"`:

### A. Hypothese formuleren

```markdown
## Experiment: [naam]
**Datum:** YYYY-MM-DD
**Hypothese:** [Testbare voorspelling]
**Nulhypothese:** De wijziging heeft geen effect op PnL/WR
```

### B. Baseline meten (ALTIJD VPS data!)

```bash
# Haal actuele data op van VPS
scp root@45.76.38.183:/opt/bottie/data/trades.jsonl /tmp/bottie_trades_baseline.jsonl

# Bereken baseline metrics
python3 -c "
import json, math
trades = [json.loads(l) for l in open('/tmp/bottie_trades_baseline.jsonl') if l.strip()]
# Filter: alleen live copy trades, geen pre-14 maart crypto
live = [t for t in trades if not t.get('dry_run') and t.get('result') in ('win','loss')]
wins = [t for t in live if t['result'] == 'win']
n = len(live)
wr = len(wins)/n if n else 0
pnl = sum(t.get('pnl',0) for t in live)
ev = pnl/n if n else 0
avg_w = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
avg_l = sum(t['pnl'] for t in [t for t in live if t['result']=='loss'])/len([t for t in live if t['result']=='loss']) if [t for t in live if t['result']=='loss'] else 0
print(f'Baseline: n={n}, WR={wr:.1%}, PnL=\${pnl:.2f}, EV=\${ev:.2f}/trade')
print(f'Avg win: \${avg_w:.2f}, Avg loss: \${avg_l:.2f}')
"
```

### C. Sample size berekenen

```python
# Minimale sample size voor 80% power, 95% confidence
# Detectie van 10 procentpunt WR verschil
import math
def sample_size(baseline_wr, expected_wr, alpha=0.05, power=0.80):
    z_alpha = 1.96  # 95% confidence
    z_beta = 0.84   # 80% power
    p1, p2 = baseline_wr, expected_wr
    p_avg = (p1 + p2) / 2
    n = ((z_alpha * math.sqrt(2 * p_avg * (1 - p_avg)) +
          z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2) / ((p1 - p2) ** 2)
    return math.ceil(n)
```

**Vuistregels voor Bottie:**
- WR verschil van 10pp detecteren: ~80-150 trades nodig
- WR verschil van 5pp detecteren: ~300-600 trades nodig
- Bij ~20 trades/dag: minimaal 4-7 dagen voor 10pp, 15-30 dagen voor 5pp

### D. Success criteria definiëren

```markdown
**Primary metric:** [WR / EV per trade / totaal PnL]
**Success threshold:** [bijv. WR > 62% OF EV > $0.50/trade]
**Guardrail metrics:** [bijv. max drawdown niet >20%, trades/dag niet <10]
**Minimum sample:** [N trades]
**Maximum duur:** [X dagen — stop als niet genoeg trades]
**Early stopping:** Stop als PnL < -$X (bescherm bankroll)
```

### E. Opslaan

Sla experiment op in `data/experiments/[naam].json`:

```json
{
  "name": "min_price_030",
  "hypothesis": "Min price verhogen van 0.20 naar 0.30 verbetert EV door longshot-losses te elimineren",
  "created": "2026-03-15",
  "status": "active",
  "baseline": {
    "n": 197,
    "win_rate": 0.589,
    "ev_per_trade": -0.14,
    "total_pnl": -27.49,
    "measured_at": "2026-03-15"
  },
  "success_criteria": {
    "primary_metric": "ev_per_trade",
    "threshold": 0.50,
    "min_sample": 80,
    "max_duration_days": 14,
    "early_stop_loss": -30.0
  },
  "config_changes": {
    "sizing.min_price": {"from": 0.20, "to": 0.30}
  },
  "results": []
}
```

---

## Stap 2: Experiment Checken

Bij `/experiment check [naam]`:

### A. Haal actuele data op

```bash
scp root@45.76.38.183:/opt/bottie/data/trades.jsonl /tmp/bottie_trades_check.jsonl
```

### B. Filter trades NA experiment startdatum

```python
# Alleen trades na experiment start, live, resolved
experiment_trades = [t for t in trades
    if t['timestamp'] >= experiment['created']
    and not t.get('dry_run')
    and t.get('result') in ('win', 'loss')]
```

### C. Bereken Wilson CI

```python
def wilson_ci(wins, n, z=1.96):
    if n == 0: return 0, 0, 0
    p = wins/n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    spread = z * math.sqrt((p*(1-p) + z*z/(4*n)) / n) / denom
    return p, max(0, center-spread), min(1, center+spread)
```

### D. Rapporteer

```markdown
## Experiment: [naam] — Check [datum]

**Status:** [RUNNING / EARLY_STOP / READY_FOR_DECISION]
**Trades sinds start:** N / [minimum required]
**Dagen actief:** X / [maximum]

### Resultaten
| Metric | Baseline | Huidig | 95% CI | Threshold |
|--------|----------|--------|--------|-----------|
| WR | XX% | XX% | [XX-XX%] | >XX% |
| EV/trade | $X.XX | $X.XX | — | >$X.XX |
| PnL | $X.XX | $X.XX | — | — |

### Beslissing
- [ ] Te weinig data (N < minimum) → WACHTEN
- [ ] CI bevat baseline → NIET SIGNIFICANT, wachten
- [ ] CI boven threshold → GO (implementeer permanent)
- [ ] CI onder baseline → NO-GO (rollback)
- [ ] Early stop triggered (PnL < -$X) → STOP + ROLLBACK
```

---

## Stap 3: Go/No-Go Beslissing

**GO criteria (ALLE moeten waar zijn):**
1. Minimum sample size bereikt
2. Primary metric boven threshold
3. 95% CI ondergrens boven baseline OF boven 0
4. Geen guardrail metrics geschonden

**NO-GO criteria (EEN is genoeg):**
1. Early stop triggered
2. Na max duur: primary metric niet significant beter
3. Guardrail metric geschonden

**Bij GO:** Documenteer resultaat, markeer als permanent
**Bij NO-GO:** Rollback config change, documenteer waarom

---

## Regels

| DO | DON'T |
|----|-------|
| ALTIJD baseline meten voor de change | Conclusies trekken zonder baseline |
| ALTIJD sample size berekenen | "Na 5 trades ziet het er goed uit" |
| ALTIJD VPS data gebruiken (scp!) | Lokale/tmp bestanden gebruiken |
| Verifieer tegen dashboard | Alleen eigen berekeningen vertrouwen |
| Eén experiment tegelijk per dimensie | Twee dingen tegelijk veranderen |
| Early stop bij groot verlies | Bankroll opbranden voor statistiek |
| Resultaat opslaan in data/experiments/ | Alleen in het gesprek bespreken |

---

## Anti-patterns

1. **"80% WR op 5 trades!"** → Nee. CI is 38-96%. Je weet niets.
2. **"Laten we 3 dingen tegelijk veranderen"** → Nee. Eén variabele per keer.
3. **"Het voelt alsof het beter gaat"** → Nee. Laat de data spreken.
4. **Config change zonder experiment** → Elke change is een experiment. Geen uitzonderingen.
