# Onderzoeksplan: Cannae strategie-shift naar O/U + Spread?

**Aangemaakt:** 2026-04-07
**Auteur:** Koen + Claude (chat session)
**Status:** plan, niet uitgevoerd
**Aanleiding:** Wedstrijd `arg-iac-def-2026-04-06` (Instituto AC Córdoba vs CSyD Defensa y Justicia) — Cannae's hauptbet was O/U met >$6K, terwijl historisch Cannae als win/draw-specialist gold. Vermoeden: strategie-shift de afgelopen ~7 dagen.

---

## Hypotheses

- **H1:** Cannae heeft de afgelopen ~7 dagen z'n hauptbet-allocatie verschoven van Win/Draw naar O/U + Spread.
- **H2:** Z'n hit-rate en ROI op O/U + Spread is in die periode beter dan op Win/Draw.
- **H3:** Voor NBA betekent dit dat de oude per-game-total gate (ROI +29.6% bij ≥$4K, gemeten 2026-04-07) niet "Cannae's ML conviction" mat, maar **"Cannae's totale conviction in dit spel via z'n O/U/spread-stack"**. Dat zou een proxy-signaal zijn dat we als ML-copier kunnen gebruiken zelfs al kopieren we de OU/spread leg niet.

> Als H3 klopt, verandert de framing fundamenteel: de gate is geen kwaliteitsfilter op ML-data, maar een proxy voor Cannae's totale game-conviction. De fix die op 2026-04-07 in commit `0a076b7` werd gedaan (per-leg-only gate) zou dan een werkend signaal hebben gebroken.

---

## Data-bronnen & coverage

| Bron | Wat | Coverage | Locatie |
|---|---|---|---|
| `bottie trades.jsonl` | Bottie's eigen trades met sport, leg, pnl | Sinds 2026-03-27 | `/opt/bottie/data/trades.jsonl` |
| Journal `T5 POSITIONS` | Per-leg Cannae stake breakdown | **Pas vanaf 2026-04-01** ⚠️ | `journalctl -u bottie` |
| Journal `T5 PLAN` summaries | `$N | M legs (win+ou+spread)` per game | Volledig sinds 2026-03-27 | `journalctl -u bottie` |
| PM data-API `/positions` | Cannae's huidige open posities | Live snapshot, niet historisch | `data-api.polymarket.com` |
| PM data-API `/activity` | Cannae's trade history (TRADE events met timestamp) | Historisch retrievable — **schema ONBEVESTIGD voor sport metadata** | `data-api.polymarket.com` |
| PM gamma `/positions?user=…` | Idem, ander endpoint | Idem | `gamma-api.polymarket.com` |
| Lokale Cannae datasets | Bestaande analyses uit `research/cannae_*/` | Onbekend, te checken | `research/cannae_trades/`, `research/cannae_quant_analysis/` |

**Cannae bron-wallet:** `0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b`
(Niet de Bottie executing wallet `0x89dcA91b...` — zie memory `wallets_cannae_vs_bottie.md`)

---

## Plan in 5 stappen

### Stap 1 — Data-source discovery (~30 min, read-only)

Doel: weten of we überhaupt Cannae's resolved O/U + Spread trades met PnL kunnen ophalen voor de afgelopen 7-30 dagen. Zonder dat is alles speculatie.

**Parallelle deelacties:**

1. **PM activity API**: probeer `data-api.polymarket.com/activity?user=0x7ea571c4...&limit=1000&type=TRADE` voor de Cannae bron-wallet. Check welke velden er zijn: `token_id`, `side`, `price`, `size`, `timestamp`, en kritisch: **resolved status + payout** of equivalent.
2. **PM positions met date filter**: probeer `data-api.polymarket.com/positions?user=...&sizeThreshold=0` en check of resolved (closed) positions ook terugkomen, met `realized_pnl` of vergelijkbaar.
3. **Lokale research check**: `ls research/cannae_*/` + `find research -name "*.jsonl"`. Memory zegt er was ooit "16.761 Cannae positions" geanalyseerd. Mogelijk bestaande dataset of script — niet duplicaat werken.
4. **Instituto-Defensa verificatie**: zoek `arg-iac-def-2026-04-06` in journalctl. Bevestig hauptbet was O/U met >$6K, en kijk wat Cannae op win/draw had. Dit is anchor-evidence voor de hele hypothese — als het er niet is, scope herzien.

**Beslis-punt na Stap 1:**
- ✅ Resolved Cannae trades met sport+leg+PnL bereikbaar → door naar Stap 2.
- ❌ Niet bereikbaar → terug naar gebruiker met de vraag of we toegang tot een andere bron krijgen (Dune query, PM intern endpoint, of handmatige export).

---

### Stap 2 — Cannae per-leg ROI baseline (1-2 uur)

**Voorwaarde:** Stap 1 levert bruikbare bron.

Bouw een Cannae-PnL dataset met velden:
```
{date, sport, league, event_slug, leg_type, outcome, side, size_usdc,
 entry_price, exit_price_or_resolved, pnl, hauptbet (highest cv in game)}
```

Waar `leg_type ∈ {win, draw, spread, ou, btts, player_prop}`.

**Per leg_type, per sport, voor twee tijdsvensters:**
- **Lange historie**: alles wat we kunnen krijgen, bv 30+ dagen tot 2026-03-31.
- **Recente week**: 2026-04-01 → 2026-04-06 (en doorlopend bijhouden).

**Tabel-output:**

| Periode | Sport | Leg | n | WR | ROI | Wilson LB |
|---|---|---|---|---|---|---|
| Lange | voetbal | win | … | … | … | … |
| Lange | voetbal | draw | … | … | … | … |
| Lange | voetbal | ou | … | … | … | … |
| Lange | voetbal | spread | … | … | … | … |
| Lange | NBA | win | … | … | … | … |
| Lange | NBA | spread | … | … | … | … |
| Lange | NBA | ou | … | … | … | … |
| Recent | voetbal | win | … | … | … | … |
| … | … | … | … | … | … | … |

**Beslis-punt:**
- Statistisch significante shift = delta ROI > 10pp tussen lange en recente periode **én** Wilson LB recente periode > 0.
- Ja → strategie-shift bevestigd → Stap 3.
- Nee → H1+H2 verworpen → spring naar Stap 4 (alleen H3 testen).

---

### Stap 3 — Hauptbet-leg verschuiving meten (~30 min)

**Per game** (recente week + langere historie als baseline):
- Bepaal Cannae's hauptbet leg = leg met hoogste game-totaal CV.
- Tel hoe vaak hauptbet `win/draw` is vs `spread/ou/btts`.
- Plot percentage over tijd (per dag of per week).

**Acceptance:** percentage hauptbet=ou/spread aantoonbaar gestegen in afgelopen week (delta > 10pp) → strategie-shift bevestigd op gedragsniveau, niet alleen ROI.

---

### Stap 4 — Test H3 (proxy hypothesis) op bestaande Bottie data

Onafhankelijk van Stap 2-3. Bottie's historische trades (NBA én voetbal vanaf 2026-04-01, vanwege T5 POSITIONS coverage):

Voor elke Bottie trade kruisreferentie naar T5 POSITIONS log:
- Cannae's `total_game_cv` (oude gate metric)
- Cannae's `copyable_leg_cv` (nieuwe gate metric: alleen win/draw)

**Splits Bottie's PnL in 4 buckets:** hoog/laag op total × hoog/laag op copyable.

Welke metric correleert beter met Bottie's success?
- Vergelijk ROI in `total ≥X & copyable <Y` vs `total <X & copyable ≥Y` etc.
- Spearman correlatie tussen elke metric en trade pnl.

**Conclusie:**
- `total_game_cv` correleert beter → H3 waar → oude gate-semantiek was per ongeluk een goede proxy → fix uit commit `0a076b7` brak een werkend signaal.
- `copyable_leg_cv` correleert beter → H3 onwaar → nieuwe gate is correct, oude was misleidend.
- Beide ongeveer gelijk → onbeslist, meer data nodig.

---

### Stap 5 — Aanbeveling produceren

Op basis van Stap 2-4:

| H1+H2 | H3 | Actie |
|---|---|---|
| ✅ | ✅ | Bot uitbreiden om O/U + Spread te kopieren (`sport_sizing` flags), én oude gate herstellen of behouden naast de nieuwe |
| ✅ | ❌ | O/U + Spread copying inschakelen, nieuwe gate-semantiek behouden |
| ❌ | ✅ | Geen strategie-shift, maar oude gate was goede proxy. Revert commit `0a076b7` of gebruik beide gates als AND-conditie |
| ❌ | ❌ | Zoals nu, maar zonder vertrouwen in NBA — overweeg langere observatie (B-optie uit eerdere chat: lage drempel + data verzamelen) |

---

## Risico's & caveats

1. **PM data API kan beperkte historie hebben.** Sommige endpoints geven alleen open positions, geen resolved met PnL. Stap 1 moet dit hard verifiëren voor we doorgaan.
2. **Sport-classificatie van Cannae's trades is niet triviaal** zonder event slug. Als de API alleen `token_id` geeft, moeten we per token de market metadata ophalen — veel API calls.
3. **"Recente 7 dagen" is statistisch klein.** Cannae doet ~5-15 trades per dag → 35-100 trades per leg per week. Win/draw heeft genoeg n; OU/spread per sport mogelijk wankel.
4. **Het Instituto-Defensa voorbeeld is anekdotisch** — één hauptbet=OU bewijst geen trend. Stap 3 moet dit kwantitatief bevestigen.
5. **Coverage gap voor Bottie data**: T5 POSITIONS pas vanaf 2026-04-01. Stap 4 werkt alleen op trades vanaf die datum. n is dus klein (~16 NBA trades, voetbal te tellen).
6. **Tijd-investering**: realistisch 3-5 uur als Stap 1 een werkende data source vindt. Als API niet bruikbaar is, kan het oplopen naar 1-2 dagen of stranden.

---

## Open vragen voor user (vóór start)

1. **Scope**: alleen Cannae, of ook GIYN-bron-wallets (Tokidoki, kch123, Sovereign)?
2. **Sporten**: alleen NBA + voetbal, of breder (NHL, MLB, NFL ook)?
3. **Persistence van resultaten**: wil je dat ik Stap 2's tabellen als CSV of JSON ergens opsla in `research/cannae_strategy_shift/`?
4. **Stop-condities**: als Stap 1 strandt op data-toegang, wil je dat ik een aparte spike op Dune Analytics of een PM intern endpoint probeer, of stoppen we daar?

---

## Concrete eerste actie als akkoord

Stap 1 deelacties (1) + (3) + (4) parallel — allen pure-read, onafhankelijk:
1. `curl 'https://data-api.polymarket.com/activity?user=0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b&limit=10&type=TRADE'` — schema check
2. `ls research/cannae_*/` + check bestaande exports/scripts
3. `grep 'arg-iac-def-2026-04-06' /tmp/bottie_full.log` — verifieer Instituto hauptbet

Daarna pas Stap 2 in detail plannen op basis van wat bruikbaar is.
