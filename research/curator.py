"""LLM Playbook Curator — distills evolutionary wisdom across cycles."""

import json
import logging
from pathlib import Path

import anthropic

log = logging.getLogger("autoresearch")

PLAYBOOK_PATH = "data/playbook.md"


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
    dag_recent: list[dict],
    current_playbook: str,
    portfolio_fitness: float,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Call Claude to distill patterns from recent decisions into reusable wisdom."""
    client = anthropic.AsyncAnthropic()

    dag_summary = json.dumps(dag_recent[-20:], indent=2, default=str) if dag_recent else "No decisions yet."

    prompt = f"""Je bent de Playbook Curator voor een Polymarket copy trading bot.

De bot kopieert trades van winstgevende wallets. Elke 2 uur draait een evolutionaire loop die 30 mutaties van wallet-portfolios genereert, scoort, en de beste selecteert.

## Huidige Playbook
{current_playbook or "Leeg — eerste cycle."}

## Laatste Beslissingen (Research DAG)
{dag_summary}

## Huidige Portfolio Fitness: {portfolio_fitness:.1f}/100

## Opdracht
Analyseer de recente beslissingen en destilleer patronen:
1. Welk TYPE wallets presteert goed voor ons? (hoge WR sport, hoog volume, specifieke competities?)
2. Welk TYPE wallets presteert SLECHT? (te laag volume, verkeerde markten, te veel crypto?)
3. Welke wallet-combinaties werken samen? (diversificatie vs concentratie)
4. Wanneer moeten we conservatief vs agressief zijn?

Schrijf een bijgewerkt playbook van MAX 30 regels. Concrete, actionable patronen. Geen vage adviezen.
Begin met de sterkste patronen bovenaan."""

    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"curator LLM call failed: {e}")
        return current_playbook
