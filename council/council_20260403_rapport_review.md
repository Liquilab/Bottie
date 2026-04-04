# Council: Review Analyserapport 2 April 2026
**Datum:** 2026-04-03
**Vraag:** Zijn de conclusies van het analyserapport correct (+$266 door code changes, sizing als probleem), en wat is de optimale sizing strategie?

---

## Adviseurs

### 1. De Contrariaan
Het rapport is circulair: je beoordeelt code wijzigingen op de dag van deploy. Dat is geen analyse, dat is een narrative die terugwerkt vanuit de uitkomst.

- "+$266 door code changes" is niet bewezen — 1 dag, 2 games, geen baseline
- Santos CONVICTION 17% bankroll met positief resultaat = survivorship bias
- 3 deploys maakt de data oninterpreteerbaar — experimentele conditie niet geïsoleerd
- De 10% cap is correct, maar de rest bouwt een vals fundament van zekerheid

### 2. De Eerste Principes Denker
Het echte probleem is niet sizing. Je optimaliseert de verkeerde variabele.

- Cannae's edge werkt op $200K door wet van grote aantallen. Op $3.400 is één game existentieel
- Cannae's optimale stake ≠ jouw optimale stake, zelfs niet proportioneel
- Je kopieert het signaal met vertraging en slechtere fill
- Kernvraag: welke SUBSET van Cannae's bets heeft de hoogste edge voor jou specifiek?

### 3. De Expansionist
De upside die gemist wordt: Cannae's sizing-ratio als signaalsterkte gebruiken.

- Als Cannae 3.75% van zijn bankroll zet, is de correcte proportionele inzet $127, niet 12-17%
- Brisbane Draw @ 27ct → $1 = 270% return — filter op lage-odds (<35ct) en oversize die
- Groei van $3.400 → $10K niet door minder risico maar door de beste bets zwaarder te wegen

### 4. De Buitenstaander
"Kopiëren" is een aanname, niet een bewezen strategie.

- De fundamentele vraag — "is dit iemand die structureel wint?" — wordt nergens beantwoord
- 3 software-versies op 1 dag is alarmerend
- De counterfactual is fictie (berekening, geen meting)
- $3.400 vs $200K is fundamenteel anders (andere fills, andere marktimpact)
- "Max 10%" heeft geen statistische onderbouwing

### 5. De Uitvoerder
Actieplan:
1. **Deploy max 10% event cap** (30 min) — brandverzekering
2. **Fix Odds API quota** (15 min)
3. **Code freeze tot vrijdag** — laat de cap 5 dagen draaien

---

## Peer Review (geanonimiseerd)

### Sterkste adviseur
**4/5 reviewers:** Adviseur D (Buitenstaander) — stelt de juiste fundamentele vraag die iedereen overslaat.
**1/5 reviewers:** Adviseur B (Eerste Principes) — raakt de kern van de variantie-mismatch.

### Grootste blinde vlek
**5/5 reviewers:** Adviseur E (Uitvoerder) — pragmatisch zonder fundament. Handelen op ongeldige data is slechter dan wachten.

### Wat ALLE adviseurs misten
De reviewers identificeerden twee cruciale blinde vlekken:

1. **Fill-kwaliteit / kopieerlatentie** (4/5 reviewers)
   Cannae beweegt de markt. Wij kopen NA hem tegen slechtere odds. Zonder fill-lag analyse weten we niet of onze edge überhaupt positief is. De +$292 kan een artefact zijn van gunstige markttiming, niet van de strategie.

2. **Correlatie-structuur** (1/5 reviewers)
   14 "games" zijn niet 14 onafhankelijke bets — het zijn gecorreleerde posities (meerdere legs per event). Dat verandert het risicoprofiel fundamenteel en maakt per-bet caps zinloos zonder correlatie-analyse.

---

## Chairman Synthese

### Het rapport is operationeel correct maar strategisch onvolledig
- Per-game analyse: ✅ counterfactuals juist berekend
- "+$266 door code changes": ❌ niet bewezen (1 dag, 2 games)
- Sizing-diagnose: ✅ correct geïdentificeerd
- 10% cap: ⚠️ juiste richting, maar zonder statistische onderbouwing
- Fundamentele validatie van copy-trading: ❌ onbeantwoord

### Verdict
Deploy de 10% event cap NU. Freeze code tot vrijdag. Analyseer dan met 50+ games. De fundamentele vragen (fill-kwaliteit, subset-selectie, Cannae-validatie) beantwoord je niet met 14 games op 1 dag.

### Eerste stap
Max 10% per event cap deployen — vandaag, 30 minuten.

---

## Metadata
- Agents: 10 (5 adviseurs + 5 peer reviewers)
- Model: Sonnet
- Datum: 2026-04-03
