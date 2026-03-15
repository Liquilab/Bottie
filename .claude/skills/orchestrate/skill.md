---
name: orchestrate
description: "Coördinatie van alle Bottie agents: morning → analyse → experiment → deploy → verify"
---

# /orchestrate — Agents Orchestrator

De dirigent. Coördineert de andere skills in de juiste volgorde. Houdt overzicht over lopende experimenten, openstaande acties, en voortgang naar $10K.

**Gebaseerd op:** agency-agents/agents-orchestrator + executive-summary-generator (aangepast voor Bottie)

**Status: GEPARKEERD** — Pas relevant bij $500+ bankroll. Gebruik /morning + /check tot die tijd.

---

## Gebruik

```
/orchestrate            — Overzicht: wat loopt er, wat moet er gebeuren
/orchestrate morning    — Ochtend pipeline (vervangt /morning met agent coördinatie)
/orchestrate deploy     — Deploy pipeline: build → deploy → verify
/orchestrate change     — Wijziging pipeline: experiment → deploy → verify → track
```

---

## Overzicht Mode

Bij `/orchestrate`:

### 1. Lees state

```bash
# Lopende experimenten
ls /Users/koen/Projects\ Bottie/data/experiments/*.json 2>/dev/null

# Laatste session
ls /Users/koen/Projects\ Bottie/data/sessions/*-session.md | tail -1

# Git status
git -C "/Users/koen/Projects/ Bottie" log --oneline -3
git -C "/Users/koen/Projects/ Bottie" status --short
```

### 2. Presenteer SCQA briefing

```markdown
# Bottie Orchestrator — [datum]

## Situatie
- Bankroll: $XX (doel: $10K)
- Services: [status]
- Open posities: XX
- Resolved trades: XX

## Complicaties
- [Wat gaat niet goed / wat heeft aandacht nodig]
- [Lopende experimenten die gecheckt moeten worden]
- [Ongeresolvede bevindingen uit vorige sessie]

## Vraag
Wat is de hoogste-impact actie die we nu kunnen nemen?

## Antwoord
[Concrete volgende stap]

## Lopende Experimenten
| Naam | Start | Trades | Status |
|------|-------|--------|--------|
| [naam] | [datum] | XX/XX min | [RUNNING/READY] |

## Pipeline Status
| Stap | Status |
|------|--------|
| Laatste deploy | [datum] |
| Laatste verify | [PASS/FAIL/NEEDS WORK] |
| Laatste analyse | [datum] |
```

---

## Morning Pipeline

Bij `/orchestrate morning`:

```
Stap 1: /verify              → Services draaien? Bot tradeert?
Stap 2: /bankroll             → Bankroll status (vraag user)
Stap 3: /experiment status    → Lopende experimenten checken
Stap 4: Briefing              → SCQA samenvatting
Stap 5: Volgende actie        → Wat doen we vandaag?
```

---

## Deploy Pipeline

Bij `/orchestrate deploy`:

```
Stap 1: Pre-deploy check
  - Uncommitted changes?
  - Compileert het? (cargo build --release)
  - Welke changes gaan live?

Stap 2: Deploy
  - rsync naar VPS
  - Build op VPS
  - cp binary
  - Restart services

Stap 3: /verify
  - Services draaien?
  - FILLED trades?
  - Config actief?
  - Geen regressies?

Stap 4: Documenteer
  - Wat is gedeployed
  - Verify resultaat
  - Eventueel experiment gestart
```

---

## Change Pipeline

Bij `/orchestrate change "[beschrijving]"`:

```
Stap 1: /experiment nieuw "[beschrijving]"
  - Hypothese + baseline + sample size + success criteria

Stap 2: Implementeer change (code of config)

Stap 3: /orchestrate deploy
  - Deploy + verify

Stap 4: /experiment check [naam]
  - Na voldoende trades: evalueer

Stap 5: Go/No-Go
  - GO → markeer als permanent
  - NO-GO → rollback + deploy + verify
```

---

## Quality Gates

| Gate | Vereist |
|------|---------|
| Pre-deploy | Code compileert, tests passen |
| Post-deploy | /verify = PASS (FILLED trades gezien) |
| Experiment start | Baseline gemeten, sample size berekend |
| Experiment beslissing | Minimum trades bereikt, CI berekend |
| Config change | Altijd via experiment, nooit direct |

**Uitzondering:** Bugfixes en wallet verwijderingen hoeven geen experiment (directe schade stoppen).

---

## Regels

| DO | DON'T |
|----|-------|
| Altijd /verify na deploy | Aannemen dat deploy werkt |
| Altijd /experiment voor strategie-changes | Config blind aanpassen |
| SCQA format voor briefings | Lange narratieven |
| Eén change per deploy | Meerdere dingen tegelijk |
| Documenteer elke deploy | "Het staat live" zonder bewijs |
