"""Module 12: Predictive Model — Given a game + odds, predict what Cannae would do.

This is the ENDGAME module. If this works, we can trade independently of Cannae.

Approach: rule-based model from extracted patterns (not ML — too little data).
1. League filter: is this league in Cannae's whitelist?
2. Timing: does Cannae bet on this league at this time?
3. Side selection: which team/outcome does Cannae prefer?
4. Sizing: how much would Cannae bet?
5. Conviction: how confident is the prediction?

Calibration: backtest against known Cannae trades.
"""

import logging
import statistics
from collections import defaultdict

log = logging.getLogger("intelligence.predictive")


def build_predictive_model(dataset: dict) -> dict:
    """Build a rule-based predictive model from Cannae's history."""
    resolved = dataset["resolved"]
    open_bets = dataset.get("open_bets", [])
    all_bets = resolved + open_bets

    if len(resolved) < 50:
        return {"insufficient_data": True, "n": len(resolved)}

    # Extract patterns
    league_profile = _league_profile(all_bets)
    side_profile = _side_selection_profile(resolved)
    sizing_profile = _sizing_profile(resolved)
    price_profile = _price_preference(resolved)

    # Backtest: how well do our rules predict known trades?
    backtest = _backtest_rules(resolved, league_profile, side_profile, price_profile)

    return {
        "league_profile": league_profile,
        "side_profile": side_profile,
        "sizing_profile": sizing_profile,
        "price_profile": price_profile,
        "backtest": backtest,
        "model_rules": _generate_rules(league_profile, side_profile, sizing_profile, price_profile),
    }


def _league_profile(all_bets: list) -> dict:
    """Which leagues does Cannae trade and with what intensity?"""
    by_league = defaultdict(lambda: {"bets": 0, "cost": 0, "events": set()})
    for b in all_bets:
        d = by_league[b["league"]]
        d["bets"] += 1
        d["cost"] += b["cost"]
        d["events"].add(b.get("event_slug", ""))

    total_cost = sum(d["cost"] for d in by_league.values())
    result = {}
    for league, d in sorted(by_league.items(), key=lambda x: -x[1]["cost"]):
        if d["bets"] < 3:
            continue
        result[league] = {
            "bets": d["bets"],
            "events": len(d["events"]),
            "total_cost": round(d["cost"], 0),
            "pct_of_portfolio": round(d["cost"] / total_cost, 4) if total_cost > 0 else 0,
            "avg_per_event": round(d["cost"] / len(d["events"]), 0) if d["events"] else 0,
            "active": d["cost"] / total_cost > 0.01 if total_cost > 0 else False,
        }

    return result


def _side_selection_profile(resolved: list) -> dict:
    """How does Cannae choose sides? Favorite vs underdog, YES vs NO."""
    win_bets = [b for b in resolved if b["mt"] == "win"]

    if not win_bets:
        return {}

    # YES vs NO preference
    yes_bets = [b for b in win_bets if not b.get("outcome", "").lower().startswith("no")]
    no_bets = [b for b in win_bets if b.get("outcome", "").lower().startswith("no")]

    def side_stats(bets, label):
        if not bets:
            return {"count": 0}
        wins = sum(1 for b in bets if b["result"] == "WIN")
        cost = sum(b["cost"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)
        return {
            "count": len(bets),
            "wr": round(wins / len(bets), 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "avg_price": round(statistics.mean(b["avg_price"] for b in bets), 4),
            "avg_cost": round(statistics.mean(b["cost"] for b in bets), 0),
        }

    # Favorite (price > 0.55) vs underdog (price < 0.45)
    fav_bets = [b for b in win_bets if b["avg_price"] > 0.55]
    dog_bets = [b for b in win_bets if b["avg_price"] < 0.45]
    mid_bets = [b for b in win_bets if 0.45 <= b["avg_price"] <= 0.55]

    return {
        "yes_bets": side_stats(yes_bets, "YES"),
        "no_bets": side_stats(no_bets, "NO"),
        "favorite_bets": side_stats(fav_bets, "favorite"),
        "underdog_bets": side_stats(dog_bets, "underdog"),
        "tossup_bets": side_stats(mid_bets, "tossup"),
        "prefers_no": len(no_bets) > len(yes_bets) * 1.2,
        "prefers_favorites": sum(b["cost"] for b in fav_bets) > sum(b["cost"] for b in dog_bets) * 1.5 if dog_bets else False,
    }


def _sizing_profile(resolved: list) -> dict:
    """How does Cannae size? Fixed shares, proportional, or conviction-based?"""
    if not resolved:
        return {}

    costs = [b["cost"] for b in resolved]
    shares = [b.get("shares", 0) for b in resolved if b.get("shares", 0) > 0]

    # Check if share sizes cluster (fixed share sizing)
    share_buckets = defaultdict(int)
    for s in shares:
        rounded = round(s / 50) * 50  # round to nearest 50
        share_buckets[rounded] += 1

    # Check correlation: does price predict size? (Cannae might bet more at better prices)
    price_size_pairs = [(b["avg_price"], b["cost"]) for b in resolved if b["cost"] > 0]

    result = {
        "avg_cost": round(statistics.mean(costs), 2),
        "median_cost": round(statistics.median(costs), 2),
        "stdev_cost": round(statistics.stdev(costs), 2) if len(costs) > 1 else 0,
        "cost_range": [round(min(costs), 2), round(max(costs), 2)],
    }

    if shares:
        result["avg_shares"] = round(statistics.mean(shares), 0)
        result["median_shares"] = round(statistics.median(shares), 0)
        top_share_sizes = sorted(share_buckets.items(), key=lambda x: -x[1])[:5]
        result["common_share_sizes"] = [{"shares": s, "count": c} for s, c in top_share_sizes]

    # Size by league
    by_league = defaultdict(list)
    for b in resolved:
        by_league[b["league"]].append(b["cost"])
    result["size_by_league"] = {
        lg: round(statistics.mean(costs), 0)
        for lg, costs in sorted(by_league.items(), key=lambda x: -statistics.mean(x[1]))
        if len(costs) >= 5
    }

    return result


def _price_preference(resolved: list) -> dict:
    """What price ranges does Cannae prefer and perform best at?"""
    win_bets = [b for b in resolved if b["mt"] == "win"]
    if not win_bets:
        return {}

    buckets = {
        "0_20ct": (0, 0.20),
        "20_40ct": (0.20, 0.40),
        "40_60ct": (0.40, 0.60),
        "60_80ct": (0.60, 0.80),
        "80_95ct": (0.80, 0.95),
    }

    result = {}
    for label, (lo, hi) in buckets.items():
        matching = [b for b in win_bets if lo <= b["avg_price"] < hi]
        if not matching:
            continue
        wins = sum(1 for b in matching if b["result"] == "WIN")
        cost = sum(b["cost"] for b in matching)
        pnl = sum(b["pnl"] for b in matching)
        result[label] = {
            "bets": len(matching),
            "wr": round(wins / len(matching), 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
            "pct_volume": round(cost / sum(b["cost"] for b in win_bets), 4) if win_bets else 0,
        }

    # Sweet spot
    best_bucket = max(result.items(), key=lambda x: x[1]["roi"]) if result else None
    if best_bucket:
        result["sweet_spot"] = best_bucket[0]

    return result


def _backtest_rules(resolved: list, league_profile: dict, side_profile: dict, price_profile: dict) -> dict:
    """Backtest: given Cannae's patterns, how many trades could we have predicted?

    Simple rule-based prediction:
    1. Is the league active? (>1% of portfolio)
    2. Is the price in the sweet spot?
    3. Does the NO/YES preference match?
    """
    if len(resolved) < 30:
        return {"insufficient_data": True}

    active_leagues = {k for k, v in league_profile.items() if v.get("active", False)}
    prefers_no = side_profile.get("prefers_no", False)
    sweet_spot = price_profile.get("sweet_spot", "")

    # Parse sweet spot bounds
    spot_bounds = {
        "0_20ct": (0, 0.20),
        "20_40ct": (0.20, 0.40),
        "40_60ct": (0.40, 0.60),
        "60_80ct": (0.60, 0.80),
        "80_95ct": (0.80, 0.95),
    }
    spot_lo, spot_hi = spot_bounds.get(sweet_spot, (0, 1))

    correct = 0
    total = 0
    for b in resolved:
        total += 1
        # Would we have predicted this trade?
        league_match = b["league"] in active_leagues
        is_no = b.get("outcome", "").lower().startswith("no")
        side_match = (is_no == prefers_no) or not prefers_no
        price_match = spot_lo <= b["avg_price"] < spot_hi

        # Score: how many rules match?
        score = sum([league_match, side_match, price_match])
        if score >= 2:
            correct += 1

    return {
        "total_trades": total,
        "predicted_correct": correct,
        "accuracy": round(correct / total, 4) if total > 0 else 0,
        "rules_used": {
            "active_leagues": len(active_leagues),
            "prefers_no": prefers_no,
            "sweet_spot": sweet_spot,
        },
    }


def _generate_rules(league_profile, side_profile, sizing_profile, price_profile) -> list:
    """Generate human-readable prediction rules."""
    rules = []

    # Rule 1: League selection
    active = sorted(
        [(k, v) for k, v in league_profile.items() if v.get("active")],
        key=lambda x: -x[1]["pct_of_portfolio"]
    )
    if active:
        top5 = [f"{k} ({v['pct_of_portfolio']:.0%})" for k, v in active[:5]]
        rules.append({
            "rule": "LEAGUE_FILTER",
            "description": f"Cannae trades in {len(active)} leagues. Top 5: {', '.join(top5)}",
            "active_leagues": [k for k, _ in active],
        })

    # Rule 2: Side preference
    if side_profile.get("prefers_no"):
        no_pct = side_profile.get("no_bets", {}).get("count", 0)
        yes_pct = side_profile.get("yes_bets", {}).get("count", 0)
        rules.append({
            "rule": "SIDE_PREFERENCE",
            "description": f"Cannae prefers NO bets ({no_pct} vs {yes_pct} YES). NO = safer, covers draw.",
        })

    # Rule 3: Price sweet spot
    if price_profile.get("sweet_spot"):
        spot = price_profile["sweet_spot"]
        spot_data = price_profile.get(spot, {})
        rules.append({
            "rule": "PRICE_SWEET_SPOT",
            "description": f"Best ROI at {spot}: ROI={spot_data.get('roi', 0):.0%}, {spot_data.get('bets', 0)} bets",
            "sweet_spot": spot,
        })

    # Rule 4: Sizing pattern
    if sizing_profile.get("size_by_league"):
        top_league = max(sizing_profile["size_by_league"].items(), key=lambda x: x[1])
        rules.append({
            "rule": "SIZING_BY_LEAGUE",
            "description": f"Biggest bets in {top_league[0]} (avg ${top_league[1]:.0f}). Overall avg ${sizing_profile.get('avg_cost', 0):.0f}.",
        })

    return rules


def predict_game(game_info: dict, model: dict) -> dict:
    """Given a game, predict what Cannae would do.

    Args:
        game_info: {"league": str, "teams": [str, str], "odds": {team: prob}, "volume": float}
        model: output of build_predictive_model()

    Returns: prediction with confidence
    """
    league = game_info.get("league", "")
    teams = game_info.get("teams", [])
    odds = game_info.get("odds", {})

    # Step 1: Would Cannae bet on this league?
    league_data = model.get("league_profile", {}).get(league, {})
    if not league_data.get("active", False):
        return {
            "action": "SKIP",
            "reason": f"League {league} not in Cannae's active set",
            "confidence": 0.9,
        }

    # Step 2: Which side?
    side_profile = model.get("side_profile", {})
    price_profile = model.get("price_profile", {})

    # Find the team with odds in the sweet spot
    sweet_spot = price_profile.get("sweet_spot", "40_60ct")
    spot_bounds = {
        "0_20ct": (0, 0.20), "20_40ct": (0.20, 0.40), "40_60ct": (0.40, 0.60),
        "60_80ct": (0.60, 0.80), "80_95ct": (0.80, 0.95),
    }
    spot_lo, spot_hi = spot_bounds.get(sweet_spot, (0.40, 0.60))

    predictions = []
    for team, prob in odds.items():
        in_sweet_spot = spot_lo <= prob < spot_hi
        # Cannae often bets NO (1-prob), which has a higher implied probability
        no_prob = 1 - prob
        no_in_sweet_spot = spot_lo <= no_prob < spot_hi

        if in_sweet_spot:
            predictions.append({
                "side": f"YES {team}",
                "price": round(prob, 2),
                "confidence": 0.6 if not side_profile.get("prefers_no") else 0.3,
            })
        if no_in_sweet_spot:
            predictions.append({
                "side": f"NO {team}",
                "price": round(no_prob, 2),
                "confidence": 0.7 if side_profile.get("prefers_no") else 0.4,
            })

    if not predictions:
        return {
            "action": "SKIP",
            "reason": "No odds in sweet spot",
            "confidence": 0.5,
        }

    best = max(predictions, key=lambda p: p["confidence"])
    return {
        "action": "BET",
        "prediction": best,
        "all_candidates": predictions,
        "league_strength": league_data.get("pct_of_portfolio", 0),
    }
