# /eod — Einde van de Dag Samenvatting

Maakt een compacte dagafsluiting die geoptimaliseerd is voor de `/morning` briefing van morgen.

---

## Gebruik

```
/eod
```

---

## Uitvoering

### Stap 1: Verzamel Data (PARALLEL)

```
Read: data/sessions/YYYY-MM-DD-session.md (alle saves van vandaag)
```

```bash
# Commits van vandaag
git -C <PROJECT_ROOT> log --oneline --since="midnight"

# Huidige branch en status
git -C <PROJECT_ROOT> branch --show-current
git -C <PROJECT_ROOT> status --short
```

**Verplicht:**
- Actieve/gesloten tickets ophalen uit Linear (team: RustedPoly, project: Bottie)
- Tickets bijwerken met resultaten van vandaag
- Status van draaiende services checken

### Stap 2: Schrijf EOD + Session

Schrijf naar **twee bestanden**:
1. `data/sessions/YYYY-MM-DD-eod.md` — compact (max 200 woorden, voor /morning)
2. `data/sessions/YYYY-MM-DD-session.md` — identieke inhoud (fallback voor /morning)

**Template (max 200 woorden):**

```markdown
# EOD Summary — YYYY-MM-DD

## Afgerond vandaag
- [Taak/ticket 1: korte beschrijving]
- [Taak/ticket 2: korte beschrijving]

## In progress
- [Taak die nog loopt + huidige stand]

## Blockers / Issues
- [Problemen die aandacht nodig hebben]

## Context voor morgen
1. **Eerste prioriteit:** [wat moet er als eerste gebeuren]
2. **Branch state:** [branch, uncommitted changes]
3. **Volgende stappen:** [concrete acties]

---
**Commits vandaag:** X | **Tickets gesloten:** Y | **Linear:** [link naar project board]
```

### Stap 3: Linear Bijwerken

**Verplicht bij elke /eod:**
- Update tickets in RustedPoly/Bottie project met resultaten
- Sluit afgeronde tickets (state: Done)
- Voeg comments toe aan in-progress tickets met huidige stand
- Maak nieuwe tickets voor ontdekte issues

### Stap 3: Optioneel Detail

Als er veel is gebeurd, maak een apart `YYYY-MM-DD-eod-detailed.md` voor uitgebreide referentie. Dit bestand wordt NIET geladen in `/morning` context — alleen het compacte bestand.

---

## Regels

| DO | DON'T |
|----|-------|
| Max 200 woorden in het compacte bestand | Essays schrijven |
| "Context voor morgen" is het BELANGRIJKSTE onderdeel | Focussen op wat fout ging |
| Concrete volgende stappen | Vage plannen |
| Meld blockers expliciet | Problemen verstoppen |
| Meld branch state + uncommitted changes | Aannemen dat alles gecommit is |

---

## Relatie met Andere Skills

```
/save (tijdens de dag) → /eod (einde dag) → /morning (volgende ochtend)
     ↓                                            ↓
  /compact → /continue                    Eerste actie uitvoeren
```
