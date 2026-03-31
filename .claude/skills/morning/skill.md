# /morning — Ochtend Kickoff

Laadt overnight state, checkt productie-systemen (beide instanties), en presenteert een actionable ochtend briefing.

**Draai dit EERST bij het starten van een nieuwe sessie in de ochtend.**

---

## Gebruik

```
/morning
```

---

## Instanties

| Instantie | Service | Pad | Funder | Rol |
|-----------|---------|-----|--------|-----|
| **Cannae** | `bottie` | `/opt/bottie/` | `0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15` | Hoofd-bot, Cannae copy |
| **GIYN** | `bottie-test` | `/opt/bottie-test/` | `0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a` | Expert team (GIYN, Countryside, weflyhigh, bcda) |

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
SSH: `ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@78.141.222.227`

1. **Portfolio BEIDE instanties (PM API — source of truth)**:
```bash
python3 -c "
import json, urllib.request
API='https://data-api.polymarket.com'
INSTANCES = {
    'Cannae': '0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15',
    'GIYN':   '0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a',
}
def g(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'B/1','Accept':'application/json'}),timeout=15).read())
for name, funder in INSTANCES.items():
    val=g(f'{API}/value?user={funder}')
    pos=g(f'{API}/positions?user={funder}&limit=500&sizeThreshold=0.01')
    pv=float(val[0]['value']) if val else 0
    op=[p for p in pos if float(p.get('size',0))>0.01]
    print(f'{name}: positions=\${pv:.2f}, open={len(op)}')
"
```

2. **Bot status + cash BEIDE services** (uit logs, ALLEEN voor service status en cash):
```bash
for svc in bottie bottie-test; do
  echo "=== $svc ==="
  systemctl is-active $svc
  journalctl -u $svc --no-pager -n 100 | grep STATUS | tail -1
done
```
Gebruik `bankroll=` uit STATUS regel voor cash. Totaal portfolio per instantie = positions value + bankroll.

**⚠️ NOOIT de WR/PnL/trade count uit de STATUS log gebruiken — deze zijn onbetrouwbaar door phantom fills.**
**Portfolio waarde = PM /value (posities) + bankroll (cash). ALTIJD optellen!**

3. **Overnight W/L BEIDE instanties** (trades.jsonl — source of truth voor resolved trades):
```bash
for inst in /opt/bottie /opt/bottie-test; do
  echo "=== $(basename $inst) ==="
  jq -r 'select(.resolved_at != null and .resolved_at > "YYYY-MM-DDT22:00") | [.resolved_at[11:16], .result, (.pnl // 0 | tostring), .market_title[0:45], .outcome, (.size_usdc | tostring)] | @tsv' $inst/data/trades.jsonl 2>/dev/null | sort | column -t -s $'\t'
done
```
Vervang `YYYY-MM-DD` door de datum van gisteren. Dit toont alle resolved trades (win/loss/take_profit/auto_sell) sinds 22:00.
Tel W/L op uit de output. **Dit is de enige betrouwbare bron voor W/L en PnL.**

### Stap 2: Presenteer Briefing

Houd het onder 20 regels:

```
Ochtend Briefing — YYYY-MM-DD HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gisteren:
  - [1-2 regels: wat is afgerond]

Production:
  Cannae:  [status] | $X cash + $Y pos = $Z totaal | overnight W/L
  GIYN:    [status] | $X cash + $Y pos = $Z totaal | overnight W/L

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
| Check BEIDE instanties (bottie + bottie-test) | Alleen Cannae checken |
| Lees alleen de LAATSTE session save | Alle saves doorlezen |
| Focus op "Context for tomorrow" uit EOD | Hele EOD narratief lezen |
| Check productie EERST | Aannemen dat alles draait |
| PM API + cash = portfolio | Bot STATUS log WR/PnL gebruiken |
| Positions value + bankroll optellen | PM /value als totaal behandelen |
| Presenteer 15-20 regel briefing | Multi-paragraaf status schrijven |
| Handel op duidelijke next steps | Vragen "zal ik doorgaan?" |

---

## Fallback

Als geen session file of EOD gevonden:
1. Check MEMORY.md "Current State" sectie
2. Check git log (laatste 24u)
3. Presenteer wat beschikbaar is
4. Vraag: "Geen session data. Wat is de prioriteit vandaag?"
