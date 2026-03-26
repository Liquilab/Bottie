"""Cannae Strategy Curator — distills intelligence findings into playbook rules."""

import json
import logging
from pathlib import Path

import anthropic

log = logging.getLogger("intelligence")

PLAYBOOK_PATH = "data/cannae_playbook.md"


def load_playbook(path: str = PLAYBOOK_PATH) -> str:
    p = Path(path)
    if p.exists():
        return p.read_text()
    return ""


def save_playbook(text: str, path: str = PLAYBOOK_PATH):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


async def curate_playbook(
    intelligence_report: dict,
    current_playbook: str,
    predictions_yesterday: dict = None,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Call Claude to distill Cannae intelligence into concrete IF/THEN rules."""
    client = anthropic.AsyncAnthropic()

    # Summarize key findings (avoid sending full report to save tokens)
    summary = _summarize_report(intelligence_report)
    predictions_check = json.dumps(predictions_yesterday, indent=2) if predictions_yesterday else "Geen vorige voorspellingen."

    prompt = f"""Je bent de Strategy Curator voor een Cannae intelligence systeem.

Cannae is een winstgevende Polymarket sport trader. We analyseren dagelijks zijn gedrag om zijn strategie te reverse-engineeren.

## Huidige Playbook
{current_playbook or "Leeg — eerste analyse."}

## Vandaag's Intelligence Findings
{summary}

## Gisteren's Voorspellingen vs Werkelijkheid
{predictions_check}

## Opdracht
Destilleer concrete, testbare regels uit de data. Format:

1. **IF/THEN regels** (e.g. "IF league=EPL AND price<0.65 THEN bet size = Q4")
2. **Filters** (e.g. "NEVER bet on games with liquidity < $3000")
3. **Timing** (e.g. "Bets placed 2-6h before start have highest ROI")
4. **Edge source** (e.g. "PM mispriced vs Pinnacle by avg 3.2% on favorites")

Schrijf MAX 30 regels. Sorteer op confidence (hoogste eerst).
Markeer NIEUWE inzichten vs vorige playbook met [NEW] of [UPDATED].
Verwijder regels die door nieuwe data zijn weerlegd.

Eindig met 3 VOORSPELLINGEN voor morgen (wat verwacht je dat Cannae doet?).
"""

    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"curator LLM call failed: {e}")
        return current_playbook


def _summarize_report(report: dict) -> str:
    """Extract key findings from intelligence report for the curator prompt."""
    lines = []

    # Overall stats
    overall = report.get("quant_analysis", {}).get("overall", {})
    if overall:
        lines.append(f"Overall: {overall.get('bets', 0)} bets, {overall.get('wr', 0):.0%} WR, {overall.get('roi', 0):.0%} ROI, ${overall.get('pnl', 0):.0f} PnL")

    # Event selection
    es = report.get("event_selection", {})
    if isinstance(es.get("selection_rate"), dict):
        sr = es["selection_rate"]
        lines.append(f"Event selection: {sr.get('selected_events', '?')}/{sr.get('available_events', '?')} events selected ({sr.get('rate', 0):.1%})")
    filters = es.get("inferred_filters", [])
    for f in filters:
        lines.append(f"  Filter: {f.get('description', '')}")

    # Entry price
    ep = report.get("entry_prices", {})
    ie = ep.get("implied_edge", {})
    if ie:
        best_bucket = max(ie.items(), key=lambda x: x[1].get("edge", 0)) if ie else None
        if best_bucket:
            lines.append(f"Best edge bucket: {best_bucket[0]} → edge={best_bucket[1].get('edge', 0):.1%}")

    # Sizing
    sz = report.get("sizing", {})
    rules = sz.get("decision_rules", [])
    for r in rules:
        lines.append(f"  Sizing rule: {r.get('description', '')}")

    # Temporal
    tmp = report.get("temporal", {})
    batches = tmp.get("batches", {})
    if batches:
        lines.append(f"Batch pattern: avg {batches.get('avg_batch_size', 0):.1f} bets/batch, median gap {batches.get('median_gap_hours', 0):.1f}h")

    # Odds edge
    oe = report.get("odds_edge", {})
    edge_analysis = oe.get("edge_analysis", {})
    if edge_analysis:
        lines.append(f"Odds edge: avg {edge_analysis.get('avg_edge', 0):.1%}, {edge_analysis.get('positive_edge_pct', 0):.0%} positive")

    return "\n".join(lines) if lines else "Geen data beschikbaar."
