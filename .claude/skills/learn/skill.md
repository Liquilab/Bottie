# /learn — Sessie Lessen Extraheren + Skill Improvement

Analyseert de huidige sessie, slaat herbruikbare patronen op in memory files, en **amendeert skills die faalden**.

Dit is de inspect+amend stap van de self-improving loop:
```
observe (tijdens sessie) → inspect (patronen vinden) → amend (skills updaten) → evaluate (PnL/correcties meten)
```

---

## Gebruik

```
/learn              # Analyseer huidige sessie
/learn [onderwerp]  # Extraheer lessen over specifiek onderwerp
/learn skills       # Focus op skill failures en amendments
```

---

## Wat Vastleggen

**WEL vastleggen:**
- Fouten die meerdere pogingen kostten om op te lossen
- Correcties door de user ("nee, het moet zo...")
- Workarounds voor tools/API's/libraries
- Project-specifieke conventies die nergens gedocumenteerd staan
- **Skill failures: welke skill, welke stap, welk niveau (routing/instructie/tool)**

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
- **Skills die faalden of suboptimaal presteerden**

### Stap 2: Diagnose per Failure (3 niveaus)

Bij elke skill failure, bepaal het niveau:

| Niveau | Vraag | Voorbeeld | Fix locatie |
|--------|-------|-----------|-------------|
| **Routing** | Werd de juiste skill aangeroepen? | /analyse i.p.v. /verify | Skill descriptions/triggers |
| **Instructie** | Welke stap in de skill klopte niet? | Stap 1 zei niet "altijd VPS data" | SKILL.md stap X |
| **Tool call** | Werkt de onderliggende API/command nog? | SSH alias bestaat niet, API changed | Tool/infra fix |

### Stap 3: Categoriseer

Per gevonden patroon, vraag de user:

```
Gevonden: [korte beschrijving]
Type: ERROR / LEARNING / SKILL_AMENDMENT / SKIP
Niveau: Routing / Instructie / Tool call (alleen bij SKILL_AMENDMENT)
Skill: [welke skill] (alleen bij SKILL_AMENDMENT)
```

### Stap 4: Schrijf naar Memory

**Voor ERRORS** — schrijf naar memory file:

```markdown
---
name: [titel]
description: [one-liner]
type: feedback/error
---
[beschrijving]
**Why:** [context]
**How to apply:** [concrete actie]
```

**Voor LEARNINGS** — schrijf naar memory file (zelfde format, type: learning)

### Stap 5: Amendeer Skills (NIEUW)

**Voor SKILL_AMENDMENTS** — update de SKILL.md direct:

1. **Known Failures** sectie: voeg failure toe met:
   - Niveau (routing/instructie/tool)
   - Wat ging mis
   - Impact (PnL of operationeel)
   - Root cause
   - Fix of status

2. **Changelog** sectie: voeg entry toe met:
   - Datum
   - Amendment type (tighten trigger / add condition / reorder steps / change format)
   - Wijziging
   - Reden (link naar failure)

3. **Skill instructies zelf**: pas de stap aan die faalde

Amendment types:
- **Tighten trigger** → skill activeert te breed of te vaak
- **Add condition** → ontbrekende guard/check
- **Reorder steps** → verkeerde volgorde veroorzaakt cascade failure
- **Change format** → output is misleidend of onvolledig

### Stap 6: Organiseer

- Als MEMORY.md te lang wordt (>150 regels): verplaats details naar topic files
- Link vanuit MEMORY.md naar topic files
- Verwijder of update learnings die verouderd blijken

---

## Regels

| DO | DON'T |
|----|-------|
| Alleen bewezen patronen vastleggen | Speculatie opslaan |
| Concrete, actionable beschrijvingen | Vage "let op X" notities |
| User laten kiezen ERROR/LEARNING/SKILL_AMENDMENT/SKIP | Automatisch alles opslaan |
| Bestaande entries updaten als ze verouderen | Duplicaten aanmaken |
| Compact houden (MEMORY.md < 200 regels) | Eindeloos laten groeien |
| Bij skill amendment: update Known Failures + Changelog + instructie | Alleen memory schrijven |
| Diagnose op 3 niveaus (routing/instructie/tool) | Skill als geheel pass/fail markeren |
| Link failures aan PnL impact waar mogelijk | Abstracte "dit ging fout" notities |

---

## Memory Structuur

```
.claude/
  projects/<project>/
    memory/
      MEMORY.md          ← Altijd geladen (compact, <200 regels)
      [topic files]      ← Detail per onderwerp

skills/
  [skill]/
    skill.md             ← Bevat ## Known Failures en ## Changelog
```

MEMORY.md bevat:
- Index naar memory files
- Belangrijkste regels die niet in CLAUDE.md staan

SKILL.md bevat (nieuw):
- Known Failures: failure history met niveau, impact, root cause
- Changelog: alle amendments met type en reden

---

## Known Failures

### F1: Alleen memories, geen skill amendments (2026-03-15)
- **Niveau:** Instructie
- **Wat:** /learn schreef lessen naar memory files maar updatte de skills zelf niet. Resultaat: dezelfde fouten bleven in de skill instructies staan.
- **Impact:** /analyse bleef falen op dezelfde stap (lokale data) tot de user het handmatig fixte
- **Fix:** Stap 5 (Amendeer Skills) toegevoegd. /learn update nu SKILL.md direct.

---

## Changelog

| Datum | Type | Wijziging | Reden |
|-------|------|-----------|-------|
| 2026-03-15 | Add condition | Stap 2: diagnose op 3 niveaus (routing/instructie/tool) | Skill failures werden niet gediagnosticeerd |
| 2026-03-15 | Add condition | Stap 5: skill amendments (Known Failures + Changelog + instructie fix) | Skills werden niet geüpdatet na failures |
| 2026-03-15 | Add condition | SKILL_AMENDMENT als nieuw type naast ERROR/LEARNING | Onderscheid tussen memory en skill fix |
