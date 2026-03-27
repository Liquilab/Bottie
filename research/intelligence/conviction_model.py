"""Module 9: Conviction Model — HOW does Cannae determine conviction?

Analyzes YES/NO ratio per conditionId to understand:
1. When does Cannae go all-in (>90% one side) vs hedge (50/50)?
2. What predicts high conviction? (price, league, game size)
3. Does higher conviction = better ROI?
4. Pattern: 2×NO (draw play), YES+NO same team (conviction), mixed (hedge)
"""

import logging
import statistics
from collections import defaultdict

log = logging.getLogger("intelligence.conviction")


def analyze_conviction(dataset: dict) -> dict:
    """Full conviction analysis using game-level data."""
    games = dataset.get("games", [])
    resolved = dataset["resolved"]
    if len(resolved) < 30:
        return {"insufficient_data": True, "n": len(resolved)}

    # Build game-level conviction from resolved bets
    game_conviction = _build_game_conviction(resolved)

    return {
        "conviction_distribution": _conviction_distribution(game_conviction),
        "conviction_vs_roi": _conviction_vs_roi(game_conviction),
        "conviction_vs_price": _conviction_vs_price(game_conviction),
        "conviction_vs_league": _conviction_vs_league(game_conviction),
        "conviction_patterns": _conviction_patterns(game_conviction),
        "hedge_structures": _hedge_structures(game_conviction),
        "rules": _extract_conviction_rules(game_conviction),
    }


def _build_game_conviction(resolved: list) -> list:
    """Group resolved bets by event_slug + conditionId to compute conviction.

    Conviction = largest_side_usdc / (largest + second) per conditionId.
    """
    # Group by event_slug
    by_event = defaultdict(list)
    for b in resolved:
        slug = b.get("event_slug", "")
        if slug:
            by_event[slug].append(b)

    games = []
    for slug, bets in by_event.items():
        # Group by conditionId within event
        by_cid = defaultdict(list)
        for b in bets:
            by_cid[b["cid"]].append(b)

        game_total = sum(b["cost"] for b in bets)
        league = bets[0]["league"] if bets else ""

        legs = []
        for cid, cid_bets in by_cid.items():
            # Sort by cost descending
            sorted_bets = sorted(cid_bets, key=lambda x: x["cost"], reverse=True)
            best = sorted_bets[0]
            second = sorted_bets[1] if len(sorted_bets) > 1 else None

            best_cost = best["cost"]
            second_cost = second["cost"] if second else 0
            total_cid = best_cost + second_cost
            conviction = best_cost / total_cid if total_cid > 0 else 1.0

            # Detect if both sides are same direction
            best_outcome = best.get("outcome", "").lower()
            second_outcome = second.get("outcome", "").lower() if second else ""
            same_direction = (
                best_outcome == second_outcome or
                (best_outcome.startswith("no") and second_outcome.startswith("no")) or
                (best_outcome.startswith("yes") and second_outcome.startswith("yes"))
            )

            legs.append({
                "cid": cid,
                "mt": best["mt"],
                "conviction": conviction,
                "best_cost": best_cost,
                "second_cost": second_cost,
                "best_outcome": best.get("outcome", ""),
                "avg_price": best["avg_price"],
                "result": best["result"],
                "pnl": best["pnl"],
                "title": best.get("title", ""),
                "same_direction": same_direction,
                "is_no_bet": best_outcome.startswith("no") or best_outcome.lower() in ("under",),
                "weight": best_cost / game_total if game_total > 0 else 0,
            })

        if not legs:
            continue

        # Game-level conviction: weighted average
        total_leg_cost = sum(l["best_cost"] for l in legs)
        avg_conviction = sum(l["conviction"] * l["best_cost"] for l in legs) / total_leg_cost if total_leg_cost > 0 else 0

        # Detect game pattern
        win_legs = [l for l in legs if l["mt"] == "win"]
        no_bets = [l for l in win_legs if l["is_no_bet"]]
        yes_bets = [l for l in win_legs if not l["is_no_bet"]]

        if len(no_bets) >= 2 and len(yes_bets) == 0:
            pattern = "DOUBLE_NO"  # draw/defensive play
        elif len(win_legs) >= 2 and all(l["conviction"] > 0.85 for l in win_legs):
            pattern = "STRONG_CONVICTION"
        elif any(l["conviction"] < 0.60 for l in win_legs):
            pattern = "HEDGE"
        elif len(win_legs) == 1:
            pattern = "SINGLE_LEG"
        else:
            pattern = "MIXED"

        game_won = any(l["result"] == "WIN" for l in legs)
        game_pnl = sum(l["pnl"] for l in legs)
        game_cost = sum(l["best_cost"] for l in legs)

        games.append({
            "slug": slug,
            "league": league,
            "legs": legs,
            "n_legs": len(legs),
            "game_total": game_total,
            "avg_conviction": avg_conviction,
            "pattern": pattern,
            "game_won": game_won,
            "game_pnl": game_pnl,
            "game_cost": game_cost,
            "game_roi": game_pnl / game_cost if game_cost > 0 else 0,
        })

    return games


def _conviction_distribution(games: list) -> dict:
    """Distribution of conviction levels."""
    if not games:
        return {}

    convictions = [g["avg_conviction"] for g in games]
    buckets = {
        "very_high_90_100": [c for c in convictions if c >= 0.90],
        "high_75_90": [c for c in convictions if 0.75 <= c < 0.90],
        "medium_60_75": [c for c in convictions if 0.60 <= c < 0.75],
        "low_50_60": [c for c in convictions if 0.50 <= c < 0.60],
    }

    result = {}
    for label, vals in buckets.items():
        matching_games = [g for g in games if g["avg_conviction"] in vals]
        wins = sum(1 for g in matching_games if g["game_won"])
        n = len(matching_games)
        pnl = sum(g["game_pnl"] for g in matching_games)
        cost = sum(g["game_cost"] for g in matching_games)
        result[label] = {
            "games": n,
            "pct": round(n / len(games), 4) if games else 0,
            "wr": round(wins / n, 4) if n > 0 else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
        }

    return result


def _conviction_vs_roi(games: list) -> dict:
    """Does higher conviction predict better ROI?"""
    if len(games) < 10:
        return {"insufficient_data": True}

    sorted_games = sorted(games, key=lambda g: g["avg_conviction"])
    mid = len(sorted_games) // 2
    low_conv = sorted_games[:mid]
    high_conv = sorted_games[mid:]

    def stats(group, label):
        n = len(group)
        wins = sum(1 for g in group if g["game_won"])
        cost = sum(g["game_cost"] for g in group)
        pnl = sum(g["game_pnl"] for g in group)
        return {
            "games": n,
            "wr": round(wins / n, 4) if n > 0 else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "avg_conviction": round(statistics.mean(g["avg_conviction"] for g in group), 4),
            "pnl": round(pnl, 2),
        }

    return {
        "low_conviction": stats(low_conv, "low"),
        "high_conviction": stats(high_conv, "high"),
        "conviction_predicts_roi": (
            sum(g["game_pnl"] for g in high_conv) / max(1, sum(g["game_cost"] for g in high_conv)) >
            sum(g["game_pnl"] for g in low_conv) / max(1, sum(g["game_cost"] for g in low_conv))
        ),
    }


def _conviction_vs_price(games: list) -> dict:
    """Relationship between entry price and conviction."""
    if not games:
        return {}

    all_legs = [l for g in games for l in g["legs"] if l["mt"] == "win"]
    if len(all_legs) < 10:
        return {"insufficient_data": True}

    price_buckets = {
        "cheap_0_30": [l for l in all_legs if l["avg_price"] < 0.30],
        "mid_30_60": [l for l in all_legs if 0.30 <= l["avg_price"] < 0.60],
        "fav_60_80": [l for l in all_legs if 0.60 <= l["avg_price"] < 0.80],
        "heavy_80_plus": [l for l in all_legs if l["avg_price"] >= 0.80],
    }

    result = {}
    for label, legs in price_buckets.items():
        if not legs:
            continue
        result[label] = {
            "legs": len(legs),
            "avg_conviction": round(statistics.mean(l["conviction"] for l in legs), 4),
            "pct_high_conviction": round(sum(1 for l in legs if l["conviction"] > 0.85) / len(legs), 4),
        }
    return result


def _conviction_vs_league(games: list) -> dict:
    """Conviction patterns per league."""
    by_league = defaultdict(list)
    for g in games:
        by_league[g["league"]].append(g)

    result = {}
    for league, lg_games in sorted(by_league.items(), key=lambda x: -len(x[1])):
        if len(lg_games) < 5:
            continue
        n = len(lg_games)
        avg_conv = statistics.mean(g["avg_conviction"] for g in lg_games)
        avg_legs = statistics.mean(g["n_legs"] for g in lg_games)
        wins = sum(1 for g in lg_games if g["game_won"])
        cost = sum(g["game_cost"] for g in lg_games)
        pnl = sum(g["game_pnl"] for g in lg_games)
        patterns = defaultdict(int)
        for g in lg_games:
            patterns[g["pattern"]] += 1

        result[league] = {
            "games": n,
            "avg_conviction": round(avg_conv, 4),
            "avg_legs": round(avg_legs, 1),
            "wr": round(wins / n, 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "patterns": dict(patterns),
        }
    return result


def _conviction_patterns(games: list) -> dict:
    """Analysis of game patterns: DOUBLE_NO, STRONG_CONVICTION, HEDGE, etc."""
    by_pattern = defaultdict(list)
    for g in games:
        by_pattern[g["pattern"]].append(g)

    result = {}
    for pattern, pg in sorted(by_pattern.items(), key=lambda x: -len(x[1])):
        n = len(pg)
        wins = sum(1 for g in pg if g["game_won"])
        cost = sum(g["game_cost"] for g in pg)
        pnl = sum(g["game_pnl"] for g in pg)
        result[pattern] = {
            "games": n,
            "pct": round(n / len(games), 4) if games else 0,
            "wr": round(wins / n, 4) if n > 0 else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
            "avg_legs": round(statistics.mean(g["n_legs"] for g in pg), 1),
            "avg_game_total": round(statistics.mean(g["game_total"] for g in pg), 0),
        }
    return result


def _hedge_structures(games: list) -> dict:
    """Analyze how Cannae constructs hedges within games."""
    structures = defaultdict(int)

    for g in games:
        win_legs = [l for l in g["legs"] if l["mt"] == "win"]
        draw_legs = [l for l in g["legs"] if l["mt"] == "draw"]
        ou_legs = [l for l in g["legs"] if l["mt"] == "ou"]

        # Construct signature
        parts = []
        if win_legs:
            no_wins = sum(1 for l in win_legs if l["is_no_bet"])
            yes_wins = len(win_legs) - no_wins
            if no_wins: parts.append("{}×NO_win".format(no_wins))
            if yes_wins: parts.append("{}×YES_win".format(yes_wins))
        if draw_legs:
            no_draws = sum(1 for l in draw_legs if l["is_no_bet"])
            yes_draws = len(draw_legs) - no_draws
            if no_draws: parts.append("{}×NO_draw".format(no_draws))
            if yes_draws: parts.append("{}×YES_draw".format(yes_draws))
        if ou_legs:
            parts.append("{}×ou".format(len(ou_legs)))

        sig = " + ".join(parts) if parts else "empty"
        structures[sig] += 1

    # Sort by frequency
    sorted_structures = sorted(structures.items(), key=lambda x: -x[1])

    return {
        "total_games": len(games),
        "unique_structures": len(structures),
        "top_structures": [
            {"structure": sig, "count": count, "pct": round(count / len(games), 4)}
            for sig, count in sorted_structures[:15]
        ],
    }


def _extract_conviction_rules(games: list) -> list:
    """Extract actionable rules about conviction."""
    rules = []

    if len(games) < 20:
        return [{"rule": "INSUFFICIENT_DATA", "n": len(games)}]

    # Rule 1: Does conviction predict ROI?
    sorted_games = sorted(games, key=lambda g: g["avg_conviction"])
    q1 = sorted_games[:len(sorted_games) // 4]
    q4 = sorted_games[3 * len(sorted_games) // 4:]
    q1_roi = sum(g["game_pnl"] for g in q1) / max(1, sum(g["game_cost"] for g in q1))
    q4_roi = sum(g["game_pnl"] for g in q4) / max(1, sum(g["game_cost"] for g in q4))

    rules.append({
        "rule": "CONVICTION_SIGNAL",
        "q1_low_conviction_roi": round(q1_roi, 4),
        "q4_high_conviction_roi": round(q4_roi, 4),
        "conviction_matters": q4_roi > q1_roi + 0.05,
        "description": "High conviction games outperform low conviction by {:.0%}".format(q4_roi - q1_roi) if q4_roi > q1_roi else "Conviction does NOT predict ROI",
    })

    # Rule 2: DOUBLE_NO pattern performance
    double_no = [g for g in games if g["pattern"] == "DOUBLE_NO"]
    conviction_games = [g for g in games if g["pattern"] == "STRONG_CONVICTION"]
    if len(double_no) >= 5 and len(conviction_games) >= 5:
        dn_roi = sum(g["game_pnl"] for g in double_no) / max(1, sum(g["game_cost"] for g in double_no))
        sc_roi = sum(g["game_pnl"] for g in conviction_games) / max(1, sum(g["game_cost"] for g in conviction_games))
        rules.append({
            "rule": "DOUBLE_NO_VS_CONVICTION",
            "double_no_roi": round(dn_roi, 4),
            "double_no_games": len(double_no),
            "conviction_roi": round(sc_roi, 4),
            "conviction_games": len(conviction_games),
            "prefer": "DOUBLE_NO" if dn_roi > sc_roi else "CONVICTION",
        })

    # Rule 3: Minimum conviction threshold
    for threshold in [0.60, 0.70, 0.80, 0.90]:
        above = [g for g in games if g["avg_conviction"] >= threshold]
        below = [g for g in games if g["avg_conviction"] < threshold]
        if len(above) >= 10 and len(below) >= 10:
            above_roi = sum(g["game_pnl"] for g in above) / max(1, sum(g["game_cost"] for g in above))
            below_roi = sum(g["game_pnl"] for g in below) / max(1, sum(g["game_cost"] for g in below))
            if above_roi > below_roi + 0.10:
                rules.append({
                    "rule": "MIN_CONVICTION_THRESHOLD",
                    "threshold": threshold,
                    "above_roi": round(above_roi, 4),
                    "above_games": len(above),
                    "below_roi": round(below_roi, 4),
                    "below_games": len(below),
                })
                break  # Take the first significant threshold

    return rules
