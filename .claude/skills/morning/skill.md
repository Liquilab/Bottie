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
| **Bottie** | `bottie` | `/opt/bottie/` | `0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42` | Hoofd-bot, multi-wallet sweet spot pipeline (live) |
| **GIYN** | `bottie-test` | `/opt/bottie-test/` | `0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a` | Paper bot (dry_run=true, blind_fade soccer) |

### Actieve Wallets (Bottie instantie, weight=1.0)

| Wallet | Address | Leagues | Market Types | Min Source USDC | Avg Source USDC | Sweet Spot WR |
|--------|---------|---------|-------------|----------------|-----------------|---------------|
| kch123 | 0x6a72f6 | NHL, CFB | win, spread | $20K | NHL $101K, CFB $385K | NHL 80.5%, CFB 88.2% |
| FazeS1mple | 0x13414a | LoL | win | $50K | LoL $82K | 72.5% |
| CBB_Edge | 0x163eff | CBB | win, spread | $25K | CBB $39K | 78.0% |
| texaskid_mlb | 0xc80756 | MLB | win | $25K | MLB $86K | — |
| kahe_cs2 | 0x88d17a | CS2 | win | $25K | CS2 $38K | 84.2% |
| TennisEdge | 0xe30e74 | ATP, WTA | win | $50K | ATP $132K | 84.6% |
| GIYN | 0x507e52 | SEA, FL1, LAL, EPL | win | $10K | — | — |

### Disabled Wallets (weight=0.0)

| Wallet | Reden |
|--------|-------|
| Cannae (0x7ea571) | Voetbal edge erodeert W15-W16 |
| Elkmonkey (0xead152) | 49.5% WR, -$320K LB, market maker |
| NBAShark (0xa6766c) | 0W/3L geverifieerd, niet 64% WR |
| BigEdge (0x2005d1) | Disabled |
| NoSpreader (0x91f059) | 3.9% WR geverifieerd |
| ewelmealt (0x079213) | Disabled |

### Sizing

- `copy_base_size_pct: 20.0` met `portfolio_reference_usdc: $800`
- Per-sport sizing: 7.5% voor nba/nhl/mlb/tennis/esports/voetbal ML, 0% voor FIFA
- Proportional via `avg_source_usdc_per_league` conviction ratio
- `min_bet_usdc: $2.50`, price range: 0.05–0.95 (per wallet config)

### GIYN Paper Bot

- `dry_run: true`, `blind_fade: enabled` (soccer, $5 flat, win_no 0.60–0.80)
- copy_trading: **disabled**, odds_arb: **disabled**
- Watchlist: GamblingIsAllYouNeed + Countryside (maar copy disabled)

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
- Haal actieve In Progress tickets op uit Linear (team: RustedPoly) — NIET filteren op project, pak alle tickets

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

**2b. Paper-instance strategie-mismatch check (CRITICAL, F4):**

Voor elke instance met `dry_run: true` (bv. bottie-test/GIYN): lijst alle open posities en verifieer dat elke positie match met verwachte strategie. GIYN verwachte scope = alleen `*-updown-5m-*` titels (BTC/ETH/SOL/XRP 5-min windows). Alles anders (soccer, NBA, NHL, CBB) → **CRITICAL alert**: een module negeert dry_run flag.

```python
# Per paper instance:
expected_patterns = ["Up or Down"]  # strategy fingerprint in title
for p in positions:
    if not any(pat in p.get("title","") for pat in expected_patterns):
        print(f"CRITICAL STRATEGY LEAK: {p['title']} value=${p['currentValue']}")
```

**⚠️ BINARY CHECK IS VERPLICHT.** Op 2026-04-04 draaide de service urenlang een oud binary na een deploy omdat `cp target/release/bottie bottie-bin` ontbrak. Als hashes niet matchen: rapporteer als **CRITICAL** in de briefing.

**3. Uitgebreide trade-analyse BEIDE instanties (vorige dag + nacht):**

Vervang `GISTEREN` door datum van gisteren (YYYY-MM-DD).

```bash
for inst in /opt/bottie /opt/bottie-test; do
  echo ""
  echo "====== $(basename $inst) — trade analyse ======"
  
  # Alle resolved trades van gisteren (inclusief wallet source)
  TRADES=$(jq -r 'select(.resolved_at != null and .resolved_at > "GISTEREN T00:00") | [
    .resolved_at[0:10],
    (.sport // "unknown"),
    .result,
    (.pnl // 0),
    .market_title[0:50],
    .outcome,
    (.size_usdc // 0),
    ((.consensus_wallets // ["unknown"])[0])
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
  
  # Per wallet source
  echo "--- Per wallet ---"
  echo "$TRADES" | awk -F'\t' '
    {
      wallet=$8; result=$3; pnl=$4+0
      count[wallet]++; totpnl[wallet]+=pnl
      if(result=="win") wins[wallet]++
      else if(result=="loss") losses[wallet]++
    }
    END {
      for(w in count)
        printf "  %-16s %dW/%dL  PnL: $%.2f\n", w, wins[w]+0, losses[w]+0, totpnl[w]
    }' | sort
  
  # Unknown sport warning
  UNKNOWN_COUNT=$(echo "$TRADES" | awk -F'\t' '$2=="unknown"' | wc -l | tr -d ' ')
  if [ "$UNKNOWN_COUNT" -gt 0 ]; then
    echo "--- ⚠️ SPORT TAG WARNING: $UNKNOWN_COUNT trades met sport=unknown ---"
  fi

  # Top 3 wins en top 3 losses
  echo "--- Grootste winst ---"
  echo "$TRADES" | sort -t$'\t' -k4 -rn | head -3 | awk -F'\t' '{printf "  +$%.2f  %s (%s) [%s]\n", $4, $5, $6, $8}'
  
  echo "--- Grootste verlies ---"
  echo "$TRADES" | sort -t$'\t' -k4 -n | head -3 | awk -F'\t' '{printf "  $%.2f  %s (%s) [%s]\n", $4, $5, $6, $8}'
done
```

**4. Sweet spot filter verificatie (Cannae instantie):**

Check of per-league min_source_usdc filters correct werken door te kijken naar T1 SKIP logs.

```bash
echo "=== Sweet spot filter check (laatste 24u) ==="
journalctl -u bottie --since "24 hours ago" --no-pager | grep "T1 SKIP" | awk '{
  for(i=1;i<=NF;i++) {
    if($i ~ /SKIP:/) wallet=$(i+1)
    if($i ~ /position/) { gsub(/\$/, "", $(i+1)); pos=$(i+1) }
    if($i ~ /min/) { gsub(/\$/, "", $(i+1)); min=$(i+1) }
  }
  skips[wallet]++
}
END {
  for(w in skips) printf "  %-16s %d skipped (below min_source)\n", w, skips[w]
}' | sort

echo "=== Trades accepted per wallet (laatste 24u) ==="
journalctl -u bottie --since "24 hours ago" --no-pager | grep "LADDER\|PROPORTIONAL" | awk -F'—' '{print $1}' | awk '{print $NF}' | sort | uniq -c | sort -rn | head -10
```

**5. Spread module status (als enabled):**

```bash
journalctl -u bottie --since "24 hours ago" --no-pager | grep -i "spread" | tail -5
```

### Stap 2: Presenteer Briefing

```
Ochtend Briefing — YYYY-MM-DD HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gisteren:
  - [1-2 regels: wat is afgerond]

Production:
  Bottie:  [status] | $X cash + $Y pos = $Z totaal | 7 wallets actief
  GIYN:    [status] | $X cash (paper, blind_fade soccer, dry_run)

Trade Analyse — Bottie (gisteren):
  Totaal:  NW / NL | PnL: $X
  [per sport — ALLE sports dynamisch, niet hardcoded]
  ucl:     NW / NL | PnL: $X
  nhl:     NW / NL | PnL: $X
  ...etc voor elke sport met trades
  ⚠️ unknown: N trades (als >0)

  Per wallet:
  kch123:      NW / NL | PnL: $X
  FazeS1mple:  NW / NL | PnL: $X
  texaskid_mlb:NW / NL | PnL: $X
  ...etc voor elke wallet met trades

  Beste:   +$X  [markt] [wallet]
  Slechtste: -$X  [markt] [wallet]

  Sweet Spot Filters: N skipped (below min_source)

Trade Analyse — GIYN Paper (gisteren):
  Totaal:  NW / NL | PnL: $X (paper, blind_fade)

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
| Check BEIDE instanties (bottie + bottie-test) | Alleen Bottie checken |
| Lees de EOD van gisteren als primary context | Alle session files doorlezen |
| Uitgebreide trade-analyse elke ochtend | Alleen W/L tellen |
| Per-wallet breakdown (welke wallet wint/verliest) | Alleen totaal rapporteren |
| Alle sports dynamisch tonen | Hardcoded "Voetbal/NBA" |
| Waarschuw bij sport="unknown" trades | Unknown trades negeren |
| Check sweet spot filters (T1 SKIP logs) | Aannemen dat filters werken |
| PM API adres = FUNDER_ADDRESS uit .env | Verkeerde wallet adres gebruiken |
| Portfolio = PM /value + bankroll | Één van beide weglaten |
| GIYN label als "(paper, blind_fade)" in briefing | GIYN als live bot behandelen |
| Handel op duidelijke next steps | Vragen "zal ik doorgaan?" |
| Toon sizing logica (7.5% sport, proportional conviction) | Sizing weglaten |
| Toon GIYN als blind_fade paper bot | GIYN als odds_arb of copy bot beschrijven |

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
- **Wat:** /morning trade analyse keek alleen naar resolved trades in voetbal/NBA leagues. eSports (lol/cs2/dota2/val), tennis (atp/wta), en NHL stonden niet in de schedule of analyse. Briefing template was hardcoded op "Voetbal/NBA".
- **Impact:** Gemiste signalen op 189 eSports + 121 tennis + 32 NHL games per dag.
- **Fix:** Schedule uitgebreid met esports/tennis/nhl tags (2026-04-14). Briefing template dynamisch gemaakt — alle sports (2026-04-16).

### F3: Wallet evaluatie op basis van onbetrouwbare API data (2026-04-14)
- **Niveau:** Instructie (geen verificatie-stap voor wallet claims)
- **Wat:** /morning presenteerde wallet WR/ROI cijfers die uit de scout kwamen zonder verificatie. NBAShark "64% WR" was 0W/3L, NoSpreader "70% WR" was 3.9%.
- **Impact:** Verliesgevende wallets live gezet, bankroll risico.
- **Fix:** Elke wallet claim MOET geverifieerd worden via PM profielpagina (Closed tab) of CLOB resolution check. Nooit curPrice-based WR rapporteren.

### F4: Paper-instance strategie-lek niet gedetecteerd (2026-04-16)
- **Niveau:** Instructie (stap 1D checkte portfolio-value maar niet positie-strategie-match)
- **Wat:** bottie-test (GIYN, `dry_run: true`) had 3 echte soccer posities ($14.51) van de `blind_fade` module die top-level dry_run flag negeerde. /morning rapporteerde alleen "GIYN: active, $172 cash (paper)" zonder open posities te inspecteren.
- **Impact:** Live trades op paper-instance urenlang niet opgemerkt tot user het zelf zag.
- **Fix:** Stap 2b toegevoegd: voor elke paper-instance, lijst open posities en flag strategie-mismatch als CRITICAL.

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-04-04 | Add condition | Binary md5 check in stap 2 (running vs disk vs build) | F1: oud binary draaide na deploy |
| 2026-04-14 | Add condition | Alle sports in trade analyse (niet alleen voetbal/NBA) | F2: eSports/tennis/NHL gemist |
| 2026-04-14 | Add condition | Wallet WR claims vereisen CLOB verificatie of PM profile check | F3: Survivorship bias in scout data |
| 2026-04-16 | Rewrite | Instantie tabel: "Cannae copy trading" → multi-wallet sweet spot pipeline | 6 wallets actief, niet meer Cannae-only |
| 2026-04-16 | Rewrite | Briefing template dynamisch: alle sports + per-wallet breakdown | Was hardcoded Voetbal/NBA, miste 6+ sports |
| 2026-04-16 | Add | Per-wallet W/L/PnL in trade analyse jq query | Cruciaal met 6 wallets om underperformers te spotten |
| 2026-04-16 | Add | Unknown sport tag waarschuwing | 6 trades gisteren met sport=unknown |
| 2026-04-16 | Add | Sweet spot filter verificatie (T1 SKIP logs) | Valideer dat min_source_usdc per league werkt |
| 2026-04-16 | Add | Spread module status check | Nieuw spread.rs module toegevoegd |
| 2026-04-16 | Add | Wallet lijst + sizing logica in skill | Documenteert actieve wallets + proportional 5% base |
| 2026-04-16 | Fix | Linear query: team-breed ipv project-filter | Miste Maker Bot tickets |
| 2026-04-17 | Fix | Wallet tabel: sync met live config.yaml | Cannae/Elkmonkey disabled, texaskid_mlb/GIYN wallet ontbraken |
| 2026-04-17 | Fix | Sizing: "5% base" → werkelijke config (7.5% sport, copy_base 20%, ref $800) | Skill documenteerde verkeerde sizing |
| 2026-04-17 | Fix | GIYN: "odds_arb close_games" → "blind_fade soccer, dry_run" | GIYN config was compleet anders |
| 2026-04-17 | Add | Disabled wallets tabel met reden per wallet | Voorkomt dat disabled wallets per ongeluk weer verschijnen |
| 2026-04-16 | Add condition | Stap 2b: paper-instance strategie-mismatch check (lijst open posities, CRITICAL als buiten expected pattern) | F4: blind_fade lekte live trades op GIYN paper, /morning zag alleen cash |
