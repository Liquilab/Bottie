# Council: Theorie Validatie — Cannae Strategie — 2 april 2026

**Vraag:** We hebben een theorie over Cannae's strategie reverse-engineered. Valideer de theorie en onderzoek wat er mist voordat we paper trades starten.

**Context:** Cannae is een bot (67% fills <5s) die 5-6 dagen voor wedstrijden Win NO + Draw YES koopt op Polymarket voetbal. ROI ~21.7% op $25.2M. Sizing correleert met winkans (Q5 bets: 70.2% WR vs Q1-Q4: 57%). Win NO = cash cow ($6.6M, 40.6% ROI). Hauptbet WR: EPL 64.5%, Bundesliga 68.4%. Survivorship bias in closed_positions waarschijnlijk.

---

## De 5 Adviseurs

### Adviseur 1 — De Eerste Principes Denker
**Kernpunt:** De theorie klopt grotendeels, maar bouwt op drijfzand bij twee cruciale punten.

1. **Data is vervuild.** Survivorship bias maakt de 21.7% ROI onbetrouwbaar — het is een bovengrens, geen schatting. De hauptbet-analyse (64.5-68.4% WR) is realistischer, maar de ROI is niet herberekend op hauptbet-niveau. Alles wat op de vervuilde ROI bouwt staat op losse schroeven.

2. **"Win NO + Draw YES" is een observatie, geen verklaring.** WAT Cannae doet is beschreven, niet WAAROM het werkt. Fundamentele vraag: informatievoorsprong (betere odds-inschatting) of structureel marktdefect (PM-prijzen systematisch scheef)?

3. **De verliezers ontbreken.** De analyse focust op winnaars. De sleutel zit in de verliezers: wanneer, welke leagues, welke sizing? Q1-Q4 bets met 57% WR en 4-8% ROI zijn nauwelijks boven break-even na fees. We kopiëren nu ook die marginale bets.

**Concrete gaps:** hauptbet-niveau P&L, verliesanalyse, timing-ROI relatie, slippage-meting.

---

### Adviseur 2 — De Expansionist
**Kernpunt:** De theorie is solide maar heeft drie blinde vlekken.

1. **Counter-factual ontbreekt.** Wat levert een naïeve Win NO + Draw YES strategie op zonder Cannae's timing/sizing? Simuleer blindelings op alle wedstrijden, flat sizing, entry T-6 dagen. Verschil met Cannae's ROI = zijn echte alpha. Zonder baseline weet je niet of je skill kopieert of structurele edge.

2. **Calibration curve ontbreekt.** Gebruik avgPrice vs settlement (1 of 0) per league. Dit geeft Cannae's calibration — koopt hij werkelijk underpriced tokens? Als avgPrice voor winners 0.55 is → 45 cent edge per dollar. Als 0.85 → dunne edge, alles hangt af van volume.

3. **League-specifieke timing ontbreekt.** 5.7 dagen mediaan is een gemiddelde. EPL vs CL kan totaal anders zijn. Split timing per league. Eén vaste timing = geld laten liggen.

**Actie:** Baseline simulatie + calibration + timing split. Kost 0 API credits, alleen bestaande 19.600 posities dataset.

---

### Adviseur 3 — De Buitenstaander
**Kernpunt:** Er zijn fundamentele vragen die iedereen van buiten direct zou stellen.

1. **21.7% op $25.2M klinkt te mooi.** Dat is $5.5M winst. Zonder tijdframe (6 maanden vs 3 jaar) zegt het getal niets. Fee-structuur en slippage bij die volumes?

2. **Survivorship bias correctie is halfbakken.** "Waarschijnlijk alleen winnende kanten" → correctie naar 65% is een gok. 65% winrate op 50-cent tokens is breakeven na fees.

3. **"Meer inzet = hogere winkans" is basale bankroll management.** Geen bewijs van skill, maar van: hij zet meer in wanneer de edge groter is. De vraag is of de EDGE voorspelbaar is.

4. **$3K kopiëren van $3.56M is een ander spel.** Infra, datafeed-snelheid, odds-API — paper trading valideert weinig, het echte risico zit in executie.

**Kernvraag:** Wat is het verlies op posities die je NIET ziet?

---

### Adviseur 4 — De Contrariaan
**Kernpunt:** Survivorship bias in de conviction-correlatie is een fatale fout.

**Circulaire redenering:** "Grote bets winnen 70.2% vs 57%" — Cannae zet grote bedragen op goedkope tokens (30-50c). Goedkope tokens hebben inherent hogere ROI als ze winnen ($1 op 40c = 150% vs $1 op 70c = 43%). De +0.107 correlatie verklaart <2% variantie. Je meet niet skill maar de wiskunde van goedkope tokens.

**Win NO ROI opgeblazen:** 40.6% op $6.6M. Win NO wint bij 2/3 uitkomsten, maar de prijs reflecteert dat al. PM-marktmakers zijn niet dom. Impliceert dat PM systematisch team-winkansen overschat. Is die edge dalend over tijd?

**Concrete gaps:**
- ROI tijdserie (stabiel of dalend?)
- Draw YES op slechts $0.8M = ~1.100 posities, statistische significantie?
- Pinnacle-vergelijking op 12 posities is anekdotisch (100+ nodig)
- Taker fee 0.44% vs maker 0% + rebate
- Liquiditeit bij exit: $3K+ posities kunnen niet snel uit

---

### Adviseur 5 — De Uitvoerder
**Kernpunt:** Stop met backward-looking. Je hebt één ding nodig: bewijs dat mispricing NU bestaat.

**Stap 1 — Live mispricing (2-3 uur):** Python script dat voor alle EPL/BuLi/CL wedstrijden PM-prijs vs Odds API haalt. Geen mispricing → stop hier.

**Stap 2 — Forward-test (1-2 dagen):** Log-only mode: welke wedstrijden mispricing tonen, welke kant, hoeveel.

**Stap 3 — Paper trade (1 dag):** Drempel bepalen, /opt/bottie-test/ met log-only configureren.

**Wat je NIET moet doen:** De 5 openstaande vragen (historische odds, post-entry prijsbeweging) beantwoorden. Die data heb je niet. Forward-testen is de enige optie.

---

## Peer Reviews (anoniem, letters geshuffled)

### Review 1
- **Sterkste:** A — enige die het mathematisch onderbouwt (circulaire correlatie, <2% variantie, fee-asymmetrie)
- **Grootste blinde vlek:** B — stuurt naar forward-testen terwijl historische theorie niet gefalsifieerd is
- **Gemist:** Fee-asymmetrie als structureel voordeel. Cannae is maker (0% + rebate), wij taker (0.44%). Maakt strategie mogelijk onkopieerbaar op kleine schaal.

### Review 2
- **Sterkste:** D — counterfactual baseline is de enige test die de kernvraag beantwoordt
- **Grootste blinde vlek:** B — behandelt het als mispricing-arbitrage, maar de vraag is of Cannae's patroon reproduceerbaar is
- **Gemist:** Wat deed Cannae op wedstrijden die hij NIET inzette? Die pass-rate is de echte selectiviteitstest.

### Review 3
- **Sterkste:** E — observatie ≠ strategie, verliesanalyse ontbreekt totaal
- **Grootste blinde vlek:** B — forward-testen op een onbewezen thesis is de verkeerde strategie testen
- **Gemist:** Survivorship bias op Cannae zelf. Hij is één wallet die je analyseert OMDAT hij succesvol is. Hoeveel wallets faalden met dezelfde strategie?

### Review 4
- **Sterkste:** C — $25.2M volume met $3K startkapitaal = geen netto kasstroom-analyse
- **Grootste blinde vlek:** B — vervuild signaal forward-testen produceert ruis, geen validatie
- **Gemist:** Market impact en adverse selection. Cannae beweegt de markt. Copybot koopt tegen door-Cannae-bewogen prijzen — edge bestaat structureel niet voor een volger.

### Review 5
- **Sterkste:** D — naïeve baseline is de enige falsificeerbare test
- **Grootste blinde vlek:** B — actie zonder de centrale vraag te beantwoorden
- **Gemist:** Cannae's edge zit mogelijk in timing van liquiditeit: vroeg kopen bij brede spread. Als hij maker-side koopt, is zijn fee negatief (rebate). Verklaart 21.7% ROI zonder informatieve edge.

---

## Chairman Synthese

### Consensus
Alle adviseurs en reviewers zijn het eens over vier dingen:

1. **De data is onbetrouwbaar als basis voor sizing-beslissingen.** Survivorship bias, circulaire conviction-correlatie (<2% variantie), en onbekende verliezen maken 21.7% ROI een bovengrens.
2. **Win NO + Draw YES is een observatie, geen verklaring.** Het patroon is bewezen maar het mechanisme (WAAROM het werkt) niet.
3. **Forward-testen zonder baseline is zinloos.** Naïeve counterfactual is noodzakelijk.
4. **De verliesanalyse ontbreekt volledig.** Alle focus lag op winnaars.

### Kernconflict
De Uitvoerder zegt: "stop analyseren, start meten."
De rest zegt: "je begrijpt de data nog niet."

**De rest heeft gelijk, maar de Uitvoerder heeft de juiste sequentie.** Historische data kan niet verder opgeschoond worden (survivorship bias is structureel in de API). Maar blindelings forward-testen valideert niets. **Oplossing: beide parallel.**

### Blinde Vlek — Wat de peer reviewers zagen

Drie kritische inzichten die geen adviseur benoemde:

1. **Fee-asymmetrie maakt kopiëren structureel duurder.** Cannae is market maker (0% fee + rebate). Wij zijn taker (0.44%). Op identieke posities is onze netto-ROI 1-2% lager. Bij een realistische hauptbet ROI van 5-8% is dat dodelijk.

2. **Market impact / adverse selection.** Cannae's $25.2M volume beweegt de markt. Een copybot koopt tegen door-Cannae-bewogen prijzen. De edge is deels verdampt op het moment dat wij instappen.

3. **Selectiviteits-bias op Cannae zelf.** We analyseren hem OMDAT hij succesvol is. Hoeveel wallets draaiden dezelfde strategie en zijn gestopt? Zonder die base rate is elke validatie op drijfzand.

4. **Timing als liquiditeitsedge.** Cannae koopt vroeg bij brede spread (maker). Zijn effectieve fee kan negatief zijn door rebates. Dit verklaart hoge ROI zonder informatieve edge — hij verdient aan de spread, niet aan de uitkomst.

### Verdict
**De theorie is een solide hypothese, maar nog geen bewezen strategie.**

De claim "Cannae exploiteert PM's favoriet-bias via Win NO + Draw YES" is consistent met de data maar niet gefalsifieerd. De drie grootste risico's voor onze bot:

1. **De edge kan structureel zijn** (2/3 uitkomsten) → kopieerbaar → counterfactual bewijst dit
2. **De edge kan in timing zitten** (maker-side, spread-capture) → niet kopieerbaar als taker
3. **De edge kan in selectiviteit zitten** (Cannae slaat wedstrijden over) → niet zichtbaar in onze data

### Eerste Stappen (PARALLEL, vandaag)

**A. Counterfactual baseline (2 uur, 0 credits):**
Simuleer naïef Win NO + Draw YES op alle EPL/BuLi/CL wedstrijden uit de dataset, flat sizing, entry op avgPrice. Als baseline >15% ROI → structurele edge, kopieerbaar. Als <5% → Cannae's alpha zit in selectiviteit/timing.

**B. Live mispricing scan (1 uur, ~10 Odds API credits):**
PM prijs vs bookmaker implied prob voor alle komende EPL/BuLi/CL wedstrijden. Als systematische mispricing >5% → theorie leeft. Als <2% → markt is efficiënt.

**C. Verliesanalyse (1 uur, 0 credits):**
Kenmerken van verliezende hauptbets: league, timing, sizing, avgPrice. Zoek patronen die we kunnen filteren.

Na deze drie analyses: go/no-go beslissing voor paper trading op /opt/bottie-test/.

---

*Metadata: 2026-04-02, 5 adviseurs + 5 peer reviewers, chairman: Claude Sonnet 4.6*
