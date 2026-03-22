---
name: flows
description: "Map alle Bottie workflows met failure modes, handoff contracts, en edge cases"
---

# /flows — Workflow Architect

Mapt alle workflows in Bottie met happy paths, failure modes, en recovery actions. Vindt edge cases voordat ze bugs worden.

**Gebaseerd op:** agency-agents/specialized-workflow-architect (aangepast voor trading bot)

**Status: GEPARKEERD** — Workflow specs nog leeg. Eenmalig invullen bij grote refactor, niet als recurring skill.

---

## Gebruik

```
/flows                  — Overzicht van alle workflows
/flows [workflow]       — Detail van specifieke workflow
/flows audit            — Check of specs matchen met code
/flows edge-cases       — Zoek ongedekte edge cases
```

---

## Bottie Workflow Registry

### Primaire Workflows

| Workflow | Trigger | Frequentie | Status |
|----------|---------|-----------|--------|
| Copy Trade | Wallet positie verandert | Elke 15s poll | [Specced/Missing] |
| Order Placement | Copy signal geaccepteerd | Per trade | [Specced/Missing] |
| Resolution | Market resolved | Elke 60s check | [Specced/Missing] |
| Bankroll Sync | Timer | Elke 5 min | [Specced/Missing] |
| Config Hot Reload | config.yaml gewijzigd | On change | [Specced/Missing] |

### Research Workflows

| Workflow | Trigger | Frequentie | Status |
|----------|---------|-----------|--------|
| Wallet Scout | Timer | Elk uur | [Specced/Missing] |
| Autoresearch Evolution | Timer | Elke 2 uur | [Specced/Missing] |
| Playbook Curator | Timer | Elke 6 uur (3e cycle) | [Specced/Missing] |

### Operationele Workflows

| Workflow | Trigger | Frequentie | Status |
|----------|---------|-----------|--------|
| Deploy | Handmatig | Ad hoc | [Specced/Missing] |
| Experiment Start | Handmatig | Ad hoc | [Specced/Missing] |

---

## Workflow Spec Format

Per workflow:

```markdown
# WORKFLOW: [Naam]

## Overview
[Wat doet deze workflow, wie triggert het, wat produceert het]

## Stappen

### STEP 1: [Naam]
**Actor:** [component]
**Action:** [wat gebeurt er]
**Timeout:** Xs
**Input:** { ... }
**On SUCCESS:** → STEP 2
**On FAILURE:**
  - FAILURE(timeout): [recovery]
  - FAILURE(api_error): [recovery]
  - FAILURE(validation): [recovery]

### STEP 2: ...

## Edge Cases
- Wat als [scenario]? → [gedrag]
- Wat als [scenario]? → [gedrag]

## Bekende Issues
- [Issue]: [huidige workaround]
```

---

## Bekende Edge Cases om te Checken

| Edge Case | Workflow | Risico |
|-----------|---------|--------|
| Tegenstrijdige bets op zelfde event | Copy Trade | Geld verliezen op beide kanten |
| Fee API faalt | Order Placement | Order rejected |
| Wallet verwijderd mid-cycle | Config Reload | Orphaned positions |
| Bankroll sync faalt | Bankroll Sync | Verkeerde sizing |
| Market resolved maar trade nog open in log | Resolution | PnL niet bijgewerkt |
| Twee wallets kopen tegelijk dezelfde markt | Copy Trade | Dubbele positie? |
| Config.yaml corrupt na autoresearch write | Config Reload | Bot crasht |
| VPS reboot mid-trade | Order Placement | Order status onbekend |

---

## Audit Mode

Bij `/flows audit`:

1. Lees de workflow spec
2. Lees de corresponderende code (`src/*.rs` of `research/*.py`)
3. Vergelijk: doet de code wat de spec zegt?
4. Rapporteer discrepancies

```markdown
## Audit: [Workflow Naam]

| Stap | Spec | Code | Match? |
|------|------|------|--------|
| 1 | [verwacht gedrag] | [werkelijk gedrag] | [JA/NEE] |
| 2 | ... | ... | ... |

### Discrepancies
- [Stap X]: Spec zegt [A], code doet [B]
  Impact: [wat kan er misgaan]
  Fix: [in spec of code?]
```

---

## Regels

| DO | DON'T |
|----|-------|
| Lees de code, niet alleen beschrijvingen | Aannemen dat code doet wat je denkt |
| Map ELKE failure mode | Alleen happy path documenteren |
| Check edge cases met concrete scenarios | Abstract blijven |
| Update specs na code changes | Specs laten verlopen |
| Focus op geldgerelateerde flows eerst | Alles tegelijk mappen |
