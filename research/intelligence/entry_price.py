"""Module 3: Entry Price Analysis — does Cannae buy CHEAP or EXPENSIVE?"""

import json
import logging
import statistics
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("intelligence.entry_price")

DATA_LAKE = Path(__file__).parent.parent / "data_lake"
TOKEN_MAP = DATA_LAKE / "token_condition_map.json"


def analyze_entry_prices(dataset: dict) -> dict:
    """Analyze Cannae's entry prices: edge vs market, dip buying, price distribution."""
    resolved = dataset["resolved"]
    event_cache = dataset["event_cache"]

    # Try to load token→condition mapping for price lookups
    price_data = _load_price_context(resolved)

    return {
        "price_distribution": _price_distribution(resolved),
        "implied_edge": _implied_edge(resolved),
        "dip_detection": _detect_dip_buying(resolved, price_data),
        "price_vs_outcome": _price_vs_outcome(resolved),
        "by_league_price": _avg_price_by_league(resolved),
    }


def _price_distribution(resolved: list) -> dict:
    """Distribution of entry prices."""
    prices = [r["avg_price"] for r in resolved]
    if not prices:
        return {}
    return {
        "mean": round(statistics.mean(prices), 4),
        "median": round(statistics.median(prices), 4),
        "stdev": round(statistics.stdev(prices), 4) if len(prices) > 1 else 0,
        "min": round(min(prices), 4),
        "max": round(max(prices), 4),
        "pct_below_50": round(sum(1 for p in prices if p < 0.50) / len(prices), 4),
        "pct_above_70": round(sum(1 for p in prices if p > 0.70) / len(prices), 4),
    }


def _implied_edge(resolved: list) -> dict:
    """Implied edge = actual WR per price bucket minus implied probability.

    If Cannae buys at 60c and wins 75% of the time, his edge is +15%.
    """
    buckets = defaultdict(list)
    for r in resolved:
        p = r["avg_price"]
        # 10-cent buckets
        bucket = f"{int(p * 10) * 10}-{int(p * 10) * 10 + 10}c"
        buckets[bucket].append(r)

    result = {}
    for bucket, bets in sorted(buckets.items()):
        if len(bets) < 5:
            continue
        wins = sum(1 for b in bets if b["result"] == "WIN")
        total = len(bets)
        actual_wr = wins / total
        implied_prob = statistics.mean(b["avg_price"] for b in bets)
        edge = actual_wr - implied_prob
        cost = sum(b["cost"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)

        result[bucket] = {
            "bets": total,
            "actual_wr": round(actual_wr, 4),
            "implied_prob": round(implied_prob, 4),
            "edge": round(edge, 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
        }
    return result


def _detect_dip_buying(resolved: list, price_data: dict) -> dict:
    """Detect if Cannae buys when prices have dropped (dip buying).

    Uses data_lake price history if available, otherwise analyzes multi-trade patterns.
    """
    if not price_data:
        # Fallback: check if bets with multiple trades show decreasing prices
        multi_trade = [r for r in resolved if r["n_trades"] > 1]
        if not multi_trade:
            return {"data_available": False}

        return {
            "data_available": "partial",
            "multi_trade_bets": len(multi_trade),
            "avg_trades_per_bet": round(statistics.mean(r["n_trades"] for r in multi_trade), 1),
            "note": "Full dip detection requires data_lake price history",
        }

    # With price data: compare entry vs T-24h price
    dip_buys = 0
    total_compared = 0
    edges = []

    for r in resolved:
        cid = r["cid"]
        if cid not in price_data:
            continue
        hist = price_data[cid]
        if "price_24h_ago" not in hist:
            continue

        total_compared += 1
        entry = r["avg_price"]
        p24 = hist["price_24h_ago"]
        if entry < p24 * 0.95:  # bought at 5%+ discount
            dip_buys += 1
        edges.append(p24 - entry)

    if total_compared == 0:
        return {"data_available": False}

    return {
        "bets_compared": total_compared,
        "dip_buys_pct": round(dip_buys / total_compared, 4),
        "avg_discount_vs_24h": round(statistics.mean(edges), 4) if edges else 0,
        "median_discount_vs_24h": round(statistics.median(edges), 4) if edges else 0,
    }


def _price_vs_outcome(resolved: list) -> dict:
    """How does entry price relate to outcome?"""
    wins = [r for r in resolved if r["result"] == "WIN"]
    losses = [r for r in resolved if r["result"] == "LOSS"]

    if not wins or not losses:
        return {}

    return {
        "avg_win_price": round(statistics.mean(r["avg_price"] for r in wins), 4),
        "avg_loss_price": round(statistics.mean(r["avg_price"] for r in losses), 4),
        "median_win_price": round(statistics.median(r["avg_price"] for r in wins), 4),
        "median_loss_price": round(statistics.median(r["avg_price"] for r in losses), 4),
        "wins_at_favorite": sum(1 for r in wins if r["avg_price"] >= 0.60),
        "losses_at_favorite": sum(1 for r in losses if r["avg_price"] >= 0.60),
        "wins_at_underdog": sum(1 for r in wins if r["avg_price"] < 0.40),
        "losses_at_underdog": sum(1 for r in losses if r["avg_price"] < 0.40),
    }


def _avg_price_by_league(resolved: list) -> dict:
    """Average entry price per league."""
    leagues = defaultdict(list)
    for r in resolved:
        leagues[r["league"]].append(r["avg_price"])

    result = {}
    for league, prices in sorted(leagues.items(), key=lambda x: -len(x[1])):
        if len(prices) < 5:
            continue
        result[league] = {
            "bets": len(prices),
            "avg_price": round(statistics.mean(prices), 4),
            "median_price": round(statistics.median(prices), 4),
        }
    return result


def _load_price_context(resolved: list) -> dict:
    """Try to load historical price data from data_lake for entry comparison."""
    if not TOKEN_MAP.exists():
        log.info("No token_condition_map.json — skipping price context")
        return {}

    try:
        tcm = json.loads(TOKEN_MAP.read_text())
    except Exception:
        return {}

    # Build condition→token mapping
    cid_to_token = {}
    for token_id, cid in tcm.items():
        cid_to_token[cid] = token_id

    # For each resolved bet, try to find price history
    prices_dir = DATA_LAKE / "prices"
    if not prices_dir.exists():
        return {}

    price_data = {}
    checked = 0
    for r in resolved[:100]:  # Limit to avoid long load times
        cid = r["cid"]
        token_id = cid_to_token.get(cid)
        if not token_id:
            continue

        price_file = prices_dir / f"{token_id}.parquet"
        if not price_file.exists():
            continue

        try:
            import pandas as pd
            df = pd.read_parquet(price_file)
            if df.empty:
                continue

            trade_ts = r["first_ts"]
            # Find price 24h before entry
            target_ts = trade_ts - 86400
            df["ts_diff"] = abs(df.iloc[:, 0] - target_ts)
            closest = df.loc[df["ts_diff"].idxmin()]
            price_data[cid] = {"price_24h_ago": float(closest.iloc[1]) if len(closest) > 1 else 0}
            checked += 1
        except Exception:
            continue

    log.info(f"Loaded price context for {checked} bets")
    return price_data
