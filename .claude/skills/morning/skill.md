# /morning — Ochtend Kickoff

Laadt overnight state, checkt optioneel productie-systemen, en presenteert een actionable ochtend briefing.

**Draai dit EERST bij het starten van een nieuwe sessie in de ochtend.**

---

## Gebruik

```
/morning
```

---

## Uitvoering

### Stap 1: Laad Context (PARALLEL)

**A. Laatste session save:**
```
Read: data/sessions/YYYY-MM-DD-session.md (vandaag of gisteren)
```
Focus op de LAATSTE "Session Save" sectie.

**B. Gisteren's EOD samenvatting:**
```
Read: data/sessions/YYYY-MM-DD-eod.md (datum van gisteren)
```
Lees alleen: "Completed", "In progress", "Blockers", "Context for tomorrow".

**C. Git state:**
```bash
git -C <PROJECT_ROOT> log --oneline -5
git -C <PROJECT_ROOT> status --short
git -C <PROJECT_ROOT> branch --show-current
```

**D. Issue tracking** (optioneel):
- Haal actieve tickets op

**E. Production checks** (Bottie VPS):
SSH: `export SSHPASS='}4nUzoFa#{67argr' && sshpass -e ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@78.141.222.227`

1. **Portfolio (PM API — source of truth)**:
```bash
python3 -c "
import json, urllib.request
API='https://data-api.polymarket.com'
F='0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15'
def g(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'B/1','Accept':'application/json'}),timeout=15).read())
val=g(f'{API}/value?user={F}')
pos=g(f'{API}/positions?user={F}&limit=500&sizeThreshold=0.01')
pv=float(val[0]['value']) if val else 0
op=[p for p in pos if float(p.get('size',0))>0.01]
print(f'Positions value: \${pv:.2f}')
print(f'Open positions: {len(op)}')
"
```

2. **Bot status + cash** (uit logs, ALLEEN voor service status en cash):
```bash
journalctl -u bottie --since "30 min ago" --no-pager -n 30
```
Gebruik `bankroll=` uit STATUS regel voor cash. Totaal portfolio = positions value + bankroll.

**⚠️ NOOIT de WR/PnL/trade count uit de STATUS log gebruiken — deze zijn onbetrouwbaar door phantom fills.**
**Portfolio waarde = PM /value (posities) + bankroll (cash). ALTIJD optellen!**

3. **Overnight W/L** (trades.jsonl — source of truth voor resolved trades):
```bash
jq -r 'select(.resolved_at != null and .resolved_at > "YYYY-MM-DDT22:00") | [.resolved_at[11:16], .result, (.pnl // 0 | tostring), .market_title[0:45], .outcome, (.size_usdc | tostring)] | @tsv' /opt/bottie/data/trades.jsonl | sort | column -t -s $'\t'
```
Vervang `YYYY-MM-DD` door de datum van gisteren. Dit toont alle resolved trades (win/loss/take_profit/auto_sell) sinds 22:00.
Tel W/L op uit de output. **Dit is de enige betrouwbare bron voor W/L en PnL.**

### Stap 2: Presenteer Briefing

Houd het onder 15 regels:

```
Ochtend Briefing — YYYY-MM-DD HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gisteren:
  - [1-2 regels: wat is afgerond]
  - [Eventuele kritieke fixes]

Production: [alleen als relevant]
  [status per service: draaiend/gestopt]

Actief Werk:
  - [Ticket/taak 1]
  - [Ticket/taak 2]

Checks:
  - [Belangrijkste ding om te verifiëren op basis van gisteren]

Volgende Actie: [directe volgende stap]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Stap 3: Voer Eerste Actie Uit

Prioriteit:
1. Als productie down is → onderzoek en rapporteer
2. Als session zegt "verifieer X" → voer verificatie uit
3. Als er een lopend experiment/test is → check resultaten
4. Als er geen urgente actie is → presenteer status en VRAAG wat te doen

---

## Regels

| DO | DON'T |
|----|-------|
| Lees alleen de LAATSTE session save | Alle saves doorlezen |
| Focus op "Context for tomorrow" uit EOD | Hele EOD narratief lezen |
| Check productie EERST | Aannemen dat alles draait |
| PM API + cash = portfolio | Bot STATUS log WR/PnL gebruiken |
| Positions value + bankroll optellen | PM /value als totaal behandelen |
| Presenteer 10-15 regel briefing | Multi-paragraaf status schrijven |
| Handel op duidelijke next steps | Vragen "zal ik doorgaan?" |

---

## Fallback

Als geen session file of EOD gevonden:
1. Check MEMORY.md "Current State" sectie
2. Check git log (laatste 24u)
3. Presenteer wat beschikbaar is
4. Vraag: "Geen session data. Wat is de prioriteit vandaag?"
