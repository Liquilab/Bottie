# /brainstorm — Gestructureerde Brainstorm

Faciliteert een gestructureerde brainstorm sessie met Design Thinking methodologie, kritische evaluatie en actionable output.

---

## Gebruik

```
/brainstorm                          # Nieuwe brainstorm sessie
/brainstorm capture "idee tekst"     # Snel idee vastleggen
/brainstorm list                     # Bekijk opgeslagen ideeën
/brainstorm evaluate IDEA-XXX        # Evalueer een specifiek idee
```

---

## Quick Capture

Bij `/brainstorm capture "..."`:
1. Sla op in `docs/ideas/BACKLOG.md`
2. Ken een ID toe: IDEA-001, IDEA-002, etc.
3. Timestamp + korte beschrijving
4. Bevestig met één regel

```markdown
## IDEA-XXX — YYYY-MM-DD
**Idee:** [tekst]
**Status:** captured
**Bron:** [sessie context]
```

---

## Volledige Brainstorm Sessie

### Fase 1: Doel & Scope (2 min)

Stel deze vragen:
1. **Wat willen we bereiken?** (doel in 1 zin)
2. **Voor wie?** (doelgroep)
3. **Welke beperkingen?** (budget, tijd, technisch)
4. **Hoe meten we succes?** (1-2 concrete metrics)

### Fase 2: Empathize (5 min)

Verplaats je in de doelgroep:
- **Pains:** Welke frustraties ervaren ze nu?
- **Gains:** Wat zouden ze het liefst willen?
- **Jobs-to-be-done:** Welke taak proberen ze uit te voeren?

Presenteer als tabel:

| Pains | Gains | Jobs |
|-------|-------|------|
| ... | ... | ... |

### Fase 3: Define (3 min)

Formuleer het probleem:

```
[Doelgroep] heeft moeite met [probleem] wanneer ze [context].
Dit kost hen [pijn/verlies]. Een goede oplossing zou [metric] verbeteren met [target].
```

### Fase 4: Ideate (10 min)

Genereer ideeën met 4 technieken (minimaal 3 per techniek):

**A. Klassiek:** Directe oplossingen
**B. Reverse:** "Hoe maken we het probleem ERGER?" → draai elk punt om
**C. Role-storming:** Hoe zou [expert/concurrent/gebruiker] dit oplossen?
**D. Constraints:** Wat als we maar 1 dag / €100 / geen code hadden?

Presenteer ALLE ideeën zonder filter. Kwantiteit > kwaliteit in deze fase.

### Fase 5: Convergeer & Evalueer (5 min)

1. **Cluster** ideeën in thema's
2. **Shortlist** top 5 op basis van: impact × haalbaarheid
3. **Kritische noten** per idee:
   - Wat kan fout gaan?
   - Welke aanname is het riskantst?
   - Wat weten we NIET?

Presenteer als vergelijkingsmatrix:

| Idee | Impact (1-5) | Haalbaarheid (1-5) | Risico | Score |
|------|-------------|-------------------|--------|-------|
| ... | ... | ... | ... | ... |

### Fase 6: Expert Panel (5 min)

3 perspectieven op de top 3 ideeën:

**Product Lead:** Is dit waardevol voor de gebruiker? Past het in de roadmap?
**Technisch Lead:** Is dit bouwbaar? Hoeveel effort? Welke risico's?
**Criticus:** Waarom werkt dit NIET? Welke aanname is fout?

### Fase 7: Actieplan

Output template:

```markdown
## Brainstorm Resultaat — YYYY-MM-DD

### Context & Doel
[1-2 zinnen]

### Gekozen Oplossing(en)
1. [Oplossing + rationale]
2. [Oplossing + rationale]

### Implementatie Plan
| Stap | Actie | Eigenaar | Deadline |
|------|-------|----------|----------|
| 1 | ... | ... | ... |

### Validatie
- [ ] [Eerste test/experiment om hypothese te checken]
- [ ] [Tweede validatiestap]

### Risico's & Open Vragen
- [Risico 1 + mitigatie]
- [Open vraag die beantwoord moet worden]
```

---

## Evaluatie van Bestaand Idee

Bij `/brainstorm evaluate IDEA-XXX`:

1. Lees het idee uit `docs/ideas/BACKLOG.md`
2. Doorloop Fase 5 (Convergeer) en Fase 6 (Expert Panel) voor dit specifieke idee
3. Geef een GO / NO-GO / NEEDS-MORE-INFO verdict
4. Update de status in BACKLOG.md

---

## Regels

| DO | DON'T |
|----|-------|
| Alle interactie in het Nederlands | Switchen naar Engels |
| Kwantiteit in ideate fase | Te vroeg filteren |
| Kritische noten bij ELKE shortlist | Alleen positieve punten noemen |
| Concrete metrics definiëren | Vage "verbetering" doelen |
| Actieplan met eigenaar + deadline | Ideeën zonder follow-up |
| Risico's en open vragen benoemen | Doen alsof alles zeker is |

---

## Afhankelijkheden

- Directory `docs/ideas/` moet bestaan voor quick capture
- `BACKLOG.md` wordt automatisch aangemaakt bij eerste capture
