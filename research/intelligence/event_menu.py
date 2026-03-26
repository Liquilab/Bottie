"""Module 2: Event Selection — what does Cannae CHOOSE vs what is AVAILABLE?"""

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("intelligence.event_menu")

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "CannaeIntel/1.0", "Accept": "application/json"}


def analyze_event_selection(dataset: dict) -> dict:
    """Compare Cannae's selections against the full menu of available events."""
    resolved = dataset["resolved"]
    open_bets = dataset["open_bets"]
    event_cache = dataset["event_cache"]
    all_bets = resolved + open_bets

    # Get all sport events from Gamma API (or fallback to data_lake)
    available = _fetch_sport_events()

    if not available:
        log.warning("No available events data — using event_cache only")
        return _cache_only_analysis(all_bets, event_cache)

    return {
        "menu_size": len(available),
        "selected": len(set(b["event_slug"] for b in all_bets if b["event_slug"])),
        "selection_rate": _selection_rates(all_bets, available),
        "by_league": _selection_by_league(all_bets, available),
        "by_liquidity": _selection_by_liquidity(all_bets, available),
        "by_odds_range": _selection_by_odds(all_bets, available),
        "inferred_filters": _infer_filters(all_bets, available),
    }


def _fetch_sport_events(days_back: int = 30) -> list:
    """Fetch recent sport events from Gamma API."""
    events = []
    try:
        client = httpx.Client(headers=UA, timeout=30)
        # Fetch active + recently closed sport events
        for closed in ["false", "true"]:
            offset = 0
            while offset < 2000:
                url = (
                    f"{GAMMA}/events?tag=sports&closed={closed}"
                    f"&limit=100&offset={offset}&order=startDate&ascending=false"
                )
                resp = client.get(url)
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for e in batch:
                    start = e.get("startDate", "")
                    if start:
                        try:
                            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                            if dt < datetime.now(timezone.utc) - timedelta(days=days_back):
                                break
                        except Exception:
                            pass
                    events.append({
                        "slug": e.get("slug", ""),
                        "title": e.get("title", ""),
                        "league": _detect_league_from_slug(e.get("slug", "")),
                        "start_date": start,
                        "volume": float(e.get("volume", 0) or 0),
                        "liquidity": float(e.get("liquidity", 0) or 0),
                        "closed": e.get("closed", False),
                        "n_markets": len(e.get("markets", [])),
                    })
                if len(batch) < 100:
                    break
                offset += 100
        client.close()
    except Exception as ex:
        log.error(f"Failed to fetch sport events: {ex}")

    log.info(f"Fetched {len(events)} sport events from Gamma API")
    return events


def _detect_league_from_slug(slug: str) -> str:
    if not slug:
        return "unknown"
    return slug.split("-")[0]


def _selection_rates(all_bets: list, available: list) -> dict:
    """Overall selection rate."""
    selected_slugs = set(b["event_slug"] for b in all_bets if b["event_slug"])
    available_slugs = set(e["slug"] for e in available if e["slug"])
    overlap = selected_slugs & available_slugs
    return {
        "available_events": len(available_slugs),
        "selected_events": len(selected_slugs),
        "matched": len(overlap),
        "rate": round(len(overlap) / len(available_slugs), 4) if available_slugs else 0,
    }


def _selection_by_league(all_bets: list, available: list) -> dict:
    """Selection rate per league."""
    selected_by_league = defaultdict(set)
    for b in all_bets:
        if b["event_slug"]:
            selected_by_league[b["league"]].add(b["event_slug"])

    available_by_league = defaultdict(set)
    for e in available:
        if e["slug"]:
            available_by_league[e["league"]].add(e["slug"])

    result = {}
    for league in sorted(set(list(selected_by_league.keys()) + list(available_by_league.keys()))):
        avail = len(available_by_league.get(league, set()))
        sel = len(selected_by_league.get(league, set()))
        if avail == 0 and sel == 0:
            continue
        result[league] = {
            "available": avail,
            "selected": sel,
            "rate": round(sel / avail, 4) if avail > 0 else 0,
        }
    return result


def _selection_by_liquidity(all_bets: list, available: list) -> dict:
    """Selection rate by liquidity bucket."""
    selected_slugs = set(b["event_slug"] for b in all_bets if b["event_slug"])
    buckets = {
        "<1K": (0, 1000),
        "1K-3K": (1000, 3000),
        "3K-10K": (3000, 10000),
        "10K-50K": (10000, 50000),
        "50K+": (50000, float("inf")),
    }
    result = {}
    for label, (lo, hi) in buckets.items():
        in_bucket = [e for e in available if lo <= e["liquidity"] < hi]
        selected = [e for e in in_bucket if e["slug"] in selected_slugs]
        result[label] = {
            "available": len(in_bucket),
            "selected": len(selected),
            "rate": round(len(selected) / len(in_bucket), 4) if in_bucket else 0,
        }
    return result


def _selection_by_odds(all_bets: list, available: list) -> dict:
    """Do we know what odds range Cannae prefers? Based on his entry prices."""
    buckets = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0})
    for b in all_bets:
        if b.get("result") not in ("WIN", "LOSS"):
            continue
        p = b["avg_price"]
        if p < 0.20:
            label = "longshot_0_20"
        elif p < 0.40:
            label = "underdog_20_40"
        elif p < 0.60:
            label = "tossup_40_60"
        elif p < 0.80:
            label = "favorite_60_80"
        else:
            label = "heavy_fav_80_plus"
        buckets[label]["count"] += 1
        if b["result"] == "WIN":
            buckets[label]["wins"] += 1
        else:
            buckets[label]["losses"] += 1

    result = {}
    for label, stats in buckets.items():
        total = stats["wins"] + stats["losses"]
        result[label] = {
            "bets": total,
            "wr": round(stats["wins"] / total, 4) if total > 0 else 0,
            "pct_of_total": round(total / len([b for b in all_bets if b.get("result") in ("WIN", "LOSS")]), 4) if all_bets else 0,
        }
    return result


def _infer_filters(all_bets: list, available: list) -> list:
    """Infer Cannae's selection filters from data."""
    filters = []
    selected_slugs = set(b["event_slug"] for b in all_bets if b["event_slug"])

    # Check minimum liquidity
    selected_events = [e for e in available if e["slug"] in selected_slugs]
    if selected_events:
        min_liq = min(e["liquidity"] for e in selected_events)
        liq_5th = sorted(e["liquidity"] for e in selected_events)[max(0, len(selected_events) // 20)]
        if liq_5th > 500:
            filters.append({
                "filter": "MIN_LIQUIDITY",
                "value": round(liq_5th, 0),
                "description": f"95% of selected events have liquidity >= ${liq_5th:.0f}",
            })

    # Check league concentration
    league_counts = defaultdict(int)
    for b in all_bets:
        league_counts[b["league"]] += 1
    total = sum(league_counts.values())
    if total > 0:
        active_leagues = {k for k, v in league_counts.items() if v / total >= 0.02}
        available_leagues = set(e["league"] for e in available)
        skipped = available_leagues - active_leagues - {"unknown"}
        if skipped:
            filters.append({
                "filter": "LEAGUE_WHITELIST",
                "active_leagues": sorted(active_leagues),
                "skipped_leagues": sorted(list(skipped)[:10]),
                "description": f"Active in {len(active_leagues)} leagues, skips {len(skipped)} available leagues",
            })

    return filters


def _cache_only_analysis(all_bets: list, event_cache: dict) -> dict:
    """Fallback: analyze only from event_cache (no Gamma API)."""
    leagues = defaultdict(int)
    for b in all_bets:
        leagues[b["league"]] += 1

    return {
        "menu_size": "unknown (Gamma API unavailable)",
        "selected": len(set(b["event_slug"] for b in all_bets if b["event_slug"])),
        "by_league_bets": dict(sorted(leagues.items(), key=lambda x: -x[1])),
        "cached_events": len(event_cache),
    }
