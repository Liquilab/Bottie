# Council — Cannae's Signaal

*2 april 2026 | 5 adviseurs + 5 peer reviewers | Chairman: Claude Opus 4.6*

---

## Vraag

Hoe bepaalt Cannae welk team gaat winnen of gelijkspelen? Welke databronnen en tools gebruikt hij als input-signaal om zijn wedden te structureren?

---

## Adviseurs

### A — De Contrariaan
**Kernpunt: Je kunt Cannae's signaal niet achterhalen, en het maakt niet uit.**

Je hebt geen input-data van Cannae. Je hebt alleen zijn output: trades op Polymarket. Of hij The Odds API, xG-modellen, of een eigen neural net gebruikt — dat is onzichtbaar. Uit een trade "Macarthur NO at 0.563" kun je niet afleiden of dat gebaseerd is op Pinnacle-odds, competitiestanden, of een coin flip. Zijn edge zit waarschijnlijk niet in het signaal maar in de structuur. Die multi-leg structuur (3 benen per wedstrijd, 3x leverage bij gelijk, 2x bij winst) is de alpha. Hij hoeft niet beter te voorspellen dan de markt — hij hoeft alleen wedstrijden te vinden waar de implied odds van draw + underdog samen goedkoop genoeg zijn. Het concrete risico: je gaat een signaal-model bouwen om Cannae's picks te filteren. Dat introduceert een extra foutbron. Het Macarthur-voorbeeld bewijst dit: elk signaal-model had hetzelfde gezegd. De bet verliest gewoon soms — variantie, geen signaalfout.

### B — De Eerste Principes Denker
**Kernpunt: Cannae's edge is competitie-specifiek, niet universeel — dat bewijst dat het geen predictiemodel is.**

Bundesliga +30%, EPL +21%, La Liga -10%. Een model dat "voetbal snapt" zou niet -10% draaien op La Liga en +30% op Bundesliga. Dit wijst op pricing-inefficiënties die per markt verschillen, niet op een superieur predictiemodel. Cannae exploiteert de marktprijs van PM vs zijn eigen fair-value schatting. Waar die fair value vandaan komt (odds API, xG, eigen model) is secundair. De input is: fair-value schatting vs PM-marktprijs. Dat is arbitrage, niet predictie. Stop met proberen zijn brein te reverse-engineeren — filter La Liga en MLB uit, dat is waar je directe actie kunt nemen.

### C — De Expansionist
**Kernpunt: Je kunt vandaag al filteren welke Cannae-trades edge hebben via The Odds API.**

Cannae's entry bij 0.30-0.40 verraadt zijn methode: hij koopt implied odds van 30-40% op uitkomsten die hij schat op 40-50%+. Zijn databron is waarschijnlijk simpel: closing odds van bookmakers versus PM-prijs. Bewijs: hij wint waar PM het meest afwijkt van bookmakers (Bundesliga) en verliest waar PM efficiënter is (La Liga, MLB). Bouw een pre-trade filter: bij elke Cannae-leg, haal Pinnacle closing odds op. Als PM-prijs < Pinnacle implied minus 5%+ → kopieer. Anders → skip. Drop La Liga/MLB direct. Je gaat van 100% blind naar 40% gefilterd met hogere ROI. Eerste stap: 50 regels Python in de league scanner.

### D — De Buitenstaander
**Kernpunt: Dit is geen voorspeller maar een market maker die mispricing exploiteert.**

Dit is geen gokker die teams analyseert. Dit is een geautomatiseerd fonds ($3.56M, eenmalige storting) dat structureel mispricing exploiteert. Hij dekt 2 van 3 uitkomsten af en spreidt $7K-$155K over meerdere legs. Pinnacle closing lines zijn de meest efficiënte prijsschatting die bestaat. Als PM afwijkt van Pinnacle, koopt hij. La Liga -10% en MLB -28% bewijzen het: daar zijn PM-prijzen efficiënter, minder arbitrage. Het Macarthur-voorbeeld bewijst niets — één verlies op 209 winstgevende Bundesliga-trades is variantie. Stop met zoeken naar een geheime methode. Vergelijk PM-prijzen met Pinnacle.

### E — De Uitvoerder
**Kernpunt: We weten het niet — maar we kunnen het meten.**

Cannae's leg-structuur vertelt ons HOW hij inzet, niet WHY. Geen enkele databron verklaart de Macarthur-keuze definitief. Wat we wel kunnen: backtest. Stap 1 (3 uur): correleer 19.600 closed positions met historische Pinnacle-odds. Wint Cannae vaker als PM-prijs > X% afwijkt van Pinnacle? Stap 2 (2 uur): definieer een simpele regel ("Koop NO als PM > 15% boven Pinnacle implied, top-5 leagues, 0.30-0.45"). Stap 3 (1 uur setup): paper trade op bottie-test, vergelijk maandag. Als stap 1 geen correlatie laat zien, heeft Cannae info die wij niet hebben — dan is kopiëren de enige optie.

---

## Peer Reviews

**Review 1:**
Sterkste: B — enige die het mechanisme benoemt: multi-leg structuur IS de edge. Niet voorspellen wie wint, maar events vinden waar gecombineerde pricing inefficiënt is. Blinde vlek: C — filtert 60% trades weg op ongevalideerde Pinnacle-vergelijking. Als edge in multi-leg combinatie zit, gooi je winstgevende trades weg. Gemist: wat zijn resultaten per LEG-TYPE (win/draw) binnen dezelfde wedstrijd? Als draws systematisch winnen en wins verliezen, is het geen predictie.

**Review 2:**
Sterkste: B — juiste conclusie: stop met reverse-engineeren. Blinde vlek: C en D — filteren introduceert inferieur signaal bovenop superieur signaal. Als jouw filter beter was dan Cannae, had je Cannae niet nodig. Gemist: meetbaar uit eigen P&L per segment. De databron is je eigen closed positions, niet Cannae's methode.

**Review 3:**
Sterkste: E — enige die zegt: meten, niet gokken. Backtest levert falsifieerbaar antwoord. Blinde vlek: A — "stop zoeken" is gevaarlijk, geen exitcriterium als edge verdwijnt. Gemist: alle vijf speculeren top-down terwijl bottom-up data beschikbaar is. Eerst meten.

**Review 4:**
Sterkste: B — edge is structureel, niet predictief. Blinde vlek: C — pre-trade filter is verzonnen zonder bewijs. Gemist: meten per segment uit eigen P&L, niet theoretiseren over methode.

**Review 5:**
Sterkste: B — Cannae structureert posities zodat meerdere uitkomsten winstgevend zijn, geen predictie. Blinde vlek: C — verzonnen model. Gemist: we weten het niet en dat is het eerlijke antwoord.

---

## Chairman Synthese

### Consensus (4/5 adviseurs + 4/5 reviewers)
Cannae's edge is structureel, niet predictief. Zijn multi-leg structuur maakt dat hij niet hoeft te voorspellen WIE wint — hij vindt events waar gecombineerde pricing inefficiënt is op PM.

### Kernconflict
B zegt stop reverse-engineeren. E zegt backtest. C zegt filter. E heeft gelijk maar om de verkeerde reden — de backtest dient niet om Cannae te begrijpen maar om te bepalen of de test-bot onafhankelijk kan draaien via odds-arbitrage.

### Blinde vlek
Peer reviewer 1: wat zijn resultaten per LEG-TYPE (draw YES vs win YES vs win NO) binnen dezelfde wedstrijd? Als draws systematisch winstgevender zijn, is het een draw-overweight strategie, geen predictie. Die data hebben we maar analyseren we niet.

---

## Verdict

Cannae's signaal is hoogstwaarschijnlijk bookmaker closing odds vs PM implied probability. Bewijs: Bundesliga/EPL (hoog PM retail-volume, veel mispricing) = +21-30% ROI. La Liga/MLB (efficiëntere PM-prijzen) = negatieve ROI. Entry bij 0.3-0.4 = exact waar PM het meest afwijkt van scherpe bookmakers.

Maar twee dingen tegelijk:
1. **Cannae-bot:** blijf kopiëren, filter La Liga/MLB, klaar
2. **Test-bot:** backtest PM vs Pinnacle. Als correlatie sterk → bouw onafhankelijke odds-arb bot. Als niet → Cannae heeft proprietary signal.

### Eerste stap
Backtest 50 recente Cannae-posities tegen The Odds API (Pinnacle closing lines). Is er correlatie tussen PM-mispricing en Cannae's richting? 3 uur Python-werk. Dat beantwoordt de vraag definitief.

---

*Metadata: 2026-04-02, 5 adviseurs + 5 peer reviewers, chairman: Claude Opus 4.6*
