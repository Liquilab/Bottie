"""Intelligence DAG — discovery log for Cannae strategy analysis.

Append-only JSONL log of findings from intelligence modules.
Each discovery has a module, finding, confidence, and evidence.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DAG_PATH = "data/intelligence_dag.jsonl"


def load_dag(path: str = DAG_PATH) -> list[dict]:
    """Load all discoveries from the DAG."""
    p = Path(path)
    if not p.exists():
        return []
    discoveries = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                discoveries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return discoveries


def append_discovery(discovery: dict, path: str = DAG_PATH):
    """Append one discovery to the DAG."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    discovery.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    discovery.setdefault("type", "discovery")

    with open(p, "a") as f:
        f.write(json.dumps(discovery, default=str) + "\n")


def extract_discoveries(report: dict) -> list[dict]:
    """Extract notable discoveries from an intelligence report."""
    discoveries = []
    ts = datetime.now(timezone.utc).isoformat()

    # Event selection filters
    es = report.get("event_selection", {})
    for f in es.get("inferred_filters", []):
        discoveries.append({
            "timestamp": ts,
            "type": "discovery",
            "module": "event_selection",
            "finding": f.get("description", ""),
            "confidence": 0.8,
            "evidence": f,
        })

    # Sizing rules
    sz = report.get("sizing", {})
    for r in sz.get("decision_rules", []):
        discoveries.append({
            "timestamp": ts,
            "type": "discovery",
            "module": "sizing_model",
            "finding": r.get("description", ""),
            "confidence": 0.7,
            "evidence": r,
        })

    # Implied edge outliers
    ep = report.get("entry_prices", {})
    for bucket, data in ep.get("implied_edge", {}).items():
        edge = data.get("edge", 0)
        if abs(edge) > 0.10 and data.get("bets", 0) >= 10:
            discoveries.append({
                "timestamp": ts,
                "type": "discovery",
                "module": "entry_price",
                "finding": f"Price bucket {bucket}: edge={edge:.1%} over {data['bets']} bets",
                "confidence": min(0.95, 0.5 + data["bets"] / 100),
                "evidence": data,
            })

    # Temporal patterns
    tmp = report.get("temporal", {})
    batches = tmp.get("batches", {})
    if batches.get("avg_batch_size", 0) > 3:
        discoveries.append({
            "timestamp": ts,
            "type": "discovery",
            "module": "temporal",
            "finding": f"Batch betting: avg {batches['avg_batch_size']:.1f} bets per session",
            "confidence": 0.85,
            "evidence": batches,
        })

    # Odds edge
    oe = report.get("odds_edge", {})
    edge_data = oe.get("edge_analysis", {})
    if edge_data and edge_data.get("positive_edge_pct", 0) > 0.6:
        discoveries.append({
            "timestamp": ts,
            "type": "discovery",
            "module": "odds_edge",
            "finding": f"PM mispriced vs bookmakers: {edge_data['positive_edge_pct']:.0%} of trades have positive edge (avg {edge_data['avg_edge']:.1%})",
            "confidence": 0.75,
            "evidence": edge_data,
        })

    return discoveries


def save_predictions(predictions: list[dict], path: str = "data/predictions.json"):
    """Save predictions for tomorrow's accuracy tracking."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Load existing
    existing = []
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            existing = []

    # Add new batch
    batch = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "predictions": predictions,
        "checked": False,
    }
    existing.append(batch)

    # Keep last 30 days
    existing = existing[-30:]
    p.write_text(json.dumps(existing, indent=2))


def check_predictions(yesterday_predictions: list[dict], today_dataset: dict) -> dict:
    """Check yesterday's predictions against today's actual data."""
    if not yesterday_predictions:
        return {"no_predictions": True}

    results = []
    today_leagues = set(b["league"] for b in today_dataset.get("all_bets", []))
    today_slugs = set(b["event_slug"] for b in today_dataset.get("all_bets", []))

    for pred in yesterday_predictions:
        actual = "unknown"
        if pred.get("type") == "league_activity":
            league = pred.get("league", "")
            actual = "correct" if league in today_leagues else "incorrect"
        elif pred.get("type") == "bet_count":
            expected = pred.get("expected_range", [0, 100])
            actual_count = len(today_dataset.get("all_bets", []))
            actual = "correct" if expected[0] <= actual_count <= expected[1] else "incorrect"

        results.append({**pred, "actual": actual})

    correct = sum(1 for r in results if r["actual"] == "correct")
    total = sum(1 for r in results if r["actual"] != "unknown")

    return {
        "predictions_checked": len(results),
        "correct": correct,
        "total_checkable": total,
        "accuracy": round(correct / total, 4) if total > 0 else 0,
        "details": results,
    }
