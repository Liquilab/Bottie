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
