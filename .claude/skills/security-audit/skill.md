---
name: security-audit
description: "Security audit van Bottie: private keys, API credentials, attack surface, fund protection"
---

# /security-audit — Security Engineer

Audit de security van Bottie. Bij groeiende bankroll ($72 → $10K) wordt een hack steeds duurder. Private key gelekt = alles kwijt.

**Gebaseerd op:** agency-agents/engineering-security-engineer (aangepast voor trading bot)

---

## Gebruik

```
/security-audit             — Volledige security audit
/security-audit keys        — Credentials & secrets check
/security-audit attack      — Attack surface mapping
/security-audit recommend   — Aanbevelingen
```

---

## Stap 1: Credentials & Secrets

### A. Lokale repo check

```bash
# Secrets in git history?
cd "/Users/koen/Projects/ Bottie"
git log --all -p -- .env 2>/dev/null | head -5
git log --all -p -- '*.key' '*.pem' 2>/dev/null | head -5

# .env in .gitignore?
grep -q '.env' .gitignore && echo "OK: .env in .gitignore" || echo "FAIL: .env NOT in .gitignore"

# Hardcoded secrets in source? (zoek op keywords, NIET op hex patterns — die matchen op adressen)
grep -rn "private_key\|PRIVATE_KEY\|secret\|SECRET" src/ --include="*.rs" | grep -v "test\|example\|mock"
# WAARSCHUWING: print NOOIT gevonden waarden naar de terminal. Alleen bestandsnaam + regelnummer.
```

### B. VPS secrets check

```bash
# .env permissions
ssh root@78.141.222.227 'ls -la /opt/bottie/.env'
# Moet 600 zijn (alleen root leesbaar)

# Wie kan bij de VPS?
ssh root@78.141.222.227 'cat /root/.ssh/authorized_keys | wc -l'

# Draait als root? (risico)
ssh root@78.141.222.227 'ps aux | grep bottie-bin | grep -v grep | awk "{print \$1}"'
```

---

## Stap 2: Attack Surface

### Threat Model (STRIDE)

| Threat | Scenario | Impact | Mitigatie |
|--------|----------|--------|-----------|
| **Spoofing** | Iemand doet zich voor als Polymarket API | Valse prijzen, slechte trades | HTTPS + certificate pinning |
| **Tampering** | config.yaml gewijzigd op VPS | Wallet list gemanipuleerd | File permissions, integrity check |
| **Repudiation** | Geen audit trail van trades | Niet te bewijzen wat er gebeurde | trades.jsonl (append-only) |
| **Information Disclosure** | Private key gelekt | Alle funds gestolen | .env permissions, geen git history |
| **Denial of Service** | Polymarket API down | Bot stopt met handelen | Graceful degradation |
| **Elevation of Privilege** | VPS compromised | Volledige controle | SSH key-only, firewall, updates |

### Kritieke Assets

| Asset | Locatie | Impact als gelekt |
|-------|---------|-------------------|
| Private key | .env op VPS | **FATAAL** — alle USDC gestolen |
| API key | .env op VPS | Ongeautoriseerde trades |
| Funder address | .env op VPS | Laag (publiek op-chain) |
| VPS SSH key | ~/.ssh/ lokaal | VPS compromised → private key gelekt |

---

## Stap 3: VPS Hardening Check

```bash
# Firewall actief?
ssh root@78.141.222.227 'ufw status 2>/dev/null || iptables -L -n 2>/dev/null | head -10'

# Open ports?
ssh root@78.141.222.227 'ss -tlnp | grep -v "127.0.0.1"'

# OS updates?
ssh root@78.141.222.227 'apt list --upgradable 2>/dev/null | head -10'

# Fail2ban?
ssh root@78.141.222.227 'systemctl is-active fail2ban 2>/dev/null'

# Unattended upgrades?
ssh root@78.141.222.227 'systemctl is-active unattended-upgrades 2>/dev/null'
```

---

## Stap 4: Rapporteer

```markdown
# Security Audit — [datum]

## Risico Niveau: [LAAG / MIDDEN / HOOG / KRITIEK]

## Bevindingen

### KRITIEK (directe actie)
- [bevinding]: [risico] → [oplossing]

### HOOG (deze week)
- [bevinding]: [risico] → [oplossing]

### MIDDEN (dit kwartaal)
- [bevinding]: [risico] → [oplossing]

### LAAG (nice to have)
- [bevinding]: [risico] → [oplossing]

## Aanbevelingen (prioriteit)
1. [actie] — [impact]
2. ...
```

---

## Regels

| DO | DON'T |
|----|-------|
| Check .env permissions op VPS | Private keys in chat plakken |
| Zoek secrets in git history | Aannemen dat .gitignore genoeg is |
| Map het hele attack surface | Alleen de "voor de hand liggende" checks |
| Prioriteer op impact (funds at risk) | Alles even belangrijk maken |
| Aanbevel, laat user implementeren | Auto-fixen van security settings |
