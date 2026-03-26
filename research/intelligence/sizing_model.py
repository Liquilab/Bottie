"""Module 4: Sizing Model — what determines Cannae's bet size?"""

import logging
import statistics
from collections import defaultdict

log = logging.getLogger("intelligence.sizing")


def analyze_sizing_model(dataset: dict) -> dict:
    """Multivariate sizing analysis: league × market_type × price × timing."""
    resolved = dataset["resolved"]
    if len(resolved) < 30:
        return {"insufficient_data": True}

    return {
        "quartiles": _quartile_analysis(resolved),
        "by_league_size": _size_by_group(resolved, "league"),
        "by_mt_size": _size_by_group(resolved, "mt"),
        "by_price_bucket": _size_by_price(resolved),
        "decision_rules": _extract_rules(resolved),
    }


def _quartile_analysis(resolved: list) -> dict:
    """Q1-Q4 analysis with Spearman correlation."""
    costs = sorted(r["cost"] for r in resolved)
    q25 = costs[len(costs) // 4]
    q50 = costs[len(costs) // 2]
    q75 = costs[3 * len(costs) // 4]

    quartiles = {"Q1_small": [], "Q2": [], "Q3": [], "Q4_large": []}
    for r in resolved:
        c = r["cost"]
        if c <= q25:
            quartiles["Q1_small"].append(r)
        elif c <= q50:
            quartiles["Q2"].append(r)
        elif c <= q75:
            quartiles["Q3"].append(r)
        else:
            quartiles["Q4_large"].append(r)

    result = {"thresholds": {"q25": round(q25, 2), "q50": round(q50, 2), "q75": round(q75, 2)}}
    for label, bets in quartiles.items():
        wins = sum(1 for b in bets if b["result"] == "WIN")
        total = len(bets)
        cost = sum(b["cost"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)
        result[label] = {
            "bets": total,
            "wr": round(wins / total, 4) if total else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
            "avg_cost": round(cost / total, 2) if total else 0,
        }

    # Spearman
    pairs = [(r["cost"], 1 if r["result"] == "WIN" else 0) for r in resolved]
    n = len(pairs)
    if n >= 10:
        costs_ranked = sorted(range(n), key=lambda i: pairs[i][0])
        rank_cost = [0] * n
        rank_outcome = [0] * n
        for rank, idx in enumerate(costs_ranked):
            rank_cost[idx] = rank
        outcomes_ranked = sorted(range(n), key=lambda i: pairs[i][1])
        for rank, idx in enumerate(outcomes_ranked):
            rank_outcome[idx] = rank
        d_sq = sum((rank_cost[i] - rank_outcome[i]) ** 2 for i in range(n))
        result["spearman_r"] = round(1 - 6 * d_sq / (n * (n * n - 1)), 4)

    return result


def _size_by_group(resolved: list, key: str) -> dict:
    """Average bet size and size-WR relationship per group."""
    groups = defaultdict(list)
    for r in resolved:
        groups[r[key]].append(r)

    result = {}
    for k, bets in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(bets) < 5:
            continue
        costs = [b["cost"] for b in bets]
        wins = sum(1 for b in bets if b["result"] == "WIN")
        total = len(bets)
        # Split at median cost within group
        median_cost = statistics.median(costs)
        above = [b for b in bets if b["cost"] >= median_cost]
        below = [b for b in bets if b["cost"] < median_cost]
        above_wr = sum(1 for b in above if b["result"] == "WIN") / len(above) if above else 0
        below_wr = sum(1 for b in below if b["result"] == "WIN") / len(below) if below else 0

        result[k] = {
            "bets": total,
            "avg_cost": round(statistics.mean(costs), 2),
            "median_cost": round(median_cost, 2),
            "wr": round(wins / total, 4),
            "big_bets_wr": round(above_wr, 4),
            "small_bets_wr": round(below_wr, 4),
            "size_predicts_win": above_wr > below_wr + 0.05,
        }
    return result


def _size_by_price(resolved: list) -> dict:
    """Bet size by entry price bucket."""
    buckets = {
        "cheap_0_30": [],    # implied prob 0-30%
        "mid_30_60": [],     # 30-60%
        "favorite_60_80": [],  # 60-80%
        "heavy_80_plus": [],   # 80%+
    }
    for r in resolved:
        p = r["avg_price"]
        if p < 0.30:
            buckets["cheap_0_30"].append(r)
        elif p < 0.60:
            buckets["mid_30_60"].append(r)
        elif p < 0.80:
            buckets["favorite_60_80"].append(r)
        else:
            buckets["heavy_80_plus"].append(r)

    result = {}
    for label, bets in buckets.items():
        if not bets:
            continue
        wins = sum(1 for b in bets if b["result"] == "WIN")
        total = len(bets)
        costs = [b["cost"] for b in bets]
        pnl = sum(b["pnl"] for b in bets)
        result[label] = {
            "bets": total,
            "avg_cost": round(statistics.mean(costs), 2),
            "wr": round(wins / total, 4),
            "roi": round(pnl / sum(costs), 4) if sum(costs) > 0 else 0,
            "pnl": round(pnl, 2),
        }
    return result


def _extract_rules(resolved: list) -> list:
    """Extract simple IF/THEN sizing rules from data."""
    rules = []

    # Rule 1: Does he bet more on favorites?
    fav = [r for r in resolved if r["avg_price"] >= 0.60]
    non_fav = [r for r in resolved if r["avg_price"] < 0.60]
    if fav and non_fav:
        avg_fav = statistics.mean(r["cost"] for r in fav)
        avg_non = statistics.mean(r["cost"] for r in non_fav)
        if avg_fav > avg_non * 1.3:
            rules.append({
                "rule": "SIZE_UP_FAVORITES",
                "description": f"Bets {avg_fav/avg_non:.1f}x more on favorites (price>=60c)",
                "avg_fav_cost": round(avg_fav, 2),
                "avg_non_fav_cost": round(avg_non, 2),
            })

    # Rule 2: Does he bet more in certain leagues?
    league_costs = defaultdict(list)
    for r in resolved:
        league_costs[r["league"]].append(r["cost"])
    if len(league_costs) >= 3:
        league_avgs = {k: statistics.mean(v) for k, v in league_costs.items() if len(v) >= 5}
        if league_avgs:
            overall_avg = statistics.mean(r["cost"] for r in resolved)
            big_leagues = {k: round(v, 2) for k, v in league_avgs.items() if v > overall_avg * 1.5}
            if big_leagues:
                rules.append({
                    "rule": "SIZE_UP_LEAGUES",
                    "description": f"Bets >1.5x average in: {', '.join(big_leagues.keys())}",
                    "league_avg_costs": big_leagues,
                    "overall_avg_cost": round(overall_avg, 2),
                })

    # Rule 3: Size correlates with shares (Cannae thinks in shares)
    share_sizes = [r["shares"] for r in resolved]
    if share_sizes:
        common_shares = statistics.mode([round(s, -1) for s in share_sizes]) if len(share_sizes) >= 10 else 0
        if common_shares > 0:
            rules.append({
                "rule": "FIXED_SHARE_SIZE",
                "description": f"Most common share size: ~{common_shares:.0f} shares",
                "mode_shares": round(common_shares, 0),
            })

    return rules
