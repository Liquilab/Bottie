# /morning — Ochtend Kickoff

Laadt overnight state, checkt productie-systemen (beide instanties), en presenteert een actionable ochtend briefing met uitgebreide trade-analyse.

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
| **Cannae** | `bottie` | `/opt/bottie/` | `0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42` | Hoofd-bot, Cannae copy trading (live) |
| **GIYN** | `bottie-test` | `/opt/bottie-test/` | `0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a` | Paper trade bot (dry_run=true, odds_arb close_games) |

---

## Uitvoering

### Stap 1: Laad Context (PARALLEL)

**A. Gisteren's EOD (primary session context):**
```
Read: data/sessions/YYYY-MM-DD-eod.md (datum van gisteren)
```
Lees: "Completed", "In progress", "Blockers", "Context voor morgen".
Als niet gevonden, check `YYYY-MM-DD-session.md` of `YYYY-MM-DD-daily-snapshot.md`.

**B. Git state:**
```bash
git log --oneline -5
git status --short
git branch --show-current
```

**C. Issue tracking:**
- Haal actieve In Progress tickets op uit Linear (team: RustedPoly, project: Bottie)

**D. Production checks** (VPS: `ssh -T -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@78.141.222.227`):

**1. Portfolio BEIDE instanties (PM API):**
```bash
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
    pos=g(f'{API}/positions?user={funder}&limit=500&sizeThreshold=0.01')
    pv=float(val[0]['value']) if val else 0
    op=[p for p in pos if float(p.get('size',0))>0.01]
    print(f'{name}: positions=\${pv:.2f}, open={len(op)}')
"
```

**2. Bot status + cash + binary verify BEIDE services:**
```bash
for svc in bottie bottie-test; do
  echo "=== $svc ==="
  systemctl is-active $svc
  journalctl -u $svc --no-pager -n 100 | grep STATUS | tail -1
  # CRITICAL: verify running binary matches compiled binary
  PID=$(systemctl show $svc --property=ExecMainPID --value)
  if [ "$PID" -gt 0 ] 2>/dev/null; then
    RUNNING_MD5=$(md5sum /proc/$PID/exe 2>/dev/null | awk '{print $1}')
    if [ "$svc" = "bottie" ]; then
      DISK_MD5=$(md5sum /opt/bottie/bottie-bin 2>/dev/null | awk '{print $1}')
      BUILD_MD5=$(md5sum /opt/bottie/target/release/bottie 2>/dev/null | awk '{print $1}')
    else
      DISK_MD5=$(md5sum /opt/bottie-test/bottie-bin 2>/dev/null | awk '{print $1}')
      BUILD_MD5=$(md5sum /opt/bottie-test/target/release/bottie 2>/dev/null | awk '{print $1}')
    fi
    echo "  binary: running=$RUNNING_MD5 disk=$DISK_MD5 build=$BUILD_MD5"
    if [ "$RUNNING_MD5" != "$DISK_MD5" ]; then echo "  ⚠️ RUNNING BINARY != DISK BINARY"; fi
    if [ "$DISK_MD5" != "$BUILD_MD5" ]; then echo "  ⚠️ DISK BINARY != LATEST BUILD"; fi
  fi
done
```
Gebruik `bankroll=` uit STATUS voor cash. **Portfolio = PM /value (posities) + bankroll (cash).**

**⚠️ BINARY CHECK IS VERPLICHT.** Op 2026-04-04 draaide de service urenlang een oud binary na een deploy omdat `cp target/release/bottie bottie-bin` ontbrak. Als hashes niet matchen: rapporteer als **CRITICAL** in de briefing.

**3. Uitgebreide trade-analyse BEIDE instanties (vorige dag + nacht):**

Vervang `GISTEREN` door datum van gisteren (YYYY-MM-DD).

```bash
for inst in /opt/bottie /opt/bottie-test; do
  echo ""
  echo "====== $(basename $inst) — trade analyse ======"
  
  # Alle resolved trades van gisteren
  TRADES=$(jq -r 'select(.resolved_at != null and .resolved_at > "GISTEREN T00:00") | [
    .resolved_at[0:10],
    (.sport // "unknown"),
    .result,
    (.pnl // 0),
    .market_title[0:50],
    .outcome,
    (.size_usdc // 0)
  ] | @tsv' $inst/data/trades.jsonl 2>/dev/null)
  
  if [ -z "$TRADES" ]; then
    echo "  Geen resolved trades gevonden"
    continue
  fi

  # Totaal W/L/PnL
  echo "--- Totaal ---"
  echo "$TRADES" | awk -F'\t' '
    {
      result=$3; pnl=$4+0
      total++; totpnl+=pnl
      if(result=="win") wins++
      else if(result=="loss") losses++
      else other++
    }
    END {
      printf "Trades: %d | W: %d | L: %d | Overig: %d | PnL: $%.2f\n", total, wins+0, losses+0, other+0, totpnl
    }'
  
  # Per sport
  echo "--- Per sport ---"
  echo "$TRADES" | awk -F'\t' '
    {
      sport=$2; result=$3; pnl=$4+0
      count[sport]++; totpnl[sport]+=pnl
      if(result=="win") wins[sport]++
      else if(result=="loss") losses[sport]++
    }
    END {
      for(s in count)
        printf "  %-12s %dW/%dL  PnL: $%.2f\n", s, wins[s]+0, losses[s]+0, totpnl[s]
    }' | sort
  
  # Top 3 wins en top 3 losses
  echo "--- Grootste winst ---"
  echo "$TRADES" | sort -t$'\t' -k4 -rn | head -3 | awk -F'\t' '{printf "  +$%.2f  %s (%s)\n", $4, $5, $6}'
  
  echo "--- Grootste verlies ---"
  echo "$TRADES" | sort -t$'\t' -k4 -n | head -3 | awk -F'\t' '{printf "  $%.2f  %s (%s)\n", $4, $5, $6}'
done
```

### Stap 2: Presenteer Briefing

```
Ochtend Briefing — YYYY-MM-DD HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gisteren:
  - [1-2 regels: wat is afgerond]

Production:
  Cannae:  [status] | $X cash + $Y pos = $Z totaal
  GIYN:    [status] | $X cash (paper bot, dry_run)

Trade Analyse — Cannae (gisteren):
  Totaal:  NW / NL | PnL: $X
  Voetbal: NW / NL | PnL: $X
  NBA:     NW / NL | PnL: $X
  Beste:   +$X  [markt]
  Slechtste: -$X  [markt]

Trade Analyse — GIYN Paper (gisteren):
  Totaal:  NW / NL | PnL: $X (paper)

Actief Werk:
  - [Ticket/taak]

Checks:
  - [Ding om te verifiëren]

Volgende Actie: [directe volgende stap]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Stap 3: Voer Eerste Actie Uit

Prioriteit:
1. Als productie down is → onderzoek en rapporteer
2. Als session/EOD zegt "verifieer X" → voer verificatie uit
3. Als er een lopend experiment/test is → check resultaten
4. Als er geen urgente actie is → presenteer status en VRAAG wat te doen

---

## Regels

| DO | DON'T |
|----|-------|
| Check BEIDE instanties (bottie + bottie-test) | Alleen Cannae checken |
| Lees de EOD van gisteren als primary context | Alle session files doorlezen |
| Uitgebreide trade-analyse elke ochtend | Alleen W/L tellen |
| PM API adres = FUNDER_ADDRESS uit .env | Verkeerde wallet adres gebruiken |
| Portfolio = PM /value + bankroll | Één van beide weglaten |
| GIYN label als "(paper)" in briefing | GIYN als live bot behandelen |
| Handel op duidelijke next steps | Vragen "zal ik doorgaan?" |

---

## Fallback

Als geen EOD of session file gevonden:
1. Check MEMORY.md "Current State" sectie
2. Check git log (laatste 24u)
3. Presenteer wat beschikbaar is
4. Vraag: "Geen session data. Wat is de prioriteit vandaag?"

---

## Known Failures

### F1: Binary mismatch niet gedetecteerd (2026-04-04)
- **Niveau:** Instructie (ontbrekende check)
- **Wat:** /morning checkte alleen `systemctl is-active` maar niet of het draaiende binary overeenkomt met het laatste build. Na een deploy draaide de service urenlang een oud binary met foute sizing (7%+3% i.p.v. 5%+5%).
- **Impact:** Foute sizing op live trades voor meerdere wedstrijden.
- **Fix:** Binary md5 verificatie toegevoegd aan stap 2 (running vs disk vs build).

### F2: Alleen voetbal/NBA gecheckt, eSports/tennis/NHL gemist (2026-04-14)
- **Niveau:** Instructie (stap 1D miste non-football sports)
- **Wat:** /morning trade analyse keek alleen naar resolved trades in voetbal/NBA leagues. eSports (lol/cs2/dota2/val), tennis (atp/wta), en NHL stonden niet in de schedule of analyse.
- **Impact:** Gemiste signalen op 189 eSports + 121 tennis + 32 NHL games per dag.
- **Fix:** Schedule nu uitgebreid met esports/tennis/nhl tags. Briefing moet ALLE actieve sports rapporteren.

### F3: Wallet evaluatie op basis van onbetrouwbare API data (2026-04-14)
- **Niveau:** Instructie (geen verificatie-stap voor wallet claims)
- **Wat:** /morning presenteerde wallet WR/ROI cijfers die uit de scout kwamen zonder verificatie. NBAShark "64% WR" was 0W/3L, NoSpreader "70% WR" was 3.9%.
- **Impact:** Verliesgevende wallets live gezet, bankroll risico.
- **Fix:** Elke wallet claim MOET geverifieerd worden via PM profielpagina (Closed tab) of CLOB resolution check. Nooit curPrice-based WR rapporteren.

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-04-04 | Add condition | Binary md5 check in stap 2 (running vs disk vs build) | F1: oud binary draaide na deploy |
| 2026-04-14 | Add condition | Alle sports in trade analyse (niet alleen voetbal/NBA) | F2: eSports/tennis/NHL gemist |
| 2026-04-14 | Add condition | Wallet WR claims vereisen CLOB verificatie of PM profile check | F3: Survivorship bias in scout data |
