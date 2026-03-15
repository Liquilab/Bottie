---
name: verify
description: "Post-deploy verificatie: bewijs dat de bot daadwerkelijk tradeert en wijzigingen werken"
---

# /verify — Reality Checker

**Default status: NEEDS WORK.** Bewijs het tegendeel met echte data.

Na elke deploy, config change, of experiment start: verifieer dat de bot daadwerkelijk doet wat we verwachten. Geen aannames, alleen bewijs.

**Gebaseerd op:** agency-agents/testing-reality-checker (aangepast voor Polymarket trading)

---

## Gebruik

```
/verify                — Na deploy: checkt alles
/verify trades         — Worden trades geplaatst? (grep FILLED)
/verify config         — Is de config change actief?
/verify experiment     — Draait het experiment correct?
```

---

## Stap 1: Services Draaien?

```bash
ssh root@45.76.38.183 'systemctl is-active bottie autoresearch wallet_scout 2>/dev/null'
```

**KRITISCH:** `is-active` = noodzakelijk maar NIET voldoende. Een service kan draaien zonder trades te plaatsen.

---

## Stap 2: Bot Tradeert? (BELANGRIJKSTE CHECK)

```bash
# FILLED trades in afgelopen 30 minuten
ssh root@45.76.38.183 'journalctl -u bottie --since "30 min ago" --no-pager 2>/dev/null | grep "FILLED:" | tail -5'

# Als geen FILLED: waarom niet?
ssh root@45.76.38.183 'journalctl -u bottie --since "30 min ago" --no-pager 2>/dev/null | grep -E "SKIP:|RISK REJECTED:|SIGNAL:" | tail -10'
```

| Resultaat | Status | Actie |
|-----------|--------|-------|
| FILLED gevonden | PASS | Bot tradeert |
| SIGNAL maar geen FILLED | NEEDS WORK | Orders worden niet geaccepteerd |
| SKIP/REJECT maar geen SIGNAL | NEEDS WORK | Alle signalen gefilterd |
| Helemaal niets | FAIL | Bot pollt niet of API down |

---

## Stap 3: Config Change Actief?

Na een config wijziging:

```bash
# Check of config is herladen (hot reload)
ssh root@45.76.38.183 'journalctl -u bottie --since "5 min ago" --no-pager 2>/dev/null | grep -i "config\|reload\|watchlist"'

# Verifieer specifieke waarde
ssh root@45.76.38.183 'grep "min_price" /opt/bottie/config.yaml'
```

**Bij watchlist wijzigingen:** Verifieer dat de verwijderde wallet niet meer gepolld wordt:

```bash
ssh root@45.76.38.183 'journalctl -u bottie --since "5 min ago" --no-pager 2>/dev/null | grep -i "[wallet_naam_of_adres]"'
```

---

## Stap 4: Geen Regressies?

```bash
# Error rate afgelopen uur
ssh root@45.76.38.183 'journalctl -u bottie --since "1 hour ago" --no-pager -p err 2>/dev/null | wc -l'

# Vergelijk met vorige uur
ssh root@45.76.38.183 'journalctl -u bottie --since "2 hours ago" --until "1 hour ago" --no-pager -p err 2>/dev/null | wc -l'

# Bankroll sync werkt?
ssh root@45.76.38.183 'journalctl -u bottie --since "10 min ago" --no-pager 2>/dev/null | grep "SYNC.*balance" | tail -1'
```

---

## Stap 5: Rapporteer

```markdown
# /verify Report — [datum HH:MM]

## Services
- bottie: [PASS/FAIL]
- autoresearch: [PASS/FAIL]
- wallet_scout: [PASS/FAIL]

## Trading
- FILLED trades (30 min): [N]
- Laatste FILLED: [timestamp + market]
- Status: [PASS / NEEDS WORK / FAIL]

## Config
- [wijziging]: [bevestigd actief / NIET actief]

## Regressies
- Errors afgelopen uur: [N] (vorige uur: [N])
- Bankroll sync: [OK / STALE / FAIL]

## Verdict: [PASS / NEEDS WORK / FAIL]
[Als NEEDS WORK of FAIL: specifiek wat er mis is en wat te doen]
```

---

## Regels

| DO | DON'T |
|----|-------|
| Default to NEEDS WORK | Aannemen dat het werkt |
| grep FILLED, niet grep EXECUTE | EXECUTE ≠ FILLED |
| Vergelijk error rates voor/na | Alleen kijken of service draait |
| Check dat verwijderde wallets niet meer verschijnen | Aannemen dat config reload werkt |
| Restart autoresearch + wallet_scout na Python deploy | Alleen bottie restarten |

---

## Bekende VPS Details

- **SSH:** `root@45.76.38.183`
- **Path:** `/opt/bottie/`
- **Binary:** `/opt/bottie/bottie-bin`
- **Deploy:** `cargo build --release && cp target/release/bottie bottie-bin`
- **Na deploy:** `systemctl restart bottie` (+ autoresearch + wallet_scout bij Python changes)

---

## Known Failures

### F1: is-active zonder FILLED check (meerdere sessies)
- **Niveau:** Instructie (stap volgorde)
- **Wat:** `systemctl is-active` toonde "active" maar bot plaatste geen trades
- **Impact:** Dacht dat deploy werkte, maar bot was stuck of had config fout
- **Root cause:** Stap 1 (services) kwam vóór stap 2 (FILLED). Neiging om na "active" te stoppen.
- **Fix:** Skill expliciet: "is-active = noodzakelijk maar NIET voldoende"
- **Mogelijke verbetering:** FILLED check als stap 1, service status als stap 2

### F2: SSH hostname niet gevonden (2026-03-15)
- **Niveau:** Tool call
- **Wat:** `ssh bottie` faalde — geen SSH alias geconfigureerd
- **Impact:** Productie-check in /morning mislukte, moest IP opzoeken in deploy.sh
- **Fix:** IP hardcoded in skill: `root@45.76.38.183`
- **Gerelateerd:** /morning had ook geen SSH details → faalde op dezelfde stap

### F3: Nieuwe filters niet verifieerbaar (2026-03-15)
- **Niveau:** Instructie (stap 3 — ontbrekend)
- **Wat:** Na deploy van max_delay=60s en min_price=0.20 kon /verify niet bewijzen dat de filters werkten — geen "REJECT: delay too high" of "REJECT: price too low" in logs
- **Impact:** Onbekend of filters actief zijn of dat de code ze anders logt
- **Status:** Open — moeten de exacte log messages vinden die bewijzen dat trades worden gefilterd
- **Mogelijke verbetering:** Stap 3 moet per config parameter de verwachte log output definiëren

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-03-15 | Add condition | SSH IP hardcoded in "Bekende VPS Details" | F2: ssh alias bestond niet |
| 2026-03-15 | Add condition | "is-active = noodzakelijk maar NIET voldoende" | F1: false confidence na service check |
| 2026-03-15 | Tighten trigger | grep FILLED, niet EXECUTE | EXECUTE ≠ FILLED (memory) |
