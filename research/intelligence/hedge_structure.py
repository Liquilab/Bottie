"""Module 11: Hedge Structure Analysis — HOW does Cannae construct multi-leg games?

Analyzes the COMBINATIONS of bets within a game:
1. What market type combinations does Cannae use? (win+draw, 2×NO, win+ou, etc)
2. How does the hedge ratio relate to ROI?
3. What's the optimal number of legs per game?
4. Do games with NO draw outperform games without?
"""

import logging
import statistics
from collections import defaultdict, Counter

log = logging.getLogger("intelligence.hedge_structure")


def analyze_hedge_structure(dataset: dict) -> dict:
    """Full hedge structure analysis."""
    resolved = dataset["resolved"]
    if len(resolved) < 30:
        return {"insufficient_data": True}

    # Group into games
    by_event = defaultdict(list)
    for b in resolved:
        slug = b.get("event_slug", "")
        if slug:
            by_event[slug].append(b)

    games = []
    for slug, bets in by_event.items():
        game_cost = sum(b["cost"] for b in bets)
        game_pnl = sum(b["pnl"] for b in bets)
        league = bets[0]["league"]

        # Classify each leg
        legs = []
        for b in bets:
            outcome = b.get("outcome", "").lower()
            is_no = outcome.startswith("no") or outcome in ("under",)
            legs.append({
                "mt": b["mt"],
                "is_no": is_no,
                "cost": b["cost"],
                "pnl": b["pnl"],
                "result": b["result"],
                "price": b["avg_price"],
            })

        # Build structure signature
        mt_counts = Counter(l["mt"] for l in legs)
        win_legs = [l for l in legs if l["mt"] == "win"]
        no_wins = sum(1 for l in win_legs if l["is_no"])
        yes_wins = len(win_legs) - no_wins

        # Hedge ratio: how much of win capital is on the "smaller" side?
        win_costs = sorted([l["cost"] for l in win_legs], reverse=True)
        if len(win_costs) >= 2:
            hedge_ratio = sum(win_costs[1:]) / sum(win_costs)
        else:
            hedge_ratio = 0

        # Structure type
        has_draw = "draw" in mt_counts
        has_ou = "ou" in mt_counts
        has_btts = "btts" in mt_counts

        structure = {
            "n_legs": len(legs),
            "win_legs": len(win_legs),
            "no_wins": no_wins,
            "yes_wins": yes_wins,
            "has_draw": has_draw,
            "has_ou": has_ou,
            "has_btts": has_btts,
            "hedge_ratio": hedge_ratio,
            "mt_sig": "+".join(f"{count}×{mt}" for mt, count in sorted(mt_counts.items())),
        }

        games.append({
            "slug": slug,
            "league": league,
            "structure": structure,
            "cost": game_cost,
            "pnl": game_pnl,
            "roi": game_pnl / game_cost if game_cost > 0 else 0,
            "won": game_pnl > 0,
            "legs": legs,
        })

    return {
        "structure_frequency": _structure_frequency(games),
        "hedge_ratio_vs_roi": _hedge_ratio_analysis(games),
        "draw_leg_impact": _draw_leg_impact(games),
        "optimal_leg_count": _optimal_legs(games),
        "winning_structures": _winning_structures(games),
        "rules": _extract_structure_rules(games),
    }


def _structure_frequency(games: list) -> dict:
    """Most common game structures."""
    sigs = Counter(g["structure"]["mt_sig"] for g in games)
    total = len(games)
    return {
        "total_games": total,
        "structures": [
            {
                "signature": sig,
                "count": count,
                "pct": round(count / total, 4),
                "avg_roi": round(
                    statistics.mean(g["roi"] for g in games if g["structure"]["mt_sig"] == sig), 4
                ),
            }
            for sig, count in sigs.most_common(15)
        ],
    }


def _hedge_ratio_analysis(games: list) -> dict:
    """Does hedge ratio predict ROI?"""
    multi_win = [g for g in games if g["structure"]["win_legs"] >= 2]
    if len(multi_win) < 10:
        return {"insufficient_data": True, "multi_win_games": len(multi_win)}

    # Bucket by hedge ratio
    buckets = {
        "no_hedge_0_10": (0, 0.10),
        "light_10_25": (0.10, 0.25),
        "moderate_25_40": (0.25, 0.40),
        "heavy_40_plus": (0.40, 0.51),
    }

    result = {}
    for label, (lo, hi) in buckets.items():
        matching = [g for g in multi_win if lo <= g["structure"]["hedge_ratio"] < hi]
        if not matching:
            continue
        wins = sum(1 for g in matching if g["won"])
        cost = sum(g["cost"] for g in matching)
        pnl = sum(g["pnl"] for g in matching)
        result[label] = {
            "games": len(matching),
            "wr": round(wins / len(matching), 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "avg_hedge_ratio": round(statistics.mean(g["structure"]["hedge_ratio"] for g in matching), 4),
        }

    return result


def _draw_leg_impact(games: list) -> dict:
    """Do games with a draw leg (NO draw) outperform?"""
    with_draw = [g for g in games if g["structure"]["has_draw"]]
    without_draw = [g for g in games if not g["structure"]["has_draw"]]

    def stats(group):
        if not group:
            return {"games": 0}
        wins = sum(1 for g in group if g["won"])
        cost = sum(g["cost"] for g in group)
        pnl = sum(g["pnl"] for g in group)
        return {
            "games": len(group),
            "wr": round(wins / len(group), 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
        }

    return {
        "with_draw_leg": stats(with_draw),
        "without_draw_leg": stats(without_draw),
        "draw_helps": (
            sum(g["pnl"] for g in with_draw) / max(1, sum(g["cost"] for g in with_draw)) >
            sum(g["pnl"] for g in without_draw) / max(1, sum(g["cost"] for g in without_draw))
        ) if with_draw and without_draw else None,
    }


def _optimal_legs(games: list) -> dict:
    """What's the optimal number of legs per game?"""
    by_legs = defaultdict(list)
    for g in games:
        by_legs[g["structure"]["n_legs"]].append(g)

    result = {}
    for n, lg in sorted(by_legs.items()):
        wins = sum(1 for g in lg if g["won"])
        cost = sum(g["cost"] for g in lg)
        pnl = sum(g["pnl"] for g in lg)
        result[f"{n}_legs"] = {
            "games": len(lg),
            "wr": round(wins / len(lg), 4) if lg else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
            "avg_cost": round(statistics.mean(g["cost"] for g in lg), 0),
        }
    return result


def _winning_structures(games: list) -> list:
    """Top 5 most profitable structures by total PnL."""
    by_sig = defaultdict(list)
    for g in games:
        by_sig[g["structure"]["mt_sig"]].append(g)

    ranked = []
    for sig, sg in by_sig.items():
        if len(sg) < 3:
            continue
        cost = sum(g["cost"] for g in sg)
        pnl = sum(g["pnl"] for g in sg)
        wins = sum(1 for g in sg if g["won"])
        ranked.append({
            "structure": sig,
            "games": len(sg),
            "pnl": round(pnl, 2),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "wr": round(wins / len(sg), 4),
        })

    return sorted(ranked, key=lambda x: -x["pnl"])[:10]


def _extract_structure_rules(games: list) -> list:
    """Extract actionable rules about game structure."""
    rules = []

    # Rule 1: Draw leg value
    with_draw = [g for g in games if g["structure"]["has_draw"]]
    without_draw = [g for g in games if not g["structure"]["has_draw"] and g["structure"]["win_legs"] >= 1]
    if len(with_draw) >= 10 and len(without_draw) >= 10:
        wd_roi = sum(g["pnl"] for g in with_draw) / max(1, sum(g["cost"] for g in with_draw))
        wod_roi = sum(g["pnl"] for g in without_draw) / max(1, sum(g["cost"] for g in without_draw))
        rules.append({
            "rule": "DRAW_LEG_VALUE",
            "with_draw_roi": round(wd_roi, 4),
            "without_draw_roi": round(wod_roi, 4),
            "draw_adds_value": wd_roi > wod_roi + 0.03,
            "games_with": len(with_draw),
            "games_without": len(without_draw),
        })

    # Rule 2: Optimal hedge ratio
    multi_win = [g for g in games if g["structure"]["win_legs"] >= 2]
    if len(multi_win) >= 15:
        sorted_by_hr = sorted(multi_win, key=lambda g: g["structure"]["hedge_ratio"])
        thirds = len(sorted_by_hr) // 3
        low_hedge = sorted_by_hr[:thirds]
        high_hedge = sorted_by_hr[-thirds:]
        lh_roi = sum(g["pnl"] for g in low_hedge) / max(1, sum(g["cost"] for g in low_hedge))
        hh_roi = sum(g["pnl"] for g in high_hedge) / max(1, sum(g["cost"] for g in high_hedge))
        optimal_hr = statistics.mean(g["structure"]["hedge_ratio"] for g in multi_win if g["won"])
        rules.append({
            "rule": "OPTIMAL_HEDGE_RATIO",
            "low_hedge_roi": round(lh_roi, 4),
            "high_hedge_roi": round(hh_roi, 4),
            "optimal_ratio": round(optimal_hr, 4),
            "less_hedge_better": lh_roi > hh_roi + 0.03,
        })

    return rules
