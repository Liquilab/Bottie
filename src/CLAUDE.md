# Bottie — Polymarket Sports Copy Trading Bot

## Actieve Strategie (2026-03-26)
Cannae copy-trading met **proportionele sizing**. Doel: **ROI MAXIMALISATIE.**

### Sizing: Proportioneel
```
our_usdc = bankroll × (cannae_leg / cannae_game_total) × conviction × 8%
```
- **leg_weight**: proportie van Cannae's game total op deze leg
- **conviction**: best / (best + second) per conditionId
- **8%**: max_bet_pct
- **Skip als < $2.50** (PM minimum)

### Market Types
| Sport | Types | Data (16.761 Cannae posities) |
|-------|-------|------|
| Voetbal | [win, draw] | Win +34.7% ROI, Draw +37.4% ROI |
| US sports | [win, spread] | Win +34.7% ROI, Spread +6.4% ROI |
| OU | **UIT** | 1.8% ROI op 6.642 trades = coin flip |
| BTTS | **UIT** | 0.1% ROI = waardeloos |

### Capital Recycling
Voetbal resolved ~22:45 CET → kapitaal hergebruikt voor US sports ~01:00 CET.
Deployment cap 90% geldt per moment, niet cumulatief.

### Risk Guards
- max_deployment_pct: 90%
- daily_loss_limit: 15%
- min_bankroll: $50
- min_bet: $2.50 (skip, niet bump)

## Architectuur
| File | Doel |
|------|------|
| `main.rs` | Entry, loops, T-30/T-5 scheduler, conviction berekening |
| `sizing.rs` | `proportional_size()` — leg_weight × conviction × max_pct |
| `execution.rs` | `execute_proportional()`, condition_id dedup |
| `budget.rs` | Flight board logging, recycling estimate |
| `risk.rs` | Deployment cap, daily loss, bankroll limits |
| `config.rs` | All config structs, hot-reload |

## Operationeel
- VPS: Vultr root@45.76.38.183
- Binary: `/opt/bottie/bottie-bin` (systemd service)
- Build ALTIJD op VPS: `source ~/.cargo/env && cargo build --release`
- Config: `/opt/bottie/config.yaml` (hot-reloadable)
- Logs: `journalctl -u bottie -f`
- Trades: `data/trades.jsonl`
- Evaluatie: `/opt/bottie/scripts/evaluate_experiment.sh` (VPS cron 9:03 CET)
