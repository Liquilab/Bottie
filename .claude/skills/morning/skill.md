# /morning — Ochtend Kickoff

Laadt overnight state, checkt fivemin-bot (BTC 5M) productie, en presenteert een actionable ochtend briefing.

**Draai dit EERST bij het starten van een nieuwe sessie in de ochtend.**

---

## Huidige Focus (per 2026-04-16)

**100% BTC 5M via fivemin-bot.** Copy trader (Rust `bottie` / `bottie-test` services) is bewust **inactive** — niet herstarten tenzij user expliciet vraagt.

| Service | Pad | Rol |
|---------|-----|-----|
| `fivemin-bot` | `/opt/fivemin-bot/` (of pad in .service) | **Live** — BTC 5M Up/Down, 70/20/10 tier ladder (1c/2c/3c), $15/side, T-15s cancel |
| `fivemin-monitor` | — | Monitor/logging fivemin-bot |
| `dashboard` | — | /5m dashboard (cold <5s / warm ~2ms) |
| `crypto-dryrun` | — | Paper variant |
| `bottie`, `bottie-test` | legacy | **INACTIVE bewust** — niet als failure rapporteren |

### Wallets
- **Bot funder**: `0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42`
- **Prove wallet** (profit skim): separate, $1,000 target
- **HV base vergelijker**: `0x7da07b2a...` (zie `reference_hv_base_wallet.md`)

---

## ⚠️ KRITIEKE RESOLUTION MECHANICS (lezen vóór elke P&L claim)

Elk 5m BTC Up/Down window resolvet compleet. Binnen ~15 min (ralph.py cron) zijn er **0 shares in wallet** van dat window:
- Winners → auto-redeemed naar USDC
- Losers → resolven naar $0 en verdwijnen

**Gevolgen:**
- `bankroll` uit STATUS-log = **complete P&L**. Geen "unrealized" / "open position" component.
- Overnight bankroll delta = **echte** P&L overnacht.
- NOOIT "open shares verklaren drawdown" redeneren — shares bestaan niet meer na resolution.

---

## Uitvoering

### Stap 1: Laad Context (PARALLEL)

**A. Gisteren's EOD:**
```
Read: data/sessions/YYYY-MM-DD-eod.md (datum gisteren)
```

**B. Git state:**
```bash
git log --oneline -5
git status --short
git branch --show-current
```

**C. Production checks op VPS** (`ssh root@78.141.222.227`):

```bash
# Service status
systemctl is-active fivemin-bot fivemin-monitor dashboard crypto-dryrun

# Laatste STATUS (bankroll nu)
journalctl -u fivemin-bot --no-pager -n 500 | grep -E "STATUS|bankroll" | tail -5

# T-15s cancel count laatste 12u (verwacht ~100+)
journalctl -u fivemin-bot --since "12 hours ago" --no-pager | grep -c "T-15s"

# Klapper-count: winnende windows overnight
journalctl -u fivemin-bot --since "12 hours ago" --no-pager | grep -iE "WIN|RESOLVED.*win|redeem" | head -20

# Laatste 10 regels context
journalctl -u fivemin-bot --no-pager -n 10
```

### Stap 2: Compute Overnight Delta

- EOD bankroll uit `data/sessions/YYYY-MM-DD-eod.md` (gisteren)
- Nu bankroll uit laatste STATUS log
- Delta = nu − EOD. **Toon raw getallen, geen speculatie over oorzaak.**

### Stap 3: Presenteer Briefing

```
Ochtend Briefing — YYYY-MM-DD HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Production:
  fivemin-bot:     [active/inactive] | bankroll=$X
  fivemin-monitor: [active/inactive]
  dashboard:       [active/inactive]
  crypto-dryrun:   [active/inactive]

Overnight (raw):
  EOD bankroll (gisteren):  $X
  Nu bankroll:              $Y
  Delta:                    $Z  (over ~Nu)
  T-15s cancels (12u):      N
  Winnende windows:         [tel uit log, toon raw regels]

Raw STATUS log:
  [laatste 1-2 STATUS regels letterlijk]

Gisteren (uit EOD):
  [1-2 regels afgerond + open experiments]

Actief Werk / Checks:
  [uit EOD "Context voor morgen" of "In progress"]

Volgende Actie: [directe volgende stap]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Regels (ANTI-FABRICATION)

| DO | DON'T |
|----|-------|
| Toon raw log-output voor elke claim | Speculeren over oorzaak ("waarschijnlijk door X") |
| Raw bankroll getal uit STATUS | Afgeronde/geschatte cijfers |
| Accept `bottie`/`bottie-test` als inactive (normaal) | Rapporteren als "CRITICAL service down" |
| Bankroll delta = P&L (resolution mechanics) | "Open shares verklaren..." redeneren |
| Fact-only; user concludeert | Interpreteren voor user |
| Stel volgende-actie vraag als onduidelijk | Autonoom escaleren zonder data |

---

## Fallback

Als geen EOD van gisteren:
1. Check `MEMORY.md` → `project_current_focus.md`
2. `git log --since="24 hours ago"`
3. Vraag: "Geen EOD gevonden. Wat is de prioriteit?"

---

## Known Failures

### F5: Skill verwees naar dode copy-trading stack (2026-04-18)
- **Niveau:** Instructie (skill drift)
- **Wat:** `/morning` checkte Rust `bottie`/`bottie-test` services met 7 wallets, per-sport trade analyse, PM API portfolio. Per 2026-04-16 is focus 100% BTC 5M via `fivemin-bot` (Python) — Rust services zijn bewust inactive. Skill rapporteerde "CRITICAL: beide services down" terwijl dat normaal was.
- **Impact:** Briefing volledig onbruikbaar, user corrigeerde handmatig.
- **Fix:** Skill herschreven naar fivemin-bot focus. Oude copy-trading sectie verwijderd.

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-04-18 | Rewrite | Volledige skill rewrite: fivemin-bot BTC 5M focus, Rust copy-trading stack verwijderd | F5: skill drift — huidige focus sinds 16 april is 100% BTC 5M |
