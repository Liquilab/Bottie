# Bottie — Polymarket BTC 5M Bot

## Actieve Strategie (2026-04-16)

**100% focus: BTC 5M Up/Down lottery op GIYN wallet.**
**Alle copy trading is gestopt.** De Rust `bottie` copy-trader service is `inactive + disabled` (systemctl). Alle whale-copy logica (Cannae, kch123, FazeS1mple, CBB_Edge, texaskid_mlb, kahe_cs2, TennisEdge) is passief. De multi-wallet sweet spot pipeline draait niet meer.

### Huidige setup

- **Service:** `fivemin-bot.service` (Python) op bottie-test dir, funder = GIYN (`0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a`)
- **Markt:** BTC Up or Down 5-minuten windows (288/dag)
- **Sizing:** $5 fixed per kant per window ($10 max exposure)
- **Tier ladder:** 70% @ 1c / 15% @ 2c / 15% @ 3c (beide kanten per window)
- **Cancel logica:**
  - First fill op één kant → cancel alle orders op opposite kant
  - T-60s → cancel unfilled 1c + 2c (na T-60s = 0% WR per HARVESTER data)
  - T-30s → cancel unfilled 3c
- **Exit:** hold to resolution (5 min window close) — auto-redeem via `ralph.py` cron elke 15 min
- **Skim:** 0% (compound-first tot bankroll > $1,500)

### Waarom geen copy trading meer

1. **Sport-copy edges droogden op** (Cannae voetbal W15-W16, Elkmonkey, ELK)
2. **BTC 5M werkt op een ander mechanisme:** lottery op 1c crashes → $1 recovery (~3% base rate × 100× payoff = +80% EV per bet)
3. **Focus > diversificatie** bij kleine bankroll ($1,389)

## Architectuur (huidige relevant)

| File | Doel |
|------|------|
| `scripts/fivemin_bot.py` | Main bot — ordering, fills, cancel-logica, resolution |
| `scripts/fivemin_profit_skim.py` | 2×/dag cron, 0% skim tot bankroll-gate |
| `scripts/fivemin_transfer_usdc.py` | Safe execTransaction voor skim (GIYN) |
| `scripts/ralph.py` | Auto-redeem winnende posities (cron 15 min) |

Rust copy-trader files (`main.rs`, `copy_trader.rs`, `scheduler.rs`, `sizing.rs`, `budget.rs`, etc.) zijn **dormant**. Niet aanraken tenzij we bewust terugkeren naar copy trading.

## Operationeel

- **VPS:** Vultr root@78.141.222.227
- **Service:** `systemctl {start|stop|restart} fivemin-bot`
- **Config:** `/opt/bottie-test/scripts/fivemin_bot.py` zelf (hardcoded constants, geen yaml)
- **Logs:** `journalctl -u fivemin-bot -f`
- **Trades:** `/opt/bottie-test/data/fivemin_bot/trades.jsonl`
- **Dashboard:** `/5m` is mobiele homepage, desktop via `/t/<token>/5m`

### Crons

```
*/5 * * * *   fivemin-bot heartbeat   (via systemd, geen cron)
0 6,18 * * *  fivemin_profit_skim.py  (2×/dag, 0% nu)
*/15 * * * *  ralph.py                (redemptions)
```

## Scale-gates

```
Bankroll < $1,000:  blijf $5/side
$1,000 + 7 dagen netto+:   $6/side
$1,500 + 7 dagen netto+:   $10/side + skim 10%
$3,000:                    skim 25%
```

## Als we ooit terug willen naar copy trading

1. `systemctl enable bottie && systemctl start bottie`
2. Verifieer `config.yaml` is up-to-date
3. Update deze CLAUDE.md terug

Voor nu: **BTC 5M is de enige actieve strategie.**
