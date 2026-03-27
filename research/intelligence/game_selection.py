"""Module 10: Game Selection Model — WHAT makes Cannae select a game?

Not just which leagues (event_menu.py covers that), but within a league:
1. What game characteristics predict selection? (volume, odds balance, team strength)
2. What does Cannae SKIP and why?
3. Can we predict which games Cannae will bet on tomorrow?
"""

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("intelligence.game_selection")


def analyze_game_selection(dataset: dict) -> dict:
    """Analyze what makes Cannae select specific games."""
    resolved = dataset["resolved"]
    open_bets = dataset.get("open_bets", [])
    event_cache = dataset.get("event_cache", {})
    all_bets = resolved + open_bets

    if len(all_bets) < 20:
        return {"insufficient_data": True}

    # Group bets by event
    by_event = defaultdict(list)
    for b in all_bets:
        slug = b.get("event_slug", "")
        if slug:
            by_event[slug].append(b)

    games = []
    for slug, bets in by_event.items():
        meta = event_cache.get(slug, {})
        game_cost = sum(b["cost"] for b in bets)
        league = bets[0]["league"]
        n_legs = len(set(b["cid"] for b in bets))
        market_types = list(set(b["mt"] for b in bets))
        avg_price = statistics.mean(b["avg_price"] for b in bets) if bets else 0

        # Timing: how far before start did Cannae enter?
        start_str = meta.get("start_date", "")
        hours_before = None
        if start_str and bets[0].get("first_ts"):
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                entry = datetime.fromtimestamp(bets[0]["first_ts"], tz=timezone.utc)
                hours_before = (start - entry).total_seconds() / 3600
            except Exception:
                pass

        resolved_bets = [b for b in bets if b.get("result") in ("WIN", "LOSS")]
        wins = sum(1 for b in resolved_bets if b["result"] == "WIN")
        losses = sum(1 for b in resolved_bets if b["result"] == "LOSS")
        pnl = sum(b["pnl"] for b in resolved_bets)

        games.append({
            "slug": slug,
            "league": league,
            "cost": game_cost,
            "n_legs": n_legs,
            "market_types": market_types,
            "avg_price": avg_price,
            "hours_before": hours_before,
            "volume": float(meta.get("volume", 0) or 0),
            "wins": wins,
            "losses": losses,
            "pnl": pnl,
            "roi": pnl / game_cost if game_cost > 0 else 0,
            "resolved": len(resolved_bets) > 0,
        })

    return {
        "total_games": len(games),
        "by_size": _games_by_size(games),
        "by_leg_count": _games_by_legs(games),
        "by_entry_timing": _games_by_timing(games),
        "by_avg_price": _games_by_price(games),
        "league_selection_depth": _league_depth(games),
        "selection_signals": _extract_selection_signals(games),
    }


def _games_by_size(games: list) -> dict:
    """ROI by Cannae's total investment per game."""
    buckets = {
        "micro_0_100": (0, 100),
        "small_100_500": (100, 500),
        "medium_500_2k": (500, 2000),
        "large_2k_10k": (2000, 10000),
        "whale_10k_plus": (10000, float("inf")),
    }
    return _bucket_stats(games, lambda g: g["cost"], buckets)


def _games_by_legs(games: list) -> dict:
    """ROI by number of legs per game."""
    buckets = {
        "1_leg": (1, 2),
        "2_legs": (2, 3),
        "3_4_legs": (3, 5),
        "5_plus_legs": (5, 100),
    }
    return _bucket_stats(games, lambda g: g["n_legs"], buckets)


def _games_by_timing(games: list) -> dict:
    """ROI by entry timing (hours before game start)."""
    timed = [g for g in games if g["hours_before"] is not None]
    if not timed:
        return {"no_timing_data": True}

    buckets = {
        "last_hour": (0, 1),
        "1_6h": (1, 6),
        "6_24h": (6, 24),
        "1_3_days": (24, 72),
        "3_plus_days": (72, float("inf")),
    }
    return _bucket_stats(timed, lambda g: g["hours_before"], buckets)


def _games_by_price(games: list) -> dict:
    """ROI by average entry price."""
    buckets = {
        "cheap_0_30": (0, 0.30),
        "value_30_50": (0.30, 0.50),
        "mid_50_70": (0.50, 0.70),
        "fav_70_90": (0.70, 0.90),
        "heavy_90_plus": (0.90, 1.0),
    }
    return _bucket_stats(games, lambda g: g["avg_price"], buckets)


def _league_depth(games: list) -> dict:
    """How deep does Cannae go within each league?"""
    by_league = defaultdict(list)
    for g in games:
        by_league[g["league"]].append(g)

    result = {}
    for league, lg_games in sorted(by_league.items(), key=lambda x: -len(x[1])):
        if len(lg_games) < 3:
            continue
        costs = [g["cost"] for g in lg_games]
        resolved = [g for g in lg_games if g["resolved"]]
        pnl = sum(g["pnl"] for g in resolved)
        cost = sum(g["cost"] for g in resolved)
        result[league] = {
            "games": len(lg_games),
            "total_invested": round(sum(costs), 0),
            "avg_per_game": round(statistics.mean(costs), 0),
            "max_per_game": round(max(costs), 0),
            "avg_legs": round(statistics.mean(g["n_legs"] for g in lg_games), 1),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
        }
    return result


def _extract_selection_signals(games: list) -> list:
    """Extract predictive signals for game selection."""
    signals = []
    resolved_games = [g for g in games if g["resolved"]]
    if len(resolved_games) < 20:
        return [{"signal": "INSUFFICIENT_DATA"}]

    # Signal 1: Does game size predict ROI?
    sorted_by_cost = sorted(resolved_games, key=lambda g: g["cost"])
    mid = len(sorted_by_cost) // 2
    small = sorted_by_cost[:mid]
    large = sorted_by_cost[mid:]
    small_roi = sum(g["pnl"] for g in small) / max(1, sum(g["cost"] for g in small))
    large_roi = sum(g["pnl"] for g in large) / max(1, sum(g["cost"] for g in large))
    signals.append({
        "signal": "SIZE_PREDICTS_ROI",
        "small_half_roi": round(small_roi, 4),
        "large_half_roi": round(large_roi, 4),
        "bigger_is_better": large_roi > small_roi + 0.05,
    })

    # Signal 2: Does more legs = better?
    multi = [g for g in resolved_games if g["n_legs"] >= 3]
    single = [g for g in resolved_games if g["n_legs"] <= 2]
    if multi and single:
        multi_roi = sum(g["pnl"] for g in multi) / max(1, sum(g["cost"] for g in multi))
        single_roi = sum(g["pnl"] for g in single) / max(1, sum(g["cost"] for g in single))
        signals.append({
            "signal": "MULTI_LEG_EFFECT",
            "multi_leg_roi": round(multi_roi, 4),
            "multi_leg_games": len(multi),
            "single_leg_roi": round(single_roi, 4),
            "single_leg_games": len(single),
            "more_legs_better": multi_roi > single_roi,
        })

    # Signal 3: Early entry vs late entry
    timed = [g for g in resolved_games if g["hours_before"] is not None]
    if len(timed) >= 10:
        early = [g for g in timed if g["hours_before"] > 12]
        late = [g for g in timed if g["hours_before"] <= 12]
        if early and late:
            early_roi = sum(g["pnl"] for g in early) / max(1, sum(g["cost"] for g in early))
            late_roi = sum(g["pnl"] for g in late) / max(1, sum(g["cost"] for g in late))
            signals.append({
                "signal": "ENTRY_TIMING",
                "early_12h_plus_roi": round(early_roi, 4),
                "late_under_12h_roi": round(late_roi, 4),
                "early_is_better": early_roi > late_roi + 0.03,
            })

    return signals


def _bucket_stats(items: list, key_fn, buckets: dict) -> dict:
    """Generic bucket analysis."""
    result = {}
    for label, (lo, hi) in buckets.items():
        matching = [g for g in items if lo <= key_fn(g) < hi]
        if not matching:
            continue
        resolved = [g for g in matching if g["resolved"]]
        wins = sum(g["wins"] for g in resolved)
        losses = sum(g["losses"] for g in resolved)
        cost = sum(g["cost"] for g in resolved)
        pnl = sum(g["pnl"] for g in resolved)
        result[label] = {
            "games": len(matching),
            "resolved": len(resolved),
            "wr": round(wins / (wins + losses), 4) if (wins + losses) > 0 else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
            "avg_cost": round(statistics.mean(g["cost"] for g in matching), 0),
        }
    return result
