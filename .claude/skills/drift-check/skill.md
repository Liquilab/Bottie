---
name: drift-check
description: "Detecteer wallet drift, calibratie-afwijkingen, en degraderende performance bij gevolgde wallets"
---

# /drift-check — Model QA Specialist

Monitort of de wallets die we volgen nog steeds doen wat we denken dat ze doen. Detecteert drift (wallet verandert strategie), calibratie-afwijkingen (onze edge-schattingen kloppen niet), en performance degradatie.

**Gebaseerd op:** agency-agents/specialized-model-qa (aangepast voor wallet copy trading)

**Status: GEPARKEERD** — Te weinig data voor meaningful drift detectie. Pas relevant bij 500+ resolved trades per wallet.

---

## Gebruik

```
/drift-check                — Volledige check alle wallets
/drift-check [wallet]       — Specifieke wallet analyseren
/drift-check calibration    — Kloppen onze confidence schattingen?
```

---

## Stap 1: Wallet Drift Detectie

**Vraag:** Handelen onze wallets nog in dezelfde markten als toen we ze selecteerden?

```bash
scp root@78.141.222.227:/opt/bottie/data/trades.jsonl /tmp/bottie_drift.jsonl
```

### Per wallet analyseren:

```python
# Vergelijk eerste helft vs tweede helft van trades per wallet
# Kijk naar verschuivingen in:
# 1. Markttype distributie (was 80% sports, nu 50% crypto?)
# 2. Prijsrange distributie (was 0.30-0.50, nu 0.05-0.20?)
# 3. Win rate trend (dalend? stijgend?)
# 4. Gemiddelde trade size (veranderd?)
# 5. Frequentie (veel actiever of juist stiller?)
```

### Drift Signalen

| Signaal | Wat het betekent | Actie |
|---------|-----------------|-------|
| Sport % daalt >20pp | Wallet verschuift naar crypto/politiek | Evalueer of we dit willen volgen |
| WR daalt >15pp | Performance degradeert | Monitor, verwijder bij aanhoudende daling |
| Frequentie >3x omhoog | Wallet is opeens hyperactief | Mogelijke bot, risico op noise |
| Frequentie >3x omlaag | Wallet stopt met handelen | Overweeg verwijderen (inactief) |
| Prijsrange verschuift | Andere strategie | Check of nieuwe range winstgevend is |

---

## Stap 2: Calibratie Check

**Vraag:** Als we een trade plaatsen met confidence=0.65, winnen we dan ~65% van de tijd?

```python
# Bucket trades op confidence en vergelijk met werkelijke WR
buckets = {
    '0.40-0.50': [],
    '0.50-0.60': [],
    '0.60-0.70': [],
    '0.70-0.80': [],
    '0.80-0.90': [],
}
# Per bucket: verwachte WR (gemiddelde confidence) vs werkelijke WR
# Afwijking > 10pp = miscalibratie
```

### Calibratie Rapport

```markdown
| Confidence Range | Verwacht WR | Werkelijk WR | n | Afwijking |
|-----------------|-------------|-------------|---|-----------|
| 0.40-0.50 | ~45% | XX% | XX | +/-XXpp |
| 0.50-0.60 | ~55% | XX% | XX | +/-XXpp |
| ... | ... | ... | ... | ... |
```

**Interpretatie:**
- Werkelijk > Verwacht → We zijn te conservatief (meer inzetten?)
- Werkelijk < Verwacht → We overschatten edge (minder inzetten!)
- Beide consistent → Calibratie klopt

---

## Stap 3: Performance Degradatie

### Rolling window analyse

```python
# Bereken WR en EV in rolling windows van 50 trades
# Plot trend: verbetert, stabiel, of verslechtert de bot over tijd?
windows = []
for i in range(0, len(trades) - 50, 10):
    chunk = trades[i:i+50]
    wr = sum(1 for t in chunk if t['result'] == 'win') / len(chunk)
    ev = sum(t.get('pnl', 0) for t in chunk) / len(chunk)
    windows.append({'start': i, 'wr': wr, 'ev': ev})
```

### Degradatie Signalen

| Signaal | Drempel | Actie |
|---------|---------|-------|
| WR trend dalend >5pp over 100 trades | Significant | Onderzoek oorzaak |
| EV trend dalend >$0.50 over 100 trades | Significant | Check wallet kwaliteit |
| Worst wallet degradeert | >10 trades, WR <35% | Overweeg verwijdering |
| Alle wallets degraderen | Breed patroon | Marktomstandigheden veranderd? |

---

## Stap 4: Rapporteer

```markdown
# Drift Check — [datum]

## Wallet Drift
| Wallet | Drift Type | Ernst | Detail |
|--------|-----------|-------|--------|
| [naam] | [type] | [laag/midden/hoog] | [beschrijving] |

## Calibratie
| Range | Verwacht | Werkelijk | Status |
|-------|----------|-----------|--------|
| ... | ... | ... | [OK/MISCALIBRATIE] |

## Performance Trend
Richting: [verbeterend / stabiel / verslechterend]
Rolling WR (laatste 50): XX%
Rolling EV (laatste 50): $X.XX

## Aanbevelingen
1. [Wallet/actie] — [reden]
```

---

## Regels

| DO | DON'T |
|----|-------|
| ALTIJD VPS data | Lokale kopieën |
| Rolling windows, niet totalen | Alleen overall gemiddelde |
| Vergelijk eerste/tweede helft | Aannemen dat wallets stabiel zijn |
| Flag drift, laat user beslissen | Auto-verwijderen |
