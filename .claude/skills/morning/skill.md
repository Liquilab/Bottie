# /morning — Ochtend Kickoff

Laadt overnight state, checkt optioneel productie-systemen, en presenteert een actionable ochtend briefing.

**Draai dit EERST bij het starten van een nieuwe sessie in de ochtend.**

---

## Gebruik

```
/morning
```

---

## Uitvoering

### Stap 1: Laad Context (PARALLEL)

**A. Laatste session save:**
```
Read: data/sessions/YYYY-MM-DD-session.md (vandaag of gisteren)
```
Focus op de LAATSTE "Session Save" sectie.

**B. Gisteren's EOD samenvatting:**
```
Read: data/sessions/YYYY-MM-DD-eod.md (datum van gisteren)
```
Lees alleen: "Completed", "In progress", "Blockers", "Context for tomorrow".

**C. Git state:**
```bash
git -C <PROJECT_ROOT> log --oneline -5
git -C <PROJECT_ROOT> status --short
git -C <PROJECT_ROOT> branch --show-current
```

**D. Issue tracking** (optioneel):
- Haal actieve tickets op

**E. Production checks** (optioneel — alleen als het project servers/services heeft):
- Check draaiende processen
- Bekijk recente logs (laatste 30 min)

### Stap 2: Presenteer Briefing

Houd het onder 15 regels:

```
Ochtend Briefing — YYYY-MM-DD HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gisteren:
  - [1-2 regels: wat is afgerond]
  - [Eventuele kritieke fixes]

Production: [alleen als relevant]
  [status per service: draaiend/gestopt]

Actief Werk:
  - [Ticket/taak 1]
  - [Ticket/taak 2]

Checks:
  - [Belangrijkste ding om te verifiëren op basis van gisteren]

Volgende Actie: [directe volgende stap]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Stap 3: Voer Eerste Actie Uit

Prioriteit:
1. Als productie down is → onderzoek en rapporteer
2. Als session zegt "verifieer X" → voer verificatie uit
3. Als er een lopend experiment/test is → check resultaten
4. Als er geen urgente actie is → presenteer status en VRAAG wat te doen

---

## Regels

| DO | DON'T |
|----|-------|
| Lees alleen de LAATSTE session save | Alle saves doorlezen |
| Focus op "Context for tomorrow" uit EOD | Hele EOD narratief lezen |
| Check productie EERST | Aannemen dat alles draait |
| Presenteer 10-15 regel briefing | Multi-paragraaf status schrijven |
| Handel op duidelijke next steps | Vragen "zal ik doorgaan?" |

---

## Fallback

Als geen session file of EOD gevonden:
1. Check MEMORY.md "Current State" sectie
2. Check git log (laatste 24u)
3. Presenteer wat beschikbaar is
4. Vraag: "Geen session data. Wat is de prioriteit vandaag?"
