---
name: bankroll
description: "Bankroll management: risk of ruin, compounding projecties, $10K goal tracking"
---

# /bankroll — Finance Tracker

Bankroll management en goal tracking. Monte Carlo simulatie voor risk of ruin. Compounding projecties naar $10.000.

**Gebaseerd op:** agency-agents/support-finance-tracker (aangepast voor Polymarket trading)

---

## ⚠️ KRITIEKE RESOLUTION MECHANICS

Elk 5m BTC Up/Down window resolvet compleet. Binnen ~15 min (ralph.py cron) zijn er **0 shares in wallet** van dat window. Winners → $1/share USDC. Losers → $0 en weg.

- `bankroll` (USDC balance) = **complete P&L**. Geen "unrealized" gap, geen open position value.
- Bankroll delta over elke periode = echte P&L in die periode. Projecties moeten hierop gebaseerd zijn, niet op share-counting.
- Monte Carlo / risk of ruin: gebruik gerealiseerde trade outcomes uit `closed-positions` `realizedPnl`, niet `curPrice` (onbetrouwbaar op resolved markets per memory).

---

## Gebruik

```
/bankroll               — Huidige stand + projectie
/bankroll risk          — Risk of ruin analyse
/bankroll project       — Compounding projectie naar $10K
/bankroll sizing        — Position sizing evaluatie
```

---

## Stap 0: Actuele Data (ALTIJD VPS + USER)

**Vraag de user EERST:**
```
1. Huidige portfolio waarde op Polymarket? (screenshot of bedrag)
2. Deposits/withdrawals sinds start?
3. Totaal geïnvesteerd?
```

```bash
# On-chain USDC
ssh root@78.141.222.227 'journalctl -u bottie --no-pager 2>/dev/null | grep "SYNC.*balance" | tail -1'

# Actuele trade data
scp root@78.141.222.227:/opt/bottie/data/trades.jsonl /tmp/bottie_bankroll.jsonl
```

---

## Stap 1: Huidige Stand

```markdown
## Bankroll Status — [datum]

| Metric | Waarde |
|--------|--------|
| Geïnvesteerd | $XXX |
| Portfolio waarde (user) | $XXX |
| On-chain USDC | $XXX |
| Rendement | +/-XX% |
| Open posities | XX |
| Resolved trades | XX (XXW/XXL) |
| Win rate | XX% [CI: XX-XX%] |
| Avg win | $X.XX |
| Avg loss | $X.XX |
| Win/loss ratio | X.XX:1 |
| Break-even WR | XX% |
| Afstand tot doel ($10K) | $X,XXX |
```

---

## Stap 2: Risk of Ruin

Monte Carlo simulatie op basis van actuele metrics:

```python
import random, math

def risk_of_ruin(bankroll, win_rate, avg_win, avg_loss, n_trades=1000, simulations=10000, min_bankroll=5.0):
    busts = 0
    final_bankrolls = []
    for _ in range(simulations):
        br = bankroll
        for _ in range(n_trades):
            if random.random() < win_rate:
                br += avg_win
            else:
                br += avg_loss  # avg_loss is negative
            if br <= min_bankroll:
                busts += 1
                break
        final_bankrolls.append(br)

    ruin_pct = busts / simulations * 100
    median_final = sorted(final_bankrolls)[simulations // 2]
    p10 = sorted(final_bankrolls)[simulations // 10]
    p90 = sorted(final_bankrolls)[9 * simulations // 10]
    reach_10k = sum(1 for b in final_bankrolls if b >= 10000) / simulations * 100

    return {
        'ruin_pct': ruin_pct,
        'median_final': median_final,
        'p10': p10,
        'p90': p90,
        'reach_10k_pct': reach_10k
    }
```

### Rapporteer

```markdown
## Risk of Ruin (Monte Carlo, 10K simulaties)

| Scenario | Bij [N] trades |
|----------|----------------|
| Kans op bust (<$5) | XX% |
| Mediaan bankroll | $XXX |
| 10e percentiel (worst case) | $XXX |
| 90e percentiel (best case) | $XXX |
| Kans op $10K bereiken | XX% |
```

---

## Stap 3: Compounding Projectie

```markdown
## Projectie naar $10K

**Aannames:** WR=XX%, AvgW=$X.XX, AvgL=$X.XX, XX trades/dag

| Dag | Bankroll (mediaan) | Kans op $10K |
|-----|-------------------|-------------|
| 7 | $XXX | XX% |
| 30 | $XXX | XX% |
| 90 | $XXX | XX% |
| 180 | $XXX | XX% |

**Break-even scenario:** Bij WR=XX% (huidige) groeit bankroll met ~$X/dag
**Verbeterd scenario:** Bij WR=XX% (+5pp) groeit bankroll met ~$X/dag
**Om $10K in 90 dagen:** WR van XX% nodig bij huidige sizing
```

---

## Stap 4: Position Sizing Evaluatie

```markdown
## Sizing Check

| Metric | Huidig | Aanbeveling |
|--------|--------|-------------|
| Base size | X% ($X.XX) | — |
| Min bet | $3.50 | — |
| Max bet | X% ($X.XX) | — |
| Open posities | XX / 200 max | — |
| Kapitaal in open posities | ~$XXX (XX% van portfolio) | — |

**Bij bankroll $XX:**
- 3% = $X.XX [boven/onder min_bet → min_bet overschrijft]
- Effectieve bet size: altijd $3.50 (tot bankroll > $117)
```

---

## Regels

| DO | DON'T |
|----|-------|
| VRAAG user voor portfolio waarde | Zelf berekenen uit trades |
| Monte Carlo met actuele metrics | Deterministische projecties |
| Toon risk of ruin prominent | Alleen upside scenario's |
| Vermeld aannames expliciet | Doen alsof projecties feiten zijn |
| Gebruik VPS trade data | Lokale kopieën |
