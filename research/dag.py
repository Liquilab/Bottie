"""Research DAG — decision log for evolutionary wallet management."""

import json
from datetime import datetime, timezone
from pathlib import Path

DAG_PATH = "data/research_dag.jsonl"


def load_dag(path: str = DAG_PATH) -> list[dict]:
    """Load all decisions from the DAG."""
    p = Path(path)
    if not p.exists():
        return []
    decisions = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return decisions


def append_decision(decision: dict, path: str = DAG_PATH):
    """Append one decision to the DAG."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(decision, default=str) + "\n")


def diff_portfolios(old_watchlist: list[dict], new_wallets: list, portfolio_fitness: float) -> list[dict]:
    """Compare old config watchlist vs new portfolio, emit decisions for changes."""
    old_by_addr = {w["address"].lower(): w for w in old_watchlist}
    new_by_addr = {w.address: w for w in new_wallets}

    decisions = []
    now = datetime.now(timezone.utc).isoformat()

    # Added wallets
    for addr, w in new_by_addr.items():
        if addr not in old_by_addr:
            decisions.append({
                "timestamp": now,
                "action": "add",
                "wallet_address": addr,
                "wallet_name": w.name,
                "old_weight": None,
                "new_weight": w.weight,
                "wallet_score": w.score,
                "portfolio_fitness": portfolio_fitness,
                "outcome_pnl": None,
            })

    # Removed wallets
    for addr, w in old_by_addr.items():
        if addr not in new_by_addr:
            decisions.append({
                "timestamp": now,
                "action": "remove",
                "wallet_address": addr,
                "wallet_name": w.get("name", addr[:10]),
                "old_weight": w.get("weight"),
                "new_weight": None,
                "wallet_score": None,
                "portfolio_fitness": portfolio_fitness,
                "outcome_pnl": None,
            })

    # Reweighted wallets
    for addr, w in new_by_addr.items():
        if addr in old_by_addr:
            old_weight = old_by_addr[addr].get("weight", 0.5)
            if abs(w.weight - old_weight) >= 0.1:
                decisions.append({
                    "timestamp": now,
                    "action": "reweight",
                    "wallet_address": addr,
                    "wallet_name": w.name,
                    "old_weight": old_weight,
                    "new_weight": w.weight,
                    "wallet_score": w.score,
                    "portfolio_fitness": portfolio_fitness,
                    "outcome_pnl": None,
                })

    return decisions


def update_outcomes(dag: list[dict], trades_path: str = "data/trades.jsonl"):
    """Fill in outcome_pnl for decisions older than 24h using trade data."""
    trades_file = Path(trades_path)
    if not trades_file.exists():
        return dag

    # Load trades
    trades = []
    for line in trades_file.read_text().splitlines():
        if line.strip():
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    resolved = [t for t in trades if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss")]

    now = datetime.now(timezone.utc)
    for decision in dag:
        if decision.get("outcome_pnl") is not None:
            continue
        if decision.get("action") == "remove":
            continue

        try:
            decision_time = datetime.fromisoformat(decision["timestamp"])
        except (KeyError, ValueError):
            continue

        age_hours = (now - decision_time).total_seconds() / 3600
        if age_hours < 24:
            continue

        # Find trades from this wallet after the decision
        addr = (decision.get("wallet_address") or "").lower()
        wallet_trades = [
            t for t in resolved
            if (t.get("copy_wallet") or "").lower().startswith(addr[:10])
            and t.get("timestamp", "") > decision["timestamp"]
        ]

        if wallet_trades:
            pnl = sum(t.get("pnl", 0) for t in wallet_trades)
            decision["outcome_pnl"] = round(pnl, 2)

    return dag
