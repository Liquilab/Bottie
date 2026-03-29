---
name: check
description: "Verificatie of alles bijdraagt aan het doel: bankroll → $10.000"
---

# /check — Draagt Dit Bij Aan Het Doel?

**Eén vraag. Alles wordt langs deze lat gelegd.**

De bot is een copy trader. Het enige dat ertoe doet:
1. Volgen we de juiste wallets?
2. Worden trades daadwerkelijk geplaatst?
3. Groeit de bankroll?

---

## Rol: Senior Practical Engineer

- Simpelste bron eerst (UI screenshot > API > berekening)
- VRAAG de user voor wat je niet kunt weten (deposits, withdrawals, portfolio waarde)
- Fix niets dat niet bewezen kapot is
- Verander niets aan een werkend systeem zonder concrete reden
- Na elke deploy: verifieer dat de bot DAADWERKELIJK tradeert (`grep FILLED`)

---

## Usage

```
/check              - Full check (auto-detect)
/check all          - Alles
/check wallets      - Watchlist kwaliteit
/check trading      - Tradeert de bot? Fills, skips, blokkades
/check system       - Services, connectivity, bankroll sync
/check performance  - Hoe staat de bot ervoor (VRAAGT USER)
```

---

## STAP 0: WAT IS DE HUIDIGE STAND?

**Vraag de user EERST:**
```
Voordat ik check, heb ik de echte cijfers nodig:
1. Screenshot van je Polymarket portfolio? (of: portfolio waarde + available to trade)
2. Deposits/withdrawals gedaan sinds de start?
3. Wat is het totaal geïnvesteerd?
```

Dan service + bankroll check:
```bash
ssh root@78.141.222.227 'systemctl is-active bottie autoresearch wallet_scout 2>/dev/null'

# On-chain USDC (available to trade)
ssh root@78.141.222.227 'journalctl -u bottie --no-pager | grep "SYNC.*balance" | tail -1'

# Bot's interne bankroll
ssh root@78.141.222.227 'journalctl -u bottie --no-pager | grep "STATUS:" | tail -1'
```

**Met de antwoorden van de user bereken je:**
- Totaal geïnvesteerd = start + deposits - withdrawals
- Huidige rendement = portfolio waarde / totaal geïnvesteerd
- **Als bot bankroll ≠ on-chain balance: bankroll sync is stuk**

---

## STAP 1: TRADEERT DE BOT?

**Dit is de allerbelangrijkste check.** Als de bot niet tradeert, doet niets anders ertoe.

```bash
# Fills in het afgelopen uur
ssh root@78.141.222.227 'journalctl -u bottie --since "1 hour ago" --no-pager 2>/dev/null | grep -c "FILLED:"'

# Waarom niet? Tel alle skip/reject redenen
ssh root@78.141.222.227 'journalctl -u bottie --since "1 hour ago" --no-pager 2>/dev/null | grep -E "SKIP:|RISK REJECTED:" | sed "s/.*SKIP: //;s/.*RISK REJECTED: /RISK: /" | cut -d" " -f1-4 | sort | uniq -c | sort -rn | head -10'
```

| Situatie | Actie |
|----------|-------|
| FILLED > 0 | Bot tradeert ✅ |
| 0 FILLED, veel "RISK REJECTED: bankroll" | Bankroll te laag of min_bankroll te hoog |
| 0 FILLED, veel "RISK REJECTED: sport/wallet limit" | Onnodige limieten blokkeren trades |
| 0 FILLED, veel "no edge" | Kelly edge check op copy trades? Moet er niet zijn |
| 0 FILLED, veel "price moved too much" | Slippage cap te strak, of positions te oud (drift filter) |
| 0 FILLED, veel "already open position" | Normaal — bot heeft al posities op die markten |
| 0 FILLED, geen SKIP/REJECT | Geen signalen → pollt de bot? Werkt de API? |
| 0 FILLED, "daily loss limit" | Foute bankroll berekening triggert loss limit |

**KRITISCH:** Copy trades mogen NOOIT geblokkeerd worden door een Kelly edge check. De wallet's track record IS de edge.

---

## STAP 2: VOLGEN WE DE JUISTE WALLETS?

### A. Wallet Performance — Wie verdient geld voor ons?

```bash
# Per-wallet win/loss uit onze eigen trades
ssh root@78.141.222.227 'python3 -c "
import json
from collections import defaultdict
trades = [json.loads(l) for l in open(\"/opt/bottie/data/trades.jsonl\") if l.strip()]
resolved = [t for t in trades if t.get(\"filled\") and not t.get(\"dry_run\") and t.get(\"result\") in (\"win\",\"loss\")]
by_w = defaultdict(lambda: [0,0,0.0])
for t in resolved:
    w = (t.get(\"copy_wallet\") or \"?\")[:10]
    by_w[w][0 if t[\"result\"]==\"win\" else 1] += 1
    by_w[w][2] += t.get(\"pnl\", 0)
for w, (wins,losses,pnl) in sorted(by_w.items(), key=lambda x: x[1][2], reverse=True):
    total = wins+losses
    if total >= 3:
        print(f\"{w:<12} {wins:>3}W {losses:>3}L {wins/total*100:>5.1f}% pnl=\${pnl:>7.2f}\")
"'
```

| Situatie | Actie |
|----------|-------|
| Wallet WR > 55% en PnL positief | ✅ Houden, gewicht verhogen |
| Wallet WR 45-55% op > 10 trades | ⚠ Observeren |
| Wallet WR < 45% op > 10 trades | ❌ Bespreken met user — gewicht verlagen? |
| Wallet 0 trades | ❌ Waarom staat hij erop? Verwijderen of wachten? |

### B. Scout Rapport — Vindt de scout betere wallets?

```bash
# Laatste scout rapport
ssh root@78.141.222.227 'python3 -c "
import json
r = json.loads(open(\"/opt/bottie/data/scout_report.json\").read())
print(f\"Scout report: {r.get(\"timestamp\",\"?\")[:19]}\")
print(f\"Evaluated: {r.get(\"candidates_evaluated\",0)} candidates\")
adds = r.get(\"recommended_additions\", [])
for a in adds[:5]:
    print(f\"  ADD: {a[\"name\"]:20s} WR={a[\"win_rate\"]:.0%} sharpe={a[\"sharpe\"]:.2f} closed={a[\"closed_positions\"]}\")
rems = r.get(\"recommended_removals\", [])
for rm in rems:
    print(f\"  REMOVE: {rm[\"name\"]:20s} {rm[\"reason\"]}\")
"'

# Draait de scout?
ssh root@78.141.222.227 'systemctl is-active wallet_scout 2>/dev/null'
```

### C. Autoresearch — Past het de watchlist aan?

```bash
# Laatste autoresearch cycle
ssh root@78.141.222.227 'journalctl -u autoresearch --since "6 hours ago" --no-pager 2>/dev/null | grep -E "ADJUST|ADD|DEMOTE|RESEARCH CYCLE" | tail -10'
```

Autoresearch moet wallets toevoegen/verwijderen/gewicht aanpassen op basis van het scout rapport. Als het config parameters tweakt (kelly, sizing, delays) → het doet het verkeerde.

---

## STAP 3: GEEN TEGENSTRIJDIGE BETS?

```bash
# Check of er posities zijn op beide kanten van dezelfde markt
ssh root@78.141.222.227 'python3 -c "
import json
from collections import defaultdict
trades = [json.loads(l) for l in open(\"/opt/bottie/data/trades.jsonl\") if l.strip()]
open_t = [t for t in trades if t.get(\"filled\") and not t.get(\"dry_run\") and t.get(\"result\") is None]
by_event = defaultdict(list)
for t in open_t:
    slug = t.get(\"event_slug\") or t.get(\"market_title\",\"\")[:30]
    if slug:
        by_event[slug].append(t)
conflicts = {k: v for k, v in by_event.items() if len(v) > 1}
if conflicts:
    print(f\"CONFLICT: {len(conflicts)} events met meerdere posities\")
    for slug, trades in list(conflicts.items())[:5]:
        outcomes = [f\"{t.get(\"outcome\",\"?\")} @ {t.get(\"price\",0):.0%}\" for t in trades]
        print(f\"  {slug[:40]}: {outcomes}\")
else:
    print(\"Geen conflicten gevonden\")
"'
```

---

## STAP 4: SYSTEEM GEZONDHEID

```bash
# Services
ssh root@78.141.222.227 'systemctl is-active bottie autoresearch wallet_scout 2>/dev/null'

# Recente errors
ssh root@78.141.222.227 'journalctl -u bottie --since "1 hour ago" --no-pager -p err 2>/dev/null | tail -5'

# Positions API werkt?
ssh root@78.141.222.227 'journalctl -u bottie --since "10 min ago" --no-pager 2>/dev/null | grep -c "positions fetch failed"'

# Bankroll sync werkend?
ssh root@78.141.222.227 'journalctl -u bottie --since "10 min ago" --no-pager 2>/dev/null | grep "SYNC.*balance" | tail -1'

# Laatste trade
ssh root@78.141.222.227 'journalctl -u bottie --no-pager 2>/dev/null | grep "FILLED:" | tail -1'

# Disk
ssh root@78.141.222.227 'df -h / | tail -1'
```

---

## STAP 5: RAPPORTEER

**Formaat:**

```
# /check Report — [datum]

## Stand van zaken
Portfolio: $XXX (user) | Geïnvesteerd: $XXX | Rendement: +/-XX%
On-chain USDC: $XX | Bot bankroll: $XX
Services: bottie [OK/DOWN] | autoresearch [OK/DOWN] | scout [OK/DOWN]

## Tradeert de bot?
Fills afgelopen uur: XX | Skips: XX | Rejects: XX
Belangrijkste skip reden: [reden] (XX keer)

## Wallets
Beste wallet: [naam] XX% WR, $XX PnL
Slechtste wallet: [naam] XX% WR, $XX PnL
Scout aanbevelingen: [XX toevoegen, XX verwijderen]

## Draagt bij aan het doel?

### ✅ Draagt bij
- [Component]: [waarom]

### ❌ Draagt NIET bij
- [Component]: [waarom niet] → [simpelste fix]

### ❓ Onduidelijk
- [Component]: [wat we niet weten]

## Aanbeveling
[Eén concrete actie die de grootste impact heeft]
```

---

## STAP 6: WACHT OP DE USER

**NOOIT auto-fixen. NOOIT.**

Presenteer bevindingen. Wacht op akkoord. Eén wijziging per keer.

---

## ANTI-PATTERNS

1. **NOOIT bankroll/PnL berekenen** — vraag de user, gebruik de Polymarket UI
2. **NOOIT een werkend systeem "verbeteren"** — als het geld maakt, laat het met rust
3. **NOOIT meerdere dingen tegelijk veranderen** — één wijziging, verifiëren, dan volgende
4. **NOOIT complexiteit toevoegen** zonder bewijs dat het bijdraagt aan het doel
5. **NOOIT scripts schrijven** als een simpele vraag aan de user volstaat
6. **NOOIT auto-fixen** — rapporteer en wacht op de user
7. **NOOIT parameters veranderen op basis van eigen berekeningen** — verifieer eerst met echte data
8. **NOOIT Kelly edge checks toepassen op copy trades** — de wallet's track record is de edge
9. **Na elke deploy: grep FILLED** — niet alleen systemctl is-active

## BEKENDE VPS DETAILS

- **VPS path:** `/opt/bottie/`
- **Binary:** `/opt/bottie/bottie-bin` (NIET target/release/bottie)
- **Deploy:** `cargo build --release && cp target/release/bottie bottie-bin && systemctl restart bottie`
- **Services:** bottie, autoresearch, wallet_scout
- **Positions API:** `sortBy=CURRENT` (uppercase, NIET currentValue)
