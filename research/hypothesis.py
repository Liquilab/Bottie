"""Generate trading hypotheses using Claude API with playbook wisdom."""

import json
from pathlib import Path

import anthropic


PLAYBOOK_PATH = "research/playbook.md"


def load_playbook() -> str:
    """Fix #3: Load accumulated playbook wisdom."""
    p = Path(PLAYBOOK_PATH)
    if p.exists():
        return p.read_text()
    return ""


async def update_playbook(
    hypotheses_results: list[dict],
    current_playbook: str,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Fix #3: Playbook Curator — distill cycle results into reusable wisdom."""
    client = anthropic.AsyncAnthropic()

    # Summarize results for the curator
    results_summary = []
    for h in hypotheses_results:
        bt = h.get("backtest_result", {})
        results_summary.append({
            "description": h.get("description", "?"),
            "config_changes": h.get("config_changes", {}),
            "fitness": bt.get("fitness", 0),
            "roi": bt.get("roi", 0),
            "sharpe": bt.get("sharpe", 0),
            "win_rate": bt.get("win_rate", 0),
            "trades": bt.get("trades", 0),
            "overfit_warning": bt.get("overfit_warning", False),
            "oos_degradation": bt.get("oos_degradation", None),
        })

    prompt = f"""Je bent de Playbook Curator voor een Polymarket copy-trading bot.
Je taak: destilleer de resultaten van de laatste research cyclus tot BLIJVENDE lessen.

HUIDIG PLAYBOOK:
{current_playbook if current_playbook else "(Leeg — dit is de eerste cyclus)"}

RESULTATEN LAATSTE CYCLUS:
{json.dumps(results_summary, indent=2, default=str)}

INSTRUCTIES:
1. Update het playbook met nieuwe inzichten over WAT werkt en WAAROM
2. Verwijder aannames die door backtests ontkracht zijn
3. Wees extreem beknopt (parsimony). Focus op abstracte regels
4. Markeer patronen die zich over meerdere cycli herhalen als [BEVESTIGD]
5. Max 20 regels totaal

Format:
# Trading Playbook — Laatste update: [datum]

## Bevestigde Patronen
- [regel]

## Hypotheses (nog te bevestigen)
- [regel]

## Anti-patronen (bewezen niet te werken)
- [regel]
"""

    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    new_playbook = response.content[0].text

    # Save to file
    Path(PLAYBOOK_PATH).write_text(new_playbook)

    return new_playbook


async def generate_hypotheses(
    report: dict,
    previous_hypotheses: list[dict],
    model: str = "claude-sonnet-4-20250514",
    playbook: str = "",
) -> list[dict]:
    """Use Claude to generate testable trading hypotheses."""
    client = anthropic.AsyncAnthropic()

    prev_summary = ""
    if previous_hypotheses:
        prev_summary = "\n".join(
            f"- {h.get('description', '?')} → fitness={h.get('backtest_result', {}).get('fitness', '?')} "
            f"roi={h.get('backtest_result', {}).get('roi', '?')} "
            f"{'⚠ OVERFIT' if h.get('backtest_result', {}).get('overfit_warning') else ''}"
            for h in previous_hypotheses[-20:]
        )

    playbook_section = ""
    if playbook:
        playbook_section = f"""
PLAYBOOK (accumuleerde wijsheid uit vorige cycli — gebruik dit als startpunt):
{playbook}
"""

    prompt = f"""Je bent een quant researcher voor een Polymarket copy-trading bot.
Analyseer deze trading data en genereer 5 concrete, testbare hypotheses om de performance te verbeteren.

BELANGRIJK: Dit is TRAIN data. De hypotheses worden gevalideerd op ONZIEN test data (70/30 split).
Vermijd overfitting: zoek robuuste patronen, geen ruis.
{playbook_section}
HUIDIGE PERFORMANCE (TRAIN SET):
{json.dumps(report, indent=2, default=str)}

VORIGE HYPOTHESES EN RESULTATEN:
{prev_summary if prev_summary else "Geen vorige hypotheses."}

Elke hypothese moet bevatten:
1. description: Beschrijving in één zin
2. config_changes: Specifieke parameter changes (als dict, gebruik ALLEEN de onderstaande geldige keys)
3. expected_improvement: Verwachte verbetering in % en waarom
4. min_sample_size: Minimum trades voor betrouwbare test

GELDIGE config_changes KEYS (gebruik alleen deze, anders heeft de hypothese geen effect):
- "wallet_weights": {{"0xaddress": weight_float}} — pas gewichten aan per wallet-adres
- "sport_multipliers": {{"nba": 1.2, "soccer": 0.8}} — sport-specifieke confidence multipliers
- "timing_rules": ["rule1"] — timing regels (informatief, nog niet geïmplementeerd)
- "kelly_fraction": float — Kelly fractie voor sizing (0.1 – 0.5)
- "copy_base_size_pct": float — basis inzetstapel als % van bankroll (1.0 – 10.0)
- "min_consensus": int — minimum wallets voor signaalbevestiging (1 – 5)
- "max_delay_seconds": int — max seconden na originele trade om te kopiëren (30 – 300)
- "min_edge_pct": float — minimum edge % voor odds-arb trades (1.0 – 10.0)
- "min_price": float — minimum entry price (0.10 – 0.40). Longshots <0.20 hebben 0% WR in onze data
- "max_price": float — maximum entry price (0.80 – 0.95). Hoge favorites hebben slechte payout ratio
- "max_open_bets": int — max gelijktijdige open posities (10 – 200). Bij kleine bankroll: minder is beter
- "max_resolution_days": int — max dagen tot markt resolveert (1 – 14). Snellere turnover bij kleine bankroll

BELANGRIJKE CONTEXT:
- Bankroll is ~$25 cash, ~$336 portfolio (veel in open posities)
- Sommige wallets zijn arb-traders (kopen beide kanten <$1) — hun edge is NIET kopieerbaar
- Wallets met >15% both-sides posities: sovereign2013 (78%), RN1 (12%), GamblingIsAllYouNeed (19%)
- Clean directional: Cannae (1%), HedgeMaster88 (0%) — meest kopieerbaar
- Huidige min_price=0.20, max_delay=60s (maar delay wordt niet enforced in code)

TIPS VOOR ROBUUSTE HYPOTHESES:
- Eenvoudige changes (1-2 parameters) zijn beter dan complexe (parsimony)
- Focus op wallets met >10 trades in de dataset
- Wees conservatief met sizing bij $200 bankroll
- Zoek naar structurele edge, niet lucky runs

Antwoord als JSON array van hypotheses."""

    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    # Parse the response
    text = response.content[0].text

    # Try to extract JSON from the response
    try:
        # Try direct parse
        hypotheses = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                hypotheses = json.loads(text[start:end])
            except json.JSONDecodeError:
                hypotheses = []
        else:
            hypotheses = []

    return hypotheses


def load_hypothesis_log(directory: str = "research/hypotheses/") -> list[dict]:
    """Load all previous hypothesis results."""
    p = Path(directory)
    if not p.exists():
        return []

    hypotheses = []
    for file in sorted(p.glob("*.json")):
        try:
            data = json.loads(file.read_text())
            hypotheses.append(data)
        except (json.JSONDecodeError, IOError):
            continue

    return hypotheses


def save_hypothesis(hypothesis: dict, directory: str = "research/hypotheses/"):
    """Save a hypothesis result."""
    p = Path(directory)
    p.mkdir(parents=True, exist_ok=True)

    from datetime import datetime
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    desc = hypothesis.get("description", "unknown")[:50].replace(" ", "_")
    filename = f"{timestamp}_{desc}.json"

    (p / filename).write_text(json.dumps(hypothesis, indent=2, default=str))
