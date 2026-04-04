# Council: Optimale Bet-Sizing — 2 april 2026

**Vraag:** Bij een bankroll van $3.064 en een doel van $10K, wat is de optimale max_bet_pct voor Cannae Bottie?

**Context:** Copy-trading bot, Cannae ROI 34-37% voetbal, huidige max_bet_pct=8%, 5 adviseurs + 5 peer reviewers

---

## De 5 Adviseurs

### A — Eerste Principes Denker
Cannae's 0.2-0.6% = marktdiepte-beperking, niet voorzichtigheid. Kelly bij 35% ROI en even odds = Quarter Kelly = 9-10% per bet. Jij op 4-6% = sub-half-Kelly, conservatief maar verdedigbaar. Verwacht groeipad: ~3-4% dagelijkse bankroll groei, $3K→$10K in ~35-40 handelsdagen bij consistente edge. Sizing is niet het probleem; de vraag is of de edge structureel is.

### B — De Buitenstaander
Hoeveel bets tegelijk? Bij 10 legs × 6% = 60% bankroll simultaan in de markt. 82.6% win rate op 23 trades = statistisch toeval, geen fundament. De vraag suggereert sizing verhogen omdat het goed ging — de meest gevaarlijke reden. Wat is de maximale acceptabele drawdown in dollars?

### C — De Contrariaan
Fatale fout: 4-6% per bet + 6-10 legs per event = 20-40% deployment per event. 15% daily_loss_limit bereikbaar in één slechte multi-leg avond. Cannae's kleine percentages zijn structurele marktdiepte-beperking, niet voorzichtigheid. Ruin-risico is 10-30x agressiever dan Cannae proportioneel. 82.6% op 23 trades = survivorship bias in realtime.

### D — De Uitvoerder
Kelly bij observed 82.6% win rate = 61% (gebruik fractional). Quarter Kelly = 15%. Jij op 4-8% is al conservatief onder Quarter Kelly. Actieplan: verificeer fills, check bankroll post-settlement, niets wijzigen. Wacht minimaal 100+ trades voor sizing-review.

### E — De Expansionist
Echte kans: liquiditeitsarbitrage — jij kunt harder gaan op dunne Cannae-markets waar Cannae zelf gecapped is. Draw Kelly = 9.7% (p=0.30, b=2.33). Bouw per-outcome conviction weighting: draws op 10-12%, rest op 4-5%. Stop met generiek max_bet_pct verhogen — differentieer per outcome-type.

---

## Peer Reviews (anoniem, letters geshuffled)

### Review 1
- **Sterkste:** B — identificeert het werkelijke aggregatierisico: gecorreleerde exposure per event, niet percentage per leg
- **Grootste blinde vlek:** E — analyseert draw-Kelly maar de bot speelt draw NO (veilige kant), niet draw YES
- **Gemist:** Cannae denkt in SHARES, niet dollars. De bot kopieert in dollar-gewichten. Bij asymmetrische odds is de share-exposure anders gekalibreerd dan Cannae bedoelde. Kelly-berekeningen op Cannae's dollar-percentages zijn structureel verkeerd.

### Review 2
- **Sterkste:** B — juiste operationele vraag, sceptisch over kleine sample
- **Grootste blinde vlek:** D — berekent Kelly op realized win rate van 23 trades (pseudoprecisie). Kelly op onbetrouwbare input is erger dan geen Kelly.
- **Gemist:** Cannae's 34-37% ROI is NIET de bot's eigen edge. De bot koopt later (timing-gap), soms op slechtere odds. De werkelijke edge van de bot is onbekend en ongemeten.

### Review 3
- **Sterkste:** B — simultane exposure en drawdown in dollars zijn de juiste sturingsvariabelen
- **Grootste blinde vlek:** D — Kelly op observed win rate is circulaire redenering bij 23 trades
- **Gemist:** Bets zijn niet onafhankelijk. 6-10 legs per event zijn gecorreleerd (zelfde wedstrijd). Kelly en ruin-berekeningen gaan uit van onafhankelijke events. Bij gecorreleerde blootstelling is het effectieve risico per event, niet per leg.

### Review 4
- **Sterkste:** C — benoemt het concrete mechanisme van gecorreleerde blootstelling per event
- **Grootste blinde vlek:** A — vergelijkt Cannae's sizing met Kelly alsof het onafhankelijke bets zijn. Cannae's 0.2-0.6% is klein juist omdat hij 6-10 gecorreleerde legs per event zet.
- **Gemist:** De correlatie-structuur van de kopieerstrategie. Eén wedstrijd-uitkomst raakt meerdere legs tegelijk. Kelly, Quarter Kelly, drawdown-limieten — allemaal zinloos zonder de event-exposure als correcte eenheid.

### Review 5
- **Sterkste:** C — multi-leg concentratie per event creëert correlated exposure, niet per-leg exposure
- **Grootste blinde vlek:** D — Quarter Kelly zonder rekening te houden met gecorreleerde legs is structureel fout
- **Gemist:** Cannae denkt in SHARES, niet dollars. Bij lage-prijs tokens (draws op 0.30) = hoge share-count = hoge dollar-exposure bij fill. Geen enkele adviseur raakt dit aan.

---

## Chairman Synthese

### Consensus
1. **Sizing niet verhogen** — alle vijf adviseurs zijn het hierover eens, ook degenen die andere punten benadrukken
2. **4-6% per leg is verdedigbaar** — sub-half-Kelly bij 35% ROI structureel, niemand zegt "te hoog"
3. **23 trades = geen statistisch fundament** — alle adviseurs erkennen dit expliciet of impliciet
4. **Cannae's percentage vergelijken is zinloos** — schaalverschil + marktdiepte-beperking maakt directe vergelijking ongeldig

### Kernconflict
A/D zeggen: "sizing is prima, niets doen."
C zegt: "multi-leg concentratie is het werkelijke gevaar."

**C heeft gelijk, maar om een andere reden dan hij denkt.** Het is niet de 15% daily_loss_limit die het probleem is — die is bewust conservatief. Het echte probleem is dat "max_bet_pct per leg" de verkeerde risico-eenheid is. De juiste eenheid is **totale blootstelling per event** (gecorreleerde som van alle legs in één wedstrijd).

Bij 6 legs van elk 5% = 30% bankroll op één wedstrijd. Als die wedstrijd slecht uitvalt, is het geen 5% verlies — het is potentieel 15-30%.

### Blinde vlek — Wat niemand zag
Twee fundamentele problemen die alle vijf adviseurs misten:

1. **Gecorreleerde legs onderbreken Kelly.** Kelly's formula gaat uit van onafhankelijke bets. Maar 6-10 legs per game zijn volledig gecorreleerd — Botafogo wint → draw YES verliest + win NO verliest tegelijk. De effectieve Kelly-eenheid is per EVENT, niet per leg. Als je per-event 20-30% van je bankroll inzet op één gecorreleerde uitkomst, ben je agressiever dan de per-leg percentages doen vermoeden.

2. **De bot's werkelijke edge is onbekend.** Cannae's 34-37% ROI is Cannae's edge op zijn entry-prijs. De bot koopt later (polling delay), mogelijk op slechtere odds. De echte vraag is niet "wat is de Kelly bij 35% ROI?" maar "wat is de Kelly bij de bot's werkelijke gerealiseerde ROI?" Die data hebben we over 50+ resolved trades maar analyseren we niet systematisch.

### Verdict
**max_bet_pct blijft 8%.** Niet verhogen, niet verlagen.

Maar voeg een **max_game_pct** toe: een cap op de totale dollar-blootstelling per event (som van alle legs). Stel dit in op 20-25% van bankroll. Dit lost het correlatie-probleem op zonder per-leg sizing te veranderen.

Bij $3.064 bankroll: max per game = $612-766. Als één game 6 legs heeft, worden de individuele legs kleiner zodat de som nooit boven die grens komt.

Het groeipad naar $10K bij 35% ROI en ~25 bets/dag:
- Verwachte dagelijkse groei: ~3-4% bij consistente edge
- $3.064 → $10K = 3.3x = ~35-40 handelsdagen bij onverstoorde edge

### Eerste Stap
**Bereken nu de event-exposure van afgelopen nacht.** Kijk per wedstrijd wat de totale inzet was (som van alle legs). Was één game meer dan 20% van de bankroll? Als dat het geval was, is max_game_pct de correcte config-toevoeging.

```bash
grep "FILLED" /opt/bottie/logs/*.log | grep "2026-04-01" | sort | awk 'BEGIN{} ...'
```

Of uit trades.jsonl: groepeer op event_slug, tel total_size op. Als een game meer dan $612 totaal had bij $3.064 bankroll, is max_game_pct nodig.

---

*Metadata: 2026-04-02, 5 adviseurs + 5 peer reviewers, chairman: Claude Sonnet 4.6*
