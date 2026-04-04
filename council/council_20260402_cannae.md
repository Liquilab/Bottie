# Council: Cannae Alpha Reverse Engineering
**Datum:** 2026-04-02
**Vraag:** Cannae heeft aantoonbaar alpha op Polymarket (+7.4% net ROI over $13.6M geïnvesteerd, met uitschieters tot +44% in specifieke competities). Wat is de mechanische bron van die edge, en welke aanpassingen aan onze copy-trading strategie maximaliseren het kopieerrendement?
**Adviseurs:** 5 + 5 peer reviewers
**Chairman:** Claude Sonnet 4.6

---

## De 5 Adviseurs

### 1. De Contrariaan

De fatale fout: je kopieert een prijsstrategie zonder de prijs te weten. Cannae's scherpste edge zit in de 0.3-0.4 prijsrange (+36-38% ROI). Dat betekent: hij koopt onderprijsde underdogs vóórdat de markt ze correct prijst. Zijn alpha IS de timing. Jullie kopiëren T-5 minuten voor kickoff. Cannae is er uren eerder al in.

Op een voetbalmarkt beweegt de prijs significant in die uren. Cannae koopt bij 0.35 (ROI +37%). Jullie kopen bij 0.55 (ROI: onbekend, maar structureel slechter). Op La Liga (-10.3%) en MLB (-28.5%) doet hij het al slecht bij zijn eigen entry. Bij jullie entry wordt dat erger.

Bijkomend probleem: het "beide kanten" signaal. 50.5% van posities zijn hedges — netto breakeven. Jullie weet niet welke leg de echte directional bet is.

De test die ontbreekt: vergelijk fill-prijs per competitie met Cannae's gereconstrueerde entry-prijs. Tot die data er is, weet je niet wat je kopieert.

---

### 2. De Eerste Principes Denker

Het werkelijke probleem: je kopieert een market maker, geen alpha-speler.

Bewijs:
- 50.5% beide kanten, netto +0.5% (breakeven)
- 2.6% bankroll deployed op $3.56M — geen conviction-trading, liquiditeitsverschaffing
- $1M profit op hoofdbets, -$168K netto totaal
- Één storting, nooit bijgevuld — ruikt naar een fonds, niet een retail-alpha-speler

Cannae's edge is waarschijnlijk: vroeg instappen bij slechte initiële prijzen, waarna de markt naar hem toekomt. Zijn "alpha" in Bundesliga/EPL is prijs-appreciatie terwijl jij T-5min instapt — dus ná die appreciatie.

Stop met optimaliseren van kopieerstrategie totdat je de prijs-delta kent. Bouw eerst één ding: log Cannae's entry-prijs bij detectie én jouw fill-prijs. Na 50 wedstrijden heb je de echte data.

---

### 3. De Expansionist

De mechanische bron:
1. **Prijsarbitrage bij lage liquiditeit** — 0.3-0.4 range is waar PM-markten structureel mispriced zijn
2. **Competentie-selectiviteit** — Bundesliga/EPL/NFL +21-41%, La Liga -10.3%, MLB -28.5%
3. **Hedging = noise, niet alpha**

De grotere kans: competitie-gewogen kopiëren. Voeg `roi_multiplier` toe:
- Bundesliga = 1.5x
- EPL = 1.3x
- La Liga = 0x
- MLB = 0x
- NBA/NHL = 0.5x

Tweede kans: log entry-prijsverschillen per competitie. Als verschil >0.05 bij EFL/NFL-legs, niet kopiëren.

Stop egalitair kopiëren. Cannae heeft ongelijke alpha; onze sizing moet dat ongelijk reflecteren.

---

### 4. De Buitenstaander

De edge is prijsarbitrage, niet informatiesuperioriteit. Cannae koopt vroeg bij 0.3-0.4, voordat de markt corrigeert. Jij kopieert hem vijf minuten voor aftrap — op een al gecorrigeerde markt.

Drie observaties van buiten:
1. La Liga -10.3%, MLB -28.5% — je kopieert die ook, waarom?
2. 50.5% both-sides breakeven — wat als de hoofdbet de hedge is en de echte alpha in de positie zit die je negeert?
3. 2.6% gedeployed: selectiviteit is zijn edge — jij elimineert die door alles mee te nemen

Ongezouten: je hebt geen copy-strategie. Je hebt een vertraagde versie van Cannae's portefeuille, gefilterd op de verkeerde criteria, ingekocht op de verkeerde prijzen.

---

### 5. De Uitvoerder

Drie stappen.

**Stap 1: Stop MLB volledig, filter hedge-posities (30 min)**
Data is conclusief, -28.5% is geen signaal, het zijn kosten.

**Stap 2: Prijsfilter 0.25-0.50 range implementeren (1 uur)**
ROI +36-38% zit daar, boven 0.8 slechts +9.4%.

**Stap 3: Log fill-prijs slippage voor één week (passief)**
Cannae's detected price bij detectie vs. onze fill-prijs. Na één week heb je data.

Wat je NIET doet: ACN/EFL targeten (n=13 en n=5, statistisch ruis). Niet verder optimaliseren zonder fill-data.

---

## Peer Reviews (anoniem)

### Review 1
**Sterkste:** A — enige die de fundamentele vraag stelt: is dit kopieerbare alpha? De observaties wijzen op market maker of fonds.

**Grootste blinde vlek:** E — bouwt precies systeem op statistisch ruis.

**Gemist:** Cannae's entry-prijs is nooit gereconstrueerd. Iedereen neemt aan dat hij vroeg instapt, maar dit is nooit gevalideerd. Alle vijf optimaliseren een kopieerstrategie terwijl de kernvraag onbeantwoord blijft.

---

### Review 2
**Sterkste:** A — stelt de fundamentele vraag die de anderen overslaan: market maker of alpha-speler? Die diagnose maakt alle optimalisatieadviezen van B/C/D/E potentieel irrelevant.

**Grootste blinde vlek:** B — puur tactisch zonder theoretische basis.

**Gemist:** de realiseerbaarheid van de vergelijking. Niemand vraagt: hebben we die prijsdata al? Zonder tijdgestempelde prijsvergelijking zijn alle aanbevelingen speculatie.

---

### Review 3
**Sterkste:** C — enige die een concrete, falsifieerbare test benoemt: vergelijk fill-prijs met Cannae's entry-prijs. Direct uitvoerbaar.

**Grootste blinde vlek:** E — roi_multiplier zonder te begrijpen waarom ROI per league verschilt. Symptoombestrijding.

**Gemist:** de vraag veronderstelt dat Cannae's +7.4% repliceerbaar is via kopiëren, maar niemand betwist die aanname. Als Cannae's edge in liquiditeitsproviding zit, is die structureel niet kopieerbaar.

---

### Review 4
**Sterkste:** C — mechanische kern direct: entry-prijs, niet selectie. Falsifieerbare hypothese.

**Grootste blinde vlek:** E — symptoombehandeling zonder causale structuur.

**Gemist:** Cannae's fill-prijs is misschien helemaal niet observeerbaar via publieke on-chain data. On-chain toont de order, niet de werkelijke fill tegen welk orderboek-niveau. Hebben we toegang tot Cannae's actual fill-prijzen?

---

### Review 5
**Sterkste:** C — benoemt de kern concreet.

**Grootste blinde vlek:** E — roi_multiplier is circulair, past gewicht aan op historische ROI zonder te begrijpen waarom.

**Gemist:** welk percentage van Cannae's profit komt uit posities die een follower überhaupt kan innemen tegen vergelijkbare prijzen, versus posities waarbij de edge per definitie verdampt bij kopiëren?

---

## Chairman Synthese

### Consensus (alle vijf eens)
1. Entry-prijs delta is de cruciale onbekende — wij weten niet of onze fill-prijs gelijk is aan Cannae's entry-prijs
2. MLB is verlieslatend voor Cannae én voor ons — uitzetten is het laag-hangend fruit
3. Het 0.3-0.4 prijsvenster is Cannae's sweet spot — boven 0.8 dramatisch minder ROI

### Kernconflict
A zegt: "stop alles, meet eerst de prijs-delta." E zegt: "maak het systeem slimmer terwijl je wacht." Peer reviews kozen A of C als sterkste. Maar in de praktijk is de keuze vals: je kunt tegelijkertijd de prijs-delta gaan meten EN het laag-hangend fruit implementeren (MLB uit, prijsfilter aan). A's diagnose is correct, maar "stop alles" is analytisch luiheid in omgekeerde richting.

### Blinde vlek — wat niemand volledig uitwerkte
Twee peer reviews prikten op iets cruciaals: Cannae's werkelijke fill-prijs is mogelijk niet observeerbaar. De data-api toont Cannae's trade-prijs wanneer hij iets koopt, maar GTC-orders fillen later. Echter: onze bot logt de marktprijs bij T-5 detectie. Dat IS Cannae's gereconstrueerde entry in onze context, want wij kopen op de ask op dat moment.

Wat werkelijk gemist werd: **de "beide kanten" semantiek is omgekeerd dan gedacht.** Als Cannae 50.5% van de tijd beide kanten houdt en netto +0.5% verdient, zijn zijn hedges GRATIS VERZEKERING. Hij verdient op zijn directional bet (+7.4% hauptbet ROI) en verliest minimaal op de hedge. Wij kopiëren alleen de directional bet — dat is precies wat we moeten doen. De hedge-posities zijn al correct gefilterd uit onze strategie.

### Verdict
Cannae's alpha heeft drie pijlers:

1. **Entry-timing** — vroeg instappen (uren voor kickoff) bij 0.3-0.4 prijzen die later bewegen
2. **Competentie-specialisatie** — Bundesliga +30%, EPL +21%, NFL +41% zijn structureel. La Liga -10.3%, MLB -28.5% zijn structureel slecht
3. **Directionele positie** (win/draw hoofdbet) die wij correct kopiëren — de hedges zijn noise

Onze copy-strategie pakt pijler 3 correct. Pijler 2 deels (we negeren La Liga niet expliciet). Pijler 1 missen we volledig. De timing-vraag is de enige die er echt toe doet.

### Eerste Stap
Bereken vandaag de prijs-delta: wat was de marktprijs van elke positie op het moment van onze eerste detectie in de bot-logs, versus onze actual fill-prijs? Die delta vertelt in één tabel of timing het lek is (delta >0.03 = timing is het probleem) of niet (delta <0.01 = iets anders). Die analyse bestaat al in de data — het is één Python script op de trades.jsonl.

---

*5 adviseurs + 5 peer reviewers + chairman synthese*
*Claude Sonnet 4.6 — 2026-04-02*
