# /learn — Sessie Lessen Extraheren

Analyseert de huidige sessie en slaat herbruikbare patronen op in memory files.

---

## Gebruik

```
/learn              # Analyseer huidige sessie
/learn [onderwerp]  # Extraheer lessen over specifiek onderwerp
```

---

## Wat Vastleggen

**WEL vastleggen:**
- Fouten die meerdere pogingen kostten om op te lossen
- Correcties door de user ("nee, het moet zo...")
- Workarounds voor tools/API's/libraries
- Project-specifieke conventies die nergens gedocumenteerd staan
- Debugging inzichten die tijd hebben bespaard

**NIET vastleggen:**
- Eenmalige typo's of simpele fixes
- Tijdelijke problemen (API was even down)
- Dingen die al in CLAUDE.md staan
- Sessie-specifieke context (huidige taak details)
- Speculatieve conclusies uit één datapunt

---

## Uitvoering

### Stap 1: Analyseer Sessie

Scan de conversatie voor:
- Momenten waar de user corrigeerde
- Fouten die >2 pogingen kostten
- Ongedocumenteerde gedragingen van tools/API's
- Patronen die in meerdere sessies terugkomen

### Stap 2: Categoriseer

Per gevonden patroon, vraag de user:

```
Gevonden: [korte beschrijving]
Type: ERROR (fout om te vermijden) / LEARNING (bewezen patroon) / SKIP
```

### Stap 3: Schrijf naar Memory

**Voor ERRORS** — schrijf naar MEMORY.md of een topic file:

```markdown
### ERROR: [korte titel] — YYYY-MM-DD
**Context:** [wanneer doet dit zich voor]
**Fout:** [wat ging er mis en waarom]
**Oplossing:** [hoe te voorkomen]
**Confidence:** High/Medium
```

**Voor LEARNINGS:**

```markdown
### LEARNING: [korte titel] — YYYY-MM-DD
**Context:** [wanneer is dit relevant]
**Patroon:** [wat werkt en waarom]
**Oplossing:** [concrete actie of code]
**Confidence:** High/Medium
```

### Stap 4: Organiseer

- Als MEMORY.md te lang wordt (>150 regels): verplaats details naar topic files
- Link vanuit MEMORY.md naar topic files: `| Onderwerp | pad/naar/file.md |`
- Verwijder of update learnings die verouderd blijken

---

## Regels

| DO | DON'T |
|----|-------|
| Alleen bewezen patronen vastleggen | Speculatie opslaan |
| Concrete, actionable beschrijvingen | Vage "let op X" notities |
| User laten kiezen ERROR/LEARNING/SKIP | Automatisch alles opslaan |
| Bestaande entries updaten als ze verouderen | Duplicaten aanmaken |
| Compact houden (MEMORY.md < 200 regels) | Eindeloos laten groeien |

---

## Memory Structuur

```
.claude/
  projects/<project>/
    memory/
      MEMORY.md          ← Altijd geladen (compact, <200 regels)
      debugging.md       ← Topic file: debugging patronen
      api-quirks.md      ← Topic file: API eigenaardigheden
      patterns.md        ← Topic file: code patronen
```

MEMORY.md bevat:
- Huidige project state (kort)
- Index naar topic files
- Belangrijkste 5-10 regels die niet in CLAUDE.md staan
