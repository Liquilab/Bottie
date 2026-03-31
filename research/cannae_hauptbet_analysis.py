#!/usr/bin/env python3
"""
Cannae Hauptbet Analysis — using positions API (sizeThreshold=1).

Groups positions by event (eventSlug), identifies hauptbet (largest cost position),
calculates WR/ROI per sport for hauptbet-only vs full-game.

NOTE: positions API has known biases:
- Open positions with curPrice=0/1 = resolved but not redeemed
- closed-positions API only returns winners (survivorship bias)
- We combine both for best coverage
"""

import json, sys
from collections import defaultdict

def classify_sport(event_slug):
    if not event_slug:
        return "Other"
    s = event_slug.lower()

    # NBA, NHL, NFL, MLB first (simple prefix)
    if s.startswith("nba-"): return "NBA"
    if s.startswith("nhl-"): return "NHL"
    if s.startswith("nfl-"): return "NFL"
    if s.startswith("mlb-"): return "MLB"
    if s.startswith("ncaa-") or s.startswith("cbb-"): return "NCAAB"
    if s.startswith("atp-") or s.startswith("wta-"): return "Tennis"
    if s.startswith("ufc-") or s.startswith("mma-") or s.startswith("pfl-"): return "MMA"

    # Football = everything else that's a sport league
    football_prefixes = [
        "epl-", "la-liga-", "serie-a-", "bundesliga-", "ligue-1-",
        "champions-league-", "ucl-", "europa-league-", "mls-",
        "eredivisie-", "primeira-liga-", "super-lig-", "copa-",
        "fifa-", "concacaf-", "world-cup-", "euro-", "fa-cup-",
        "carabao-", "coupe-de-france-", "dfb-", "coppa-italia-",
        "copa-del-rey-", "scottish-", "saudi-pro-", "j-league-",
        "k-league-", "a-league-", "liga-mx-", "brasileirao-",
        "superliga-", "ekstraklasa-", "eliteserien-", "allsvenskan-",
        "international-friendly-", "nations-league-", "afcon-",
        "club-friendly-", "usl-", "nwsl-", "conference-league-",
        "arg-", "mex-", "bra-", "rou1-", "es2-", "tur-", "por-",
        "ned-", "bel-", "sui-", "aut-", "cze-", "pol-", "nor-",
        "swe-", "den-", "fin-", "gre-", "ukr-", "rus-", "chi-",
        "col-", "par-", "uru-", "ecu-", "per-", "bol-", "ven-",
        "cro-", "srb-", "hun-", "rom-", "bul-", "isr-", "cyp-",
        "eng-", "sco-", "wal-", "nir-", "irl-", "ice-",
    ]
    for prefix in football_prefixes:
        if s.startswith(prefix):
            return "Football"

    return "Other"

def classify_result(cur_price):
    if cur_price is None:
        return "unknown"
    price = float(cur_price)
    if price >= 0.99:
        return "WIN"
    elif price <= 0.01:
        return "LOSS"
    return "ACTIVE"

def classify_market_type(title):
    t = title.lower()
    if "o/u" in t or "over" in t or "under" in t or "total" in t:
        return "ou"
    if "spread" in t:
        return "spread"
    if "draw" in t:
        return "draw"
    return "win"

def main():
    # Load positions
    with open("/Users/koen/Projects/ Bottie/research/cannae_positions_sz1.json") as f:
        open_pos = json.load(f)
    print(f"Loaded {len(open_pos)} open positions (size>=1)")

    # Also load closed positions (50, winners only — survivorship bias noted)
    import urllib.request, time
    closed_pos = []
    offset = 0
    while True:
        url = f"https://data-api.polymarket.com/closed-positions?user=0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b&limit=500&sortBy=TIMESTAMP&sortOrder=DESC&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "B/1"})
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        if not data:
            break
        closed_pos.extend(data)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.2)
    print(f"Loaded {len(closed_pos)} closed positions")

    # Normalize all positions, dedup by (conditionId, outcome)
    all_positions = []
    seen = set()

    for p in open_pos:
        cid = p.get("conditionId", "")
        outcome = p.get("outcome", "")
        key = (cid, outcome)
        if key in seen:
            continue
        seen.add(key)
        cost = float(p.get("initialValue", 0) or 0)
        all_positions.append({
            "source": "open",
            "title": p.get("title", ""),
            "conditionId": cid,
            "size": float(p.get("size", 0)),
            "cost": cost,
            "avgPrice": float(p.get("avgPrice", 0) or 0),
            "curPrice": float(p.get("curPrice", 0) or 0),
            "outcome": outcome,
            "eventSlug": p.get("eventSlug", ""),
            "cashPnl": float(p.get("cashPnl", 0) or 0),
        })

    for p in closed_pos:
        cid = p.get("conditionId", "")
        outcome = p.get("outcome", "")
        key = (cid, outcome)
        if key in seen:
            continue
        seen.add(key)
        size = float(p.get("totalBought", 0) or 0)
        avg = float(p.get("avgPrice", 0) or 0)
        all_positions.append({
            "source": "closed",
            "title": p.get("title", ""),
            "conditionId": cid,
            "size": size,
            "cost": avg * size if avg > 0 else 0,
            "avgPrice": avg,
            "curPrice": float(p.get("curPrice", 0) or 0),
            "outcome": outcome,
            "eventSlug": p.get("eventSlug", ""),
            "cashPnl": float(p.get("realizedPnl", 0) or 0),
        })

    print(f"Total unique positions: {len(all_positions)}")

    # Group by event
    events = defaultdict(list)
    for p in all_positions:
        key = p["eventSlug"] if p["eventSlug"] else p["title"]
        events[key].append(p)

    print(f"Total unique events: {len(events)}")

    # Analyze per sport
    sport_data = defaultdict(lambda: {
        "events": 0, "resolved": 0, "active": 0,
        "hb_wins": 0, "hb_losses": 0,
        "hb_pnl": 0.0, "hb_cost": 0.0,
        "all_pnl": 0.0, "all_cost": 0.0,
        "hedge_helped": 0, "hedge_hurt": 0, "hedge_neutral": 0,
        "market_types": defaultdict(int),
    })

    for event_key, legs in events.items():
        sport = classify_sport(event_key)
        stats = sport_data[sport]
        stats["events"] += 1

        # Sort by cost (largest investment = hauptbet)
        legs_sorted = sorted(legs, key=lambda x: x["cost"], reverse=True)
        hauptbet = legs_sorted[0]
        other_legs = legs_sorted[1:]

        hb_result = classify_result(hauptbet["curPrice"])
        mt = classify_market_type(hauptbet["title"])
        stats["market_types"][mt] += 1

        # Check if any leg is resolved → event is resolved
        any_resolved = any(classify_result(l["curPrice"]) in ("WIN", "LOSS") for l in legs_sorted)
        all_active = all(classify_result(l["curPrice"]) == "ACTIVE" for l in legs_sorted)

        if all_active:
            stats["active"] += 1
            continue

        stats["resolved"] += 1
        if hb_result == "WIN":
            stats["hb_wins"] += 1
        elif hb_result == "LOSS":
            stats["hb_losses"] += 1
        # If hauptbet is still ACTIVE but other legs resolved, skip for HB WR

        stats["hb_pnl"] += hauptbet["cashPnl"]
        stats["hb_cost"] += hauptbet["cost"]

        all_event_pnl = sum(l["cashPnl"] for l in legs_sorted)
        all_event_cost = sum(l["cost"] for l in legs_sorted)
        stats["all_pnl"] += all_event_pnl
        stats["all_cost"] += all_event_cost

        # Hedge impact
        other_pnl = sum(l["cashPnl"] for l in other_legs)
        if other_pnl > 0:
            stats["hedge_helped"] += 1
        elif other_pnl < 0:
            stats["hedge_hurt"] += 1
        else:
            stats["hedge_neutral"] += 1

    # Print results
    print("\n" + "=" * 140)
    print("  CANNAE HAUPTBET ANALYSIS — Per Sport (resolved events only)")
    print("=" * 140)
    header = f"{'Sport':<12} {'Events':>7} {'Resolved':>9} {'Active':>7} │ {'HB W':>5} {'HB L':>5} {'HB WR':>7} {'HB PnL':>12} {'HB Cost':>12} {'HB ROI':>8} │ {'All PnL':>12} {'All Cost':>12} {'All ROI':>8} │ {'Hedge+':>7} {'Hedge-':>7}"
    print(header)
    print("-" * 140)

    for sport, stats in sorted(sport_data.items(), key=lambda x: x[1]["resolved"], reverse=True):
        resolved = stats["resolved"]
        hb_resolved = stats["hb_wins"] + stats["hb_losses"]
        wr = stats["hb_wins"] / hb_resolved * 100 if hb_resolved > 0 else 0
        hb_roi = stats["hb_pnl"] / stats["hb_cost"] * 100 if stats["hb_cost"] > 0 else 0
        all_roi = stats["all_pnl"] / stats["all_cost"] * 100 if stats["all_cost"] > 0 else 0

        print(f"{sport:<12} {stats['events']:>7} {resolved:>9} {stats['active']:>7} │ "
              f"{stats['hb_wins']:>5} {stats['hb_losses']:>5} {wr:>6.1f}% "
              f"${stats['hb_pnl']:>+11,.0f} ${stats['hb_cost']:>11,.0f} {hb_roi:>+7.1f}% │ "
              f"${stats['all_pnl']:>+11,.0f} ${stats['all_cost']:>11,.0f} {all_roi:>+7.1f}% │ "
              f"{stats['hedge_helped']:>7} {stats['hedge_hurt']:>7}")

    # Market type breakdown
    print("\n" + "=" * 80)
    print("  MARKET TYPE van hauptbet (per sport)")
    print("=" * 80)
    for sport, stats in sorted(sport_data.items(), key=lambda x: x[1]["resolved"], reverse=True):
        if stats["resolved"] == 0:
            continue
        mt = stats["market_types"]
        total = sum(mt.values())
        parts = ", ".join(f"{k}: {v} ({v/total*100:.0f}%)" for k, v in sorted(mt.items(), key=lambda x: -x[1]))
        print(f"  {sport:<12} {parts}")

    # Grand total
    print("\n" + "=" * 80)
    print("  GRAND TOTAL (alle sporten)")
    print("=" * 80)
    t_events = sum(s["events"] for s in sport_data.values())
    t_resolved = sum(s["resolved"] for s in sport_data.values())
    t_active = sum(s["active"] for s in sport_data.values())
    t_hb_w = sum(s["hb_wins"] for s in sport_data.values())
    t_hb_l = sum(s["hb_losses"] for s in sport_data.values())
    t_hb_pnl = sum(s["hb_pnl"] for s in sport_data.values())
    t_hb_cost = sum(s["hb_cost"] for s in sport_data.values())
    t_all_pnl = sum(s["all_pnl"] for s in sport_data.values())
    t_all_cost = sum(s["all_cost"] for s in sport_data.values())
    t_hedge_h = sum(s["hedge_helped"] for s in sport_data.values())
    t_hedge_n = sum(s["hedge_hurt"] for s in sport_data.values())

    t_hb_resolved = t_hb_w + t_hb_l
    wr = t_hb_w / t_hb_resolved * 100 if t_hb_resolved > 0 else 0
    hb_roi = t_hb_pnl / t_hb_cost * 100 if t_hb_cost > 0 else 0
    all_roi = t_all_pnl / t_all_cost * 100 if t_all_cost > 0 else 0

    print(f"  Events: {t_events} total, {t_resolved} resolved, {t_active} active")
    print(f"  Hauptbet: W{t_hb_w}/L{t_hb_l} = {wr:.1f}% WR")
    print(f"  Hauptbet PnL: ${t_hb_pnl:+,.0f} op ${t_hb_cost:,.0f} = {hb_roi:+.1f}% ROI")
    print(f"  Full-game PnL: ${t_all_pnl:+,.0f} op ${t_all_cost:,.0f} = {all_roi:+.1f}% ROI")
    print(f"  Hedge helped: {t_hedge_h}, hurt: {t_hedge_n}")
    print(f"  Hedge netto impact: ${(t_all_pnl - t_hb_pnl):+,.0f} extra PnL op ${(t_all_cost - t_hb_cost):,.0f} extra cost")

if __name__ == "__main__":
    main()
