# /eod — Einde van de Dag Samenvatting

Maakt een compacte dagafsluiting die geoptimaliseerd is voor de `/morning` briefing van morgen.

---

## Gebruik

```
/eod
```

---

## Uitvoering

### Stap 1: Verzamel Data (PARALLEL)

```
Read: data/sessions/YYYY-MM-DD-session.md (alle saves van vandaag)
```

```bash
# Commits van vandaag
git -C <PROJECT_ROOT> log --oneline --since="midnight"

# Huidige branch en status
git -C <PROJECT_ROOT> branch --show-current
git -C <PROJECT_ROOT> status --short
```

**Production state** (VPS: `ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@78.141.222.227`):
```bash
# Bankroll + status beide instanties
for svc in bottie bottie-test; do
  echo "=== $svc ==="
  journalctl -u $svc --no-pager -n 100 | grep STATUS | tail -1
done
```

```bash
# Portfolio waarde via PM API
python3 -c "
import json, urllib.request
API='https://data-api.polymarket.com'
INSTANCES = {
    'Cannae': '0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42',
    'GIYN':   '0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a',
}
def g(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'B/1','Accept':'application/json'}),timeout=15).read())
for name, funder in INSTANCES.items():
    val=g(f'{API}/value?user={funder}')
    pv=float(val[0]['value']) if val else 0
    print(f'{name}: positions=\${pv:.2f}')
"
```

```bash
# Resolved trades vandaag (per sport + per wallet)
VANDAAG=$(date -u +%Y-%m-%d)
for inst in /opt/bottie /opt/bottie-test; do
  echo "=== $(basename $inst) ==="
  jq -r "select(.resolved_at != null and .resolved_at > \"${VANDAAG}T00:00\") | [
    (.sport // \"unknown\"), .result, (.pnl // 0),
    ((.consensus_wallets // [\"unknown\"])[0])
  ] | @tsv" $inst/data/trades.jsonl 2>/dev/null | awk -F'\t' '
    { sport=$1; result=$2; pnl=$3+0; wallet=$4
      total++; totpnl+=pnl
      if(result=="win") wins++; else if(result=="loss") losses++
      spnl[sport]+=pnl; scount[sport]++
      wpnl[wallet]+=pnl; wcount[wallet]++
    }
    END {
      printf "Totaal: %d trades | %dW/%dL | PnL: $%.2f\n", total, wins+0, losses+0, totpnl
      print "Per sport:"; for(s in scount) printf "  %-12s %d trades  $%.2f\n", s, scount[s], spnl[s]
      print "Per wallet:"; for(w in wcount) printf "  %-16s %d trades  $%.2f\n", w, wcount[w], wpnl[w]
    }'
done
```

**Verplicht:**
- Actieve/gesloten tickets ophalen uit Linear (team: RustedPoly) — NIET filteren op project
- Tickets bijwerken met resultaten van vandaag
- Status van draaiende services checken

### Stap 2: Schrijf EOD + Session

Schrijf naar **twee bestanden**:
1. `data/sessions/YYYY-MM-DD-eod.md` — compact (max 200 woorden, voor /morning)
2. `data/sessions/YYYY-MM-DD-session.md` — identieke inhoud (fallback voor /morning)

**Template (max 200 woorden):**

```markdown
# EOD Summary — YYYY-MM-DD

## Production State
- **Cannae:** [active/inactive] | $X cash + $Y pos = $Z totaal | sizing: 5% base proportional
- **GIYN:** [active/inactive] | $X cash (paper)

## Trade Resultaten Vandaag
- **Cannae:** NW/NL | PnL: $X
  - Per sport: [alle sports met trades]
  - Per wallet: [alle wallets met trades]
  - ⚠️ [eventuele unknown sport tags of filter issues]
- **GIYN:** NW/NL | PnL: $X (paper)

## Afgerond vandaag
- [Taak/ticket 1: korte beschrijving]
- [Taak/ticket 2: korte beschrijving]

## In progress
- [Taak die nog loopt + huidige stand]

## Blockers / Issues
- [Problemen die aandacht nodig hebben]

## Context voor morgen
1. **Eerste prioriteit:** [wat moet er als eerste gebeuren]
2. **Branch state:** [branch, uncommitted changes]
3. **Volgende stappen:** [concrete acties]

---
**Commits vandaag:** X | **Tickets gesloten:** Y
```

### Stap 3: Linear Bijwerken

**Verplicht bij elke /eod:**
- Update tickets in RustedPoly team met resultaten (NIET filteren op project)
- Sluit afgeronde tickets (state: Done)
- Voeg comments toe aan in-progress tickets met huidige stand
- Maak nieuwe tickets voor ontdekte issues

### Stap 4: Optioneel Detail

Als er veel is gebeurd, maak een apart `YYYY-MM-DD-eod-detailed.md` voor uitgebreide referentie. Dit bestand wordt NIET geladen in `/morning` context — alleen het compacte bestand.

---

## Regels

| DO | DON'T |
|----|-------|
| Max 200 woorden in het compacte bestand | Essays schrijven |
| "Context voor morgen" is het BELANGRIJKSTE onderdeel | Focussen op wat fout ging |
| Concrete volgende stappen | Vage plannen |
| Meld blockers expliciet | Problemen verstoppen |
| Meld branch state + uncommitted changes | Aannemen dat alles gecommit is |
| Production state + bankroll in EOD | Alleen dev werk rapporteren |
| Per-wallet en per-sport trade resultaten | Alleen totaal W/L |
| Check BEIDE instanties (bottie + bottie-test) | Alleen Cannae checken |
| Meld unknown sport tags of filter issues | Data quality problemen negeren |

---

## Relatie met Andere Skills

```
/save (tijdens de dag) → /eod (einde dag) → /morning (volgende ochtend)
     ↓                                            ↓
  /compact → /continue                    Eerste actie uitvoeren
```
