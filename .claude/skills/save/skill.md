# /save — Sessie State Opslaan

Slaat de huidige sessie-context op naar disk zodat deze hersteld kan worden na `/compact` of `/clear`.

**Gebruik:** `/save` of `/save [notitie]`

---

## Wanneer Gebruiken

- VOOR `/compact` (verplicht — anders gaat context verloren)
- Na een significante beslissing of milestone
- Voordat je stopt met werken
- Na complexe debugging sessies

---

## Uitvoering

### Stap 1: Verzamel State (PARALLEL)

Draai alle commands parallel:

```bash
# Git state
git -C <PROJECT_ROOT> log --oneline -5
git -C <PROJECT_ROOT> status --short
git -C <PROJECT_ROOT> branch --show-current
```

Optioneel (als het project issue tracking gebruikt):
- Haal actieve tickets op

Optioneel (als het project servers/services heeft):
- Check draaiende processen

### Stap 2: Schrijf Session File

Schrijf naar `data/sessions/YYYY-MM-DD-session.md`.

Als het bestand al bestaat, VERVANG alleen de laatste "Session Save" sectie (bewaar eerdere saves niet — bespaart context).

**Template:**

```markdown
# Session — YYYY-MM-DD

## Session Save — HH:MM [TIJDZONE]

### Waar We Mee Bezig Waren
[1-3 zinnen: huidige taak, context, stand van zaken]

### Actief Werk
- **Branch**: [huidige branch]
- **Tickets**: [open tickets met status]
- **Uncommitted changes**: [ja/nee + beschrijving]
- **Laatste commit**: [hash + beschrijving]

### Belangrijke Beslissingen Deze Sessie
- [Beslissing 1 + rationale]
- [Beslissing 2 + rationale]

### Volgende Stappen
1. [Eerste prioriteit — wat moet er als eerste gebeuren]
2. [Tweede prioriteit]
3. [Derde prioriteit]

### User Notitie
[Optioneel: door user meegegeven context]
```

### Stap 3: Bevestig

Toon:
```
Saved: data/sessions/YYYY-MM-DD-session.md (HH:MM)
Next: /compact → /continue
```

---

## Regels

| DO | DON'T |
|----|-------|
| Bewaar alleen de LAATSTE save per dag | Meerdere saves stapelen |
| Noteer concrete next steps | Vage "we gaan verder" notities |
| Bewaar beslissingen + rationale | Hele conversatie samenvatten |
| Meld uncommitted changes | Aannemen dat alles gecommit is |

---

## Afhankelijkheden

- Directory `data/sessions/` moet bestaan
- MEMORY.md in `.claude/` project memory voor persistente kennis
