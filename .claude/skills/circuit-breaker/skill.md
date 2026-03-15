---
name: circuit-breaker
description: "Detecteer en bescherm tegen API failures, runaway losses, en anomalieën in Bottie"
---

# /circuit-breaker — Autonomous Optimization Architect

Detecteert anomalieën en beschermt de bankroll tegen runaway losses, API failures, en onverwacht gedrag.

**Gebaseerd op:** agency-agents/engineering-autonomous-optimization-architect (aangepast voor trading bot)

**Status: GEPARKEERD** — Bestaande risk.rs dekt de basis. Pas relevant als de bot complexer wordt.

---

## Gebruik

```
/circuit-breaker             — Check alle circuit breakers
/circuit-breaker losses      — Anomalie-detectie op verliezen
/circuit-breaker api         — API health check
/circuit-breaker recommend   — Aanbevelingen voor nieuwe breakers
```

---

## Stap 1: Loss Anomalie Detectie

```bash
scp root@45.76.38.183:/opt/bottie/data/trades.jsonl /tmp/bottie_cb.jsonl

# Check verliezen afgelopen 24u vs gemiddelde
ssh root@45.76.38.183 'journalctl -u bottie --since "24 hours ago" --no-pager 2>/dev/null | grep -c "result.*loss"'
```

### Anomalie Signalen

| Signaal | Drempel | Actie |
|---------|---------|-------|
| Daily loss > 15% bankroll | Config: max_daily_loss_pct | Bot stopt automatisch |
| 5+ opeenvolgende losses | Streak alert | Onderzoek — is er iets mis? |
| Loss >2x gemiddelde loss | Grote positie verloren | Check sizing logic |
| Loss rate >70% over 20 trades | Structureel verliezend | HALT — onderzoek |
| 0 trades in 2 uur | Bot stopt met handelen | Check API, services |

### Implementatie check

```bash
# Is daily loss limit actief?
ssh root@45.76.38.183 'grep max_daily_loss /opt/bottie/config.yaml'

# Huidige daily PnL
ssh root@45.76.38.183 'journalctl -u bottie --since "today" --no-pager 2>/dev/null | grep "daily_pnl\|STATUS:" | tail -3'
```

---

## Stap 2: API Health

```bash
# Polymarket API errors afgelopen uur
ssh root@45.76.38.183 'journalctl -u bottie --since "1 hour ago" --no-pager 2>/dev/null | grep -c "positions fetch failed\|order.*failed\|API.*error"'

# Fee API failures
ssh root@45.76.38.183 'journalctl -u bottie --since "1 hour ago" --no-pager 2>/dev/null | grep -c "fee.*failed\|fee.*retry"'

# Rate limit hits
ssh root@45.76.38.183 'journalctl -u bottie --since "1 hour ago" --no-pager 2>/dev/null | grep -c "429\|rate.limit\|too many"'
```

### API Health Status

| API | Errors/uur | Status |
|-----|-----------|--------|
| Positions | <5 | HEALTHY |
| Positions | 5-20 | DEGRADED |
| Positions | >20 | UNHEALTHY — trading compromised |
| Fee | <3 | HEALTHY (retry werkt) |
| Fee | >10 | UNHEALTHY — orders falen |
| CLOB Order | Any | CHECK — elke order failure = gemist trade |

---

## Stap 3: Bestaande Beschermingen Audit

Check welke circuit breakers al actief zijn in de code:

```markdown
## Huidige Beschermingen

| Bescherming | Config | Actief? |
|-------------|--------|---------|
| Max daily loss | max_daily_loss_pct: 15% | [JA/NEE] |
| Min bankroll | min_bankroll: $5 | [JA/NEE] |
| Max open bets | max_open_bets: 200 | [JA/NEE] |
| Price drift filter | 10% in copy_trader.rs | [JA/NEE] |
| Slippage cap | 25% in execution.rs | [JA/NEE] |
| Event dedup | open_slugs check | [JA/NEE] |
| Crypto Up/Down filter | title filter | [JA/NEE] |
| Min price | min_price: 0.20 | [JA/NEE] |
| Fee retry | parse from error msg | [JA/NEE] |
```

---

## Stap 4: Aanbevelingen

```markdown
## Ontbrekende Circuit Breakers

| Breaker | Wat het voorkomt | Prioriteit |
|---------|-----------------|-----------|
| [naam] | [scenario] | [hoog/midden/laag] |

## Aanbevolen Config Wijzigingen
| Parameter | Huidig | Aanbevolen | Reden |
|-----------|--------|-----------|-------|
| [param] | [waarde] | [waarde] | [reden] |
```

---

## Regels

| DO | DON'T |
|----|-------|
| Check bestaande breakers eerst | Nieuwe toevoegen zonder te weten wat er is |
| Rapporteer anomalieën | Auto-stoppen zonder user akkoord |
| Vergelijk met historische rates | Eenmalige spike als structureel zien |
| Focus op bankroll bescherming | Over-engineeren voor edge cases |
