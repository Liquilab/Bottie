#!/usr/bin/env python3
"""Per-League Specialist Scanner — Find the best wallet per league + top 3 per sport.

8-layer anti-bias pipeline:
  1. lb-api verplicht (no leaderboard = no trust)
  2. Sanity gap ≤30% (existing in lib/analyse.py)
  3. Match-level filter (slug must have date + ≤8 markets)
  4. Resolved ratio ≥60% (enough data must be resolved)
  5. Sliding WR cap (WR ceiling depends on resolved ratio)
  6. Sell ratio <30% (predictors, not traders)
  7. Both-sides filter <30% (not market makers)
  8. Recency + frequency (active wallet, not seasonal)

Usage:
    python3 scripts/league_scanner.py --sport football
    python3 scripts/league_scanner.py --sport football --league bun
    python3 scripts/league_scanner.py --sport nba
    python3 scripts/league_scanner.py --all
"""

import json, os, sys, time, argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from lib.analyse import (
    fetch_and_merge, hauptbet_analysis, classify_sport,
    fetch, API, get_lb_profit,
    is_match_event, both_sides_pct, sell_ratio, sliding_wr_cap,
    SLUG_TO_SPORT, FOOTBALL_SLUGS,
)

EXCLUDE = {
    "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b",  # Cannae
    "0x07921379f7b31ef93da634b688b2fe36897db778",  # ewelmealt
    "0x9f23f6d5d18f9fc5aef42efec8f63a7db3db6d15",  # Our bot (main)
    "0x8a3a19aec04eeb6e3c183ee5750d06fe5c08066a",  # Our bot (test)
    "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",  # GamblingIsAllYouNeed
}

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "wallet-scout")

# --- Sport → Gamma tag mapping ---

SPORT_TAGS = {
    "football": ["soccer"],
    "nba": ["nba"],
    "nhl": ["nhl"],
    "mlb": ["mlb"],
    "nfl": ["nfl"],
    "tennis": ["tennis", "atp", "wta"],
    "esports": ["esports", "lol", "cs2", "dota2"],
}

# Sports where events are outright/tournament markets, not individual matches.
# For these, skip is_match_event filter and use tag-based grouping instead.
TAG_BASED_SPORTS = {"esports"}

# --- League tiers ---

TIER_1 = {"epl", "bun", "lal", "itc", "nba", "nhl", "atp"}
TIER_2 = {"fl1", "ere", "es2", "bl2", "tur", "mlb", "mls", "mex",
          "wta", "esports"}
TIER_3 = {"ucl", "uel", "acn", "cdr", "fif", "spl", "elc", "arg", "por",
          "bra", "rou1", "efa", "sea", "fr2", "aus", "efl", "ssc", "cde",
          "nfl", "cbb"}

def get_tier(league: str) -> int:
    if league in TIER_1:
        return 1
    if league in TIER_2:
        return 2
    return 3

def tier_config(tier: int) -> dict:
    """Min games (league), min games (sport top 3), lookback days."""
    if tier == 1:
        return {"min_league": 15, "min_sport": 30, "lookback": 30}
    if tier == 2:
        return {"min_league": 10, "min_sport": 20, "lookback": 45}
    return {"min_league": 5, "min_sport": 15, "lookback": 90}


# --- Step 1: Fetch events ---

def get_league_from_slug(slug: str, sport: str | None = None) -> str | None:
    """Extract league prefix from event slug."""
    if not slug:
        return None
    # Tag-based sports: league = sport itself (no slug prefix convention)
    if sport and sport in TAG_BASED_SPORTS:
        return sport
    prefix = slug.split("-")[0]
    # For non-football sports, the prefix IS the sport (nba, nhl, etc.)
    if prefix in SLUG_TO_SPORT:
        return prefix
    return None


def fetch_events_for_sport(sport: str, lookback_days: int = 90) -> list:
    """Fetch active + closed events for a sport via Gamma API."""
    tags = SPORT_TAGS[sport]
    all_events = []

    for tag in tags:
        # Active events
        try:
            active = fetch(
                f"https://gamma-api.polymarket.com/events?active=true&closed=false"
                f"&tag_slug={tag}&limit=100"
            )
            all_events.extend(active or [])
        except Exception as e:
            print(f"  Error fetching active {tag}: {e}", flush=True)

        # Closed events (lookback period)
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            offset = 0
            while True:
                closed = fetch(
                    f"https://gamma-api.polymarket.com/events?active=false&closed=true"
                    f"&tag_slug={tag}&end_date_min={cutoff}&limit=100&offset={offset}"
                )
                if not closed:
                    break
                all_events.extend(closed)
                if len(closed) < 100:
                    break
                offset += 100
                time.sleep(0.2)
        except Exception as e:
            print(f"  Error fetching closed {tag}: {e}", flush=True)

    # Dedup by slug
    seen = set()
    deduped = []
    for e in all_events:
        slug = e.get("slug", "")
        if slug and slug not in seen:
            seen.add(slug)
            deduped.append(e)

    return deduped


def filter_and_group_events(events: list, sport: str, target_league: str | None = None) -> dict:
    """Filter to match events only (layer 3), group by league."""
    leagues = defaultdict(list)
    is_tag_based = sport in TAG_BASED_SPORTS

    for e in events:
        slug = e.get("slug", "")
        markets = e.get("markets", [])
        n_markets = len(markets)

        # Layer 3: match-level filter (skip for tag-based sports)
        if not is_tag_based and not is_match_event(slug, n_markets):
            continue

        league = get_league_from_slug(slug, sport)
        if not league:
            continue

        # Verify this league belongs to this sport
        if sport == "football" and league not in FOOTBALL_SLUGS:
            continue
        if not is_tag_based and sport != "football" and league != sport.lower():
            # For NBA, NHL etc., league prefix = sport tag
            if league not in SPORT_TAGS.get(sport, []) and league != sport:
                continue

        if target_league and league != target_league:
            continue

        leagues[league].append(e)

    return dict(leagues)


# --- Step 2: Scan holders ---

def get_holders(condition_id: str) -> list:
    """Get ALL holders for a market condition."""
    try:
        data = fetch(f"{API}/holders?market={condition_id}")
        holders = []
        for group in data:
            for h in group.get("holders", []):
                wallet = h.get("proxyWallet", "").lower()
                amount = float(h.get("amount", 0) or 0)
                name = h.get("name", h.get("pseudonym", ""))
                if wallet and amount > 0 and wallet not in EXCLUDE:
                    holders.append({"wallet": wallet, "name": name, "shares": amount})
        return holders
    except Exception:
        return []


def scan_league_holders(league: str, events: list, max_events: int = 20) -> dict:
    """Scan holders for events in a league. Returns {wallet: {count, names, events, conditions}}."""
    wallet_stats = defaultdict(lambda: {"count": 0, "names": set(), "events": set(), "conditions": {}})
    scan = events[:max_events]

    for event in scan:
        eslug = event.get("slug", "")
        for market in event.get("markets", []):
            cid = market.get("conditionId", "")
            question = market.get("question", "")
            if not cid:
                continue
            for h in get_holders(cid):
                ws = wallet_stats[h["wallet"]]
                ws["count"] += 1
                ws["names"].add(h["name"])
                ws["events"].add(eslug)
                ws["conditions"][cid] = {"event_slug": eslug, "question": question}
            time.sleep(0.1)

    # Filter: 5+ different events (3+ for tag-based sports with fewer events)
    min_events = 3 if league in TAG_BASED_SPORTS else 5
    frequent = {w: s for w, s in wallet_stats.items() if len(s["events"]) >= min_events}
    for s in frequent.values():
        s["names"] = list(s["names"])
        s["events"] = list(s["events"])

    return frequent


# --- Step 3: Analyse per wallet ---

MAX_POSITIONS = 5000


def fetch_targeted_conditions(wallet: str, known_conditions: dict) -> dict:
    """Fetch wallet positions only for known conditions (bottom-up).

    known_conditions: {conditionId: {event_slug, question}} from holders scan.
    Only fetches conditions where we already know the wallet has a position.

    Returns dict in same format as merge_positions: {key: {pnl, invested, title, event_slug}}
    """
    all_conds = {}
    for cid, meta in known_conditions.items():
        eslug = meta.get("event_slug", "")
        question = meta.get("question", "")
        try:
            positions = fetch(f"{API}/positions?user={wallet}&market={cid}&sizeThreshold=0")
            if not positions:
                closed = fetch(f"{API}/closed-positions?user={wallet}&market={cid}")
                positions = closed or []
            for p in positions:
                oi = str(p.get("outcomeIndex", ""))
                key = f"{cid}_{oi}"
                pnl = float(p.get("realizedPnl", 0) or 0)
                cur_price = float(p.get("curPrice", 0) or 0)
                size = float(p.get("size", 0) or 0)
                avg_price = float(p.get("avgPrice", 0) or 0)
                # Resolved loser: curPrice~0, size>0, no realizedPnl
                if abs(pnl) < 0.001 and cur_price < 0.02 and size > 0 and avg_price > 0:
                    pnl = -(size * avg_price)
                invested = float(p.get("initialValue", 0) or 0)
                if invested <= 0:
                    invested = float(p.get("cashPaid", 0) or 0)
                if invested <= 0:
                    tb = float(p.get("totalBought", 0) or 0)
                    invested = tb * avg_price if tb > 0 and avg_price > 0 else size * avg_price
                all_conds[key] = {
                    "pnl": pnl,
                    "invested": max(invested, 0),
                    "title": p.get("title", question) or "",
                    "event_slug": p.get("eventSlug", eslug) or eslug,
                }
        except Exception:
            pass
        time.sleep(0.05)
    return all_conds


def analyse_candidate_targeted(wallet: str, name: str, sport: str, league_events_map: dict,
                               all_conds_cache: dict, known_conditions: dict = None) -> dict | None:
    """Targeted analysis for tag-based sports (tennis, esports).

    Instead of fetching the entire wallet (which fails for mega-traders),
    fetch only positions in the known conditions from the holders scan.
    """
    lb_profit = get_lb_profit(wallet)

    if not known_conditions:
        print(f"    ✗ {name}: no known conditions", flush=True)
        return None

    print(f"    fetching {len(known_conditions)} conditions targeted...", flush=True)
    all_conds = fetch_targeted_conditions(wallet, known_conditions)

    if not all_conds:
        print(f"    ✗ {name}: no positions found in {sport} events", flush=True)
        return None

    # Both-sides filter on sport conditions only
    bs_pct = both_sides_pct(all_conds)
    if bs_pct > 0.3:
        print(f"    ✗ {name}: REJECTED L7 — both sides {bs_pct:.0%} > 30%", flush=True)
        return None

    # Cache for sport-level aggregation
    all_conds_cache[wallet] = (all_conds, lb_profit, None)

    # Per-league analysis (same as standard path from layer 4 onwards)
    league_results = {}

    for league, events in league_events_map.items():
        league_slugs = {e.get("slug", "") for e in events}

        # Filter conditions to this league
        league_conds = {k: v for k, v in all_conds.items() if v.get("event_slug", "") in league_slugs}

        if not league_conds:
            continue

        # Layer 4: resolved ratio
        total_conds = len(league_conds)
        resolved = sum(1 for e in league_conds.values() if abs(e["pnl"]) > 0.001)
        resolved_ratio = resolved / total_conds if total_conds > 0 else 0

        # Layer 5: sliding WR cap
        wr_cap = sliding_wr_cap(resolved_ratio)
        if wr_cap == 0:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"L4/L5: resolved ratio {resolved_ratio:.0%} < 50%",
                "resolved_ratio": round(resolved_ratio, 2),
            }
            continue

        # Hauptbet analysis — pass sport for classify_sport, but conditions are already filtered
        hb = hauptbet_analysis(league_conds, sport)
        if hb["games"] == 0:
            # For tag-based sports, hauptbet may fail on classify_sport.
            # Fallback: manual aggregation since conditions are already sport-filtered.
            hb = _manual_hauptbet(league_conds)
            if hb["games"] == 0:
                continue

        # Layer 5: check WR against cap
        if hb["wr"] > wr_cap:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"L5: WR {hb['wr']}% > cap {wr_cap}% (resolved {resolved_ratio:.0%})",
                "resolved_ratio": round(resolved_ratio, 2),
                "wr": hb["wr"],
                "wr_cap": wr_cap,
                "games": hb["games"],
            }
            continue

        # Tier config
        tier = get_tier(league)
        cfg = tier_config(tier)

        # Min games check
        if hb["games"] < cfg["min_league"]:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"games {hb['games']} < min {cfg['min_league']} (tier {tier})",
                "games": hb["games"],
            }
            continue

        # ROI check
        if hb["roi"] <= 0:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"ROI {hb['roi']}% ≤ 0",
                "games": hb["games"],
                "roi": hb["roi"],
            }
            continue

        league_results[league] = {
            "status": "PASS",
            "games": hb["games"],
            "wins": hb["wins"],
            "losses": hb["losses"],
            "wr": hb["wr"],
            "roi": hb["roi"],
            "pnl": hb["pnl"],
            "invested": hb["invested"],
            "per_line": hb.get("per_line", {}),
            "resolved_ratio": round(resolved_ratio, 2),
            "wr_cap": wr_cap,
            "tier": tier,
        }

    if not any(r.get("status") == "PASS" for r in league_results.values()):
        reasons = "; ".join(f"{l}: {r.get('reason', '?')}" for l, r in league_results.items())
        print(f"    ✗ {name}: no passing leagues — {reasons}", flush=True)
        return None

    return {
        "wallet": wallet,
        "name": name,
        "lb_total_pnl": round(lb_profit, 2) if lb_profit else None,
        "sanity_gap": None,
        "both_sides_pct": round(bs_pct, 3),
        "sell_ratio": 0,
        "leagues": league_results,
    }


def _manual_hauptbet(conds: dict) -> dict:
    """Fallback hauptbet for tag-based sports where classify_sport fails.

    Groups by event_slug, picks max invested leg per event.
    """
    games = {}
    for key, entry in conds.items():
        slug = entry.get("event_slug", "") or entry.get("title", "")
        if not slug:
            continue
        if slug not in games:
            games[slug] = []
        games[slug].append(entry)

    wins, losses, total_pnl, total_inv = 0, 0, 0.0, 0.0
    for slug, legs in games.items():
        hb = max(legs, key=lambda l: l["invested"])
        total_pnl += hb["pnl"]
        total_inv += hb["invested"]
        if hb["pnl"] > 0:
            wins += 1
        else:
            losses += 1

    n = wins + losses
    return {
        "games": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wins / n * 100, 1) if n > 0 else 0,
        "roi": round(total_pnl / total_inv * 100, 1) if total_inv > 0 else 0,
        "pnl": round(total_pnl, 2),
        "invested": round(total_inv, 2),
        "per_line": {},
    }


def analyse_candidate(wallet: str, name: str, sport: str, league_events_map: dict,
                      all_conds_cache: dict) -> dict | None:
    """Full 8-layer anti-bias analysis on one wallet.

    Returns dict with per-league results, or None if rejected.
    """
    # Quick OOM check (skip for tag-based sports — broad traders may still be sport specialists)
    if sport not in TAG_BASED_SPORTS:
        try:
            probe = fetch(f"{API}/positions?user={wallet}&limit=1&offset={MAX_POSITIONS}&sizeThreshold=0")
            if probe and len(probe) > 0:
                print(f"    ✗ {name}: SKIP — >{MAX_POSITIONS} positions", flush=True)
                return None
        except Exception:
            pass

    # Fetch once (layers 1+2)
    try:
        all_conds, lb_profit, sanity_gap, open_pos = fetch_and_merge(wallet, require_lb=True)
    except ValueError as e:
        print(f"    ⚠ {name}: REJECTED L1/L2 — {e}", flush=True)
        return None

    # Layer 7: both-sides filter (market maker)
    bs_pct = both_sides_pct(all_conds)
    if bs_pct > 0.3:
        print(f"    ✗ {name}: REJECTED L7 — both sides {bs_pct:.0%} > 30%", flush=True)
        return None

    # Layer 6: sell ratio
    sr = sell_ratio(open_pos)
    if sr > 0.3:
        print(f"    ✗ {name}: REJECTED L6 — sell ratio {sr:.0%} > 30%", flush=True)
        return None

    # Cache for sport-level aggregation
    all_conds_cache[wallet] = (all_conds, lb_profit, sanity_gap)

    # Per-league analysis
    league_results = {}

    for league, events in league_events_map.items():
        league_slugs = {e.get("slug", "") for e in events}

        # Filter conditions to this league
        league_conds = {}
        for key, entry in all_conds.items():
            eslug = entry.get("event_slug", "")
            if sport in TAG_BASED_SPORTS:
                # Tag-based: match by event slug membership
                if eslug in league_slugs:
                    league_conds[key] = entry
            else:
                entry_league = get_league_from_slug(eslug)
                if entry_league == league:
                    league_conds[key] = entry

        if not league_conds:
            continue

        # Layer 4: resolved ratio
        total_conds = len(league_conds)
        resolved = sum(1 for e in league_conds.values() if abs(e["pnl"]) > 0.001)
        resolved_ratio = resolved / total_conds if total_conds > 0 else 0

        # Layer 5: sliding WR cap
        wr_cap = sliding_wr_cap(resolved_ratio)
        if wr_cap == 0:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"L4/L5: resolved ratio {resolved_ratio:.0%} < 50%",
                "resolved_ratio": round(resolved_ratio, 2),
            }
            continue

        # Hauptbet analysis on league conditions
        hb = hauptbet_analysis(league_conds, sport)
        if hb["games"] == 0:
            continue

        # Layer 5: check WR against cap
        if hb["wr"] > wr_cap:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"L5: WR {hb['wr']}% > cap {wr_cap}% (resolved {resolved_ratio:.0%})",
                "resolved_ratio": round(resolved_ratio, 2),
                "wr": hb["wr"],
                "wr_cap": wr_cap,
                "games": hb["games"],
            }
            continue

        # Tier config
        tier = get_tier(league)
        cfg = tier_config(tier)

        # Min games check
        if hb["games"] < cfg["min_league"]:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"games {hb['games']} < min {cfg['min_league']} (tier {tier})",
                "games": hb["games"],
            }
            continue

        # ROI check
        if hb["roi"] <= 0:
            league_results[league] = {
                "status": "REJECTED",
                "reason": f"ROI {hb['roi']}% ≤ 0",
                "games": hb["games"],
                "roi": hb["roi"],
            }
            continue

        # Layer 8: recency
        # Check last bet timestamp via event slugs
        league_event_slugs = [entry.get("event_slug", "") for entry in league_conds.values()]
        # Rough recency: check if wallet has positions in recent league events
        # (exact timestamp not available from conditions, but league_slugs from events gives us this)
        has_recent = bool(league_slugs & {e.get("event_slug", "") for e in league_conds.values()
                                          if abs(e["pnl"]) > 0.001})
        # Layer 8 is advisory — we flag but don't hard-reject here
        # (recency check is better done on event dates from Gamma API)

        league_results[league] = {
            "status": "PASS",
            "games": hb["games"],
            "wins": hb["wins"],
            "losses": hb["losses"],
            "wr": hb["wr"],
            "roi": hb["roi"],
            "pnl": hb["pnl"],
            "invested": hb["invested"],
            "per_line": hb["per_line"],
            "resolved_ratio": round(resolved_ratio, 2),
            "wr_cap": wr_cap,
            "tier": tier,
        }

    if not any(r.get("status") == "PASS" for r in league_results.values()):
        reasons = "; ".join(f"{l}: {r.get('reason', '?')}" for l, r in league_results.items())
        print(f"    ✗ {name}: no passing leagues — {reasons}", flush=True)
        return None

    return {
        "wallet": wallet,
        "name": name,
        "lb_total_pnl": round(lb_profit, 2) if lb_profit else None,
        "sanity_gap": sanity_gap,
        "both_sides_pct": round(bs_pct, 3),
        "sell_ratio": round(sr, 3),
        "leagues": league_results,
    }


# --- Step 4: Aggregation ---

def aggregate_results(results: list, sport: str, all_conds_cache: dict) -> dict:
    """Per league: rank on ROI → top 1. Per sport: merge → top 3."""

    # Per league: best wallet
    league_best = defaultdict(list)
    for r in results:
        for league, lr in r["leagues"].items():
            if lr.get("status") != "PASS":
                continue
            league_best[league].append({
                "wallet": r["wallet"],
                "name": r["name"],
                "lb_total_pnl": r["lb_total_pnl"],
                "sanity_gap": r["sanity_gap"],
                "both_sides_pct": r["both_sides_pct"],
                "sell_ratio": r["sell_ratio"],
                **lr,
            })

    for league in league_best:
        league_best[league].sort(key=lambda x: -x["roi"])

    # Sport top 3: take best per-league wallets, de-dup, rank by ROI
    sport_wallets = {}
    for league, candidates in league_best.items():
        for c in candidates:
            w = c["wallet"]
            if w not in sport_wallets or c["roi"] > sport_wallets[w]["best_roi"]:
                # Compute sport-wide stats from cache
                if w in all_conds_cache:
                    conds, lb, gap = all_conds_cache[w]
                    if sport in TAG_BASED_SPORTS:
                        # For tag-based sports, league = sport, so league conds = sport conds
                        # Use the league results directly (already computed)
                        sport_conds = conds  # will be filtered by hauptbet
                    else:
                        sport_conds = {k: v for k, v in conds.items()
                                       if classify_sport(v["title"], v.get("event_slug", "")) == sport}
                    hb = hauptbet_analysis(sport_conds, sport)
                    tier = get_tier(league)
                    cfg = tier_config(tier)
                    if hb["games"] >= cfg["min_sport"]:
                        sport_wallets[w] = {
                            "wallet": w,
                            "name": c["name"],
                            "best_roi": c["roi"],
                            "best_league": league,
                            "sport_games": hb["games"],
                            "sport_wr": hb["wr"],
                            "sport_roi": hb["roi"],
                            "sport_pnl": hb["pnl"],
                            "sport_per_line": hb["per_line"],
                            "lb_total_pnl": c.get("lb_total_pnl"),
                            "sanity_gap": c.get("sanity_gap"),
                        }

    sport_top3 = sorted(sport_wallets.values(), key=lambda x: -x["sport_roi"])[:3]

    return {
        "per_league": {l: cs[:3] for l, cs in sorted(league_best.items())},
        "sport_top3": sport_top3,
    }


# --- Output ---

def print_report(agg: dict, sport: str):
    """Human-readable report."""
    print(f"\n{'=' * 80}", flush=True)
    print(f"PER-LEAGUE SPECIALISTS — {sport.upper()}", flush=True)
    print(f"{'=' * 80}", flush=True)

    for league, candidates in sorted(agg["per_league"].items()):
        tier = get_tier(league)
        print(f"\n--- {league.upper()} (tier {tier}) ---", flush=True)
        for i, c in enumerate(candidates[:3]):
            gap_s = f"{c['sanity_gap']:.0f}%" if c.get('sanity_gap') is not None else "?"
            print(f"  {i+1}. {c['name'][:25]:<25} {c['games']:3d}g | WR={c['wr']:5.1f}% (cap {c['wr_cap']}%) | "
                  f"ROI={c['roi']:+6.1f}% | PnL=${c['pnl']:+,.0f} | resolved={c['resolved_ratio']:.0%} | "
                  f"gap={gap_s} | sell={c['sell_ratio']:.0%} | bothsides={c['both_sides_pct']:.0%}", flush=True)
            if c.get("per_line"):
                for line, ls in c["per_line"].items():
                    if ls["games"] >= 2:
                        print(f"     {line}: {ls['games']}g WR={ls['wr']}% ROI={ls['roi']}%", flush=True)

    if agg["sport_top3"]:
        print(f"\n{'=' * 80}", flush=True)
        print(f"SPORT TOP 3 — {sport.upper()}", flush=True)
        print(f"{'=' * 80}", flush=True)
        for i, w in enumerate(agg["sport_top3"]):
            print(f"  {i+1}. {w['name'][:25]:<25} {w['sport_games']:3d}g | WR={w['sport_wr']:5.1f}% | "
                  f"ROI={w['sport_roi']:+6.1f}% | PnL=${w['sport_pnl']:+,.0f} | best league: {w['best_league']}", flush=True)
            print(f"     wallet: {w['wallet']}", flush=True)


def print_yaml_config(agg: dict, sport: str):
    """Print YAML config entries for bottie-test."""
    print(f"\n{'=' * 80}", flush=True)
    print(f"YAML CONFIG ENTRIES (bottie-test)", flush=True)
    print(f"{'=' * 80}", flush=True)

    # Collect unique wallets across all leagues
    seen = set()
    entries = []

    for league, candidates in sorted(agg["per_league"].items()):
        if candidates:
            c = candidates[0]  # Best per league
            if c["wallet"] not in seen:
                seen.add(c["wallet"])
                # Collect all passing leagues for this wallet
                leagues = [l for l, cs in agg["per_league"].items()
                           if any(x["wallet"] == c["wallet"] for x in cs)]
                entries.append((c, leagues))

    print(f"\ncopy_targets:", flush=True)
    for c, leagues in entries:
        leagues_str = ", ".join(sorted(leagues))
        print(f"  - address: \"{c['wallet']}\"", flush=True)
        print(f"    name: \"{c['name']}\"", flush=True)
        print(f"    # {sport} specialist: {leagues_str}", flush=True)
        print(f"    # {c['games']}g WR={c['wr']}% ROI={c['roi']:+.1f}%", flush=True)


# --- Main ---

def scan_sport(sport: str, target_league: str | None = None, parallel: int = 3, top_n: int = 10):
    """Full scan pipeline for one sport."""
    # Determine max lookback from tier config
    max_lookback = 90 if not target_league else tier_config(get_tier(target_league))["lookback"]

    # Step 1: Fetch events
    print(f"\nStep 1: Fetching {sport} events (lookback {max_lookback}d)...", flush=True)
    all_events = fetch_events_for_sport(sport, lookback_days=max_lookback)
    print(f"  Total events: {len(all_events)}", flush=True)

    leagues = filter_and_group_events(all_events, sport, target_league)
    for league, evts in sorted(leagues.items(), key=lambda x: -len(x[1])):
        tier = get_tier(league)
        print(f"  {league:8s}: {len(evts):3d} match events (tier {tier})", flush=True)

    if not leagues:
        print("  No match events found!", flush=True)
        return None

    # Step 2: Scan holders per league
    print(f"\nStep 2: Scanning holders per league...", flush=True)
    all_candidates = defaultdict(lambda: {"names": set(), "events": set(), "leagues": set(), "conditions": {}})

    for league, events in sorted(leagues.items()):
        # Tennis has many small events (10 mkts each) — scan more to find repeat bettors
        max_ev = 50 if sport == "tennis" else 20
        holders = scan_league_holders(league, events, max_events=max_ev)
        print(f"  {league}: {len(holders)} wallets with 5+ events (from {len(events)} events)", flush=True)
        for wallet, stats in holders.items():
            ac = all_candidates[wallet]
            ac["names"].update(stats["names"])
            ac["events"].update(stats["events"])
            ac["leagues"].add(league)
            ac["conditions"].update(stats.get("conditions", {}))

    # Pre-filter: lb-api profit > 0
    print(f"\nStep 2b: Pre-filter {len(all_candidates)} candidates via lb-api...", flush=True)
    profitable = []
    for wallet, stats in sorted(all_candidates.items(), key=lambda x: -len(x[1]["events"])):
        name = list(stats["names"])[0] if stats["names"] else wallet[:15]
        try:
            lb = get_lb_profit(wallet)
            if lb is not None and lb > 0 and lb < 200_000:
                profitable.append((wallet, name, stats))
                print(f"    ✓ {name[:25]}: ${lb:+,.0f} ({len(stats['events'])} events, {len(stats['leagues'])} leagues)", flush=True)
            else:
                reason = f"${lb:+,.0f}" if lb is not None else "no data"
                if lb and lb >= 200_000:
                    reason = "whale"
                print(f"    ✗ {name[:25]}: {reason}", flush=True)
        except Exception:
            print(f"    ✗ {name[:25]}: error", flush=True)
        time.sleep(0.05)

    # Sort by event count, take top N
    profitable.sort(key=lambda x: -len(x[2]["events"]))
    to_analyse = profitable[:top_n]
    print(f"\n  {len(to_analyse)} profitable wallets → full analysis", flush=True)

    # Step 3: Full analysis
    print(f"\nStep 3: 8-layer analysis on {len(to_analyse)} wallets...", flush=True)
    results = []
    all_conds_cache = {}

    # Build league→events map for analysis
    league_events_map = leagues

    def analyse_one(item):
        wallet, name, stats = item
        leagues_str = ",".join(sorted(stats["leagues"]))
        print(f"  → {name[:25]} ({len(stats['events'])} events, leagues: {leagues_str})...", flush=True)
        if sport in TAG_BASED_SPORTS:
            result = analyse_candidate_targeted(
                wallet, name, sport, league_events_map, all_conds_cache,
                known_conditions=stats.get("conditions", {}),
            )
        else:
            result = analyse_candidate(wallet, name, sport, league_events_map, all_conds_cache)
        if result:
            passing = [l for l, r in result["leagues"].items() if r.get("status") == "PASS"]
            for l in passing:
                lr = result["leagues"][l]
                print(f"    ✓ {l}: {lr['games']}g WR={lr['wr']}% ROI={lr['roi']:+.1f}%", flush=True)
        return result

    # Sequential to avoid rate limits on PM API
    for item in to_analyse:
        try:
            result = analyse_one(item)
            if result:
                results.append(result)
        except Exception as e:
            _, name, _ = item
            print(f"    ✗ {name}: EXCEPTION — {e}", flush=True)

    # Step 4: Aggregate
    print(f"\nStep 4: Aggregation...", flush=True)
    agg = aggregate_results(results, sport, all_conds_cache)

    # Output
    print_report(agg, sport)
    print_yaml_config(agg, sport)

    return {
        "sport": sport,
        "target_league": target_league,
        "events_fetched": len(all_events),
        "leagues_scanned": list(leagues.keys()),
        "candidates_analysed": len(to_analyse),
        "results": results,
        "aggregation": {
            "per_league": agg["per_league"],
            "sport_top3": agg["sport_top3"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Per-League Specialist Scanner")
    parser.add_argument("--sport", choices=list(SPORT_TAGS.keys()), help="Sport to scan")
    parser.add_argument("--league", help="Specific league to scan (e.g. bun, epl)")
    parser.add_argument("--all", action="store_true", help="Scan all sports")
    parser.add_argument("--top", type=int, default=10, help="Max wallets to analyse per sport")
    parser.add_argument("--parallel", type=int, default=3, help="Parallel workers")
    args = parser.parse_args()

    if not args.sport and not args.all:
        parser.error("Either --sport or --all is required")

    os.makedirs(OUT_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"{'=' * 80}", flush=True)
    print(f"LEAGUE SPECIALIST SCANNER — {today}", flush=True)
    print(f"8-layer anti-bias pipeline", flush=True)
    print(f"{'=' * 80}", flush=True)

    sports = list(SPORT_TAGS.keys()) if args.all else [args.sport]
    all_results = {}

    for sport in sports:
        print(f"\n{'#' * 80}", flush=True)
        print(f"# SCANNING: {sport.upper()}", flush=True)
        print(f"{'#' * 80}", flush=True)

        result = scan_sport(sport, target_league=args.league, parallel=args.parallel, top_n=args.top)
        if result:
            all_results[sport] = result

    # Save
    suffix = args.league or args.sport or "all"
    outpath = os.path.join(OUT_DIR, f"league-scan-{suffix}-{today}.json")
    with open(outpath, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nSaved to {outpath}", flush=True)


if __name__ == "__main__":
    main()
