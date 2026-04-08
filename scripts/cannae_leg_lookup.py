#!/usr/bin/env python3
"""
cannae_leg_lookup.py — Live SSOT lookup van Cannae's actuele leg-mix per game.

Doel: voor een gegeven event slug, geef terug:
  - alle legs (market_title × outcome) waar Cannae nu in zit
  - totale stake $ per leg (uit /positions, current holdings)
  - leg share (% van totale game stake)
  - hauptbet (leg met hoogste stake)
  - cluster-count (hoeveel logische orders, niet ruwe fills)

Achtergrond: zonder deze lookup heb je geen betrouwbare grond om te zeggen
"Cannae heeft hoge conviction op leg X". RUS-311 reconciliatie liet zien
dat manual labels op basis van losse snapshots in 6 van de 8 gevallen fout
waren — soms zelfs de tegenovergestelde kant.

KRITISCHE LES (2026-04-08): /activity?limit=500 capt fills, niet trades.
Cannae gebruikt GTC ask-1 ladders die in honderden 1-share fills uiteenvallen.
Voor high-volume events miste de oude script-versie >50% van de stake omdat
oudere fills buiten het 500-record window vielen. Fix:
  - PRIMARY source = /positions (current holdings, accurate stake $)
  - /activity is alleen voor cluster-display + first/last timing
  - Fills clusteren per (conditionId × outcome × price-bucket) → "logische orders"

Usage:
  python3 scripts/cannae_leg_lookup.py ucl-fcb1-atm1-2026-04-08
  python3 scripts/cannae_leg_lookup.py ucl-fcb1-atm1-2026-04-08 --json
"""
import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict

API = "https://data-api.polymarket.com"
CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"


def fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch_positions(wallet: str, threshold: float = 0.01):
    return fetch(f"{API}/positions?user={wallet}&limit=500&sizeThreshold={threshold}")


def fetch_activity(wallet: str, limit: int = 500):
    return fetch(f"{API}/activity?user={wallet}&limit={limit}&type=TRADE")


def cluster_fills(fills: list, price_decimals: int = 2) -> list:
    """
    Cluster GTC ask-1 fills into logical orders.
    Two fills are in the same cluster if:
      - same conditionId + outcome
      - same price (rounded to N decimals)
    Returns list of clusters with {price, n_fills, total_stake, total_shares, first_ts, last_ts}.
    """
    buckets = defaultdict(lambda: {"n_fills": 0, "stake": 0.0, "shares": 0.0, "first_ts": None, "last_ts": None})
    for f in fills:
        cid = f.get("conditionId", "")
        out = f.get("outcome", "")
        price = round(float(f.get("price", 0) or 0), price_decimals)
        key = (cid, out, price)
        b = buckets[key]
        b["n_fills"] += 1
        b["stake"] += float(f.get("usdcSize", 0) or 0)
        b["shares"] += float(f.get("size", 0) or 0)
        ts = f.get("timestamp", 0)
        b["first_ts"] = ts if b["first_ts"] is None else min(b["first_ts"], ts)
        b["last_ts"] = ts if b["last_ts"] is None else max(b["last_ts"], ts)
    out = []
    for (cid, outcome, price), b in buckets.items():
        out.append({"conditionId": cid, "outcome": outcome, "price": price, **b})
    return out


def lookup(slug: str) -> dict:
    """
    Build leg-mix from /positions (source of truth for current $ stake)
    + cluster /activity for fill-pattern visibility.
    """
    slug_lc = slug.lower()

    # PRIMARY: positions endpoint — accurate current stake
    positions = fetch_positions(CANNAE, threshold=0.01)
    pos_for_event = [p for p in positions if (p.get("eventSlug") or "").lower() == slug_lc]

    # Activity for cluster info + leg discovery (in case position was sold but recently traded)
    acts = fetch_activity(CANNAE)
    fills_for_event = [a for a in acts if (a.get("eventSlug") or "").lower() == slug_lc]

    if not pos_for_event and not fills_for_event:
        return {"slug": slug, "n_legs": 0, "total_stake": 0.0, "legs": [], "warning": "No positions or activity found"}

    # Build legs from positions (truth)
    legs = []
    for p in pos_for_event:
        avg = float(p.get("avgPrice", 0) or 0)
        bought = float(p.get("totalBought", 0) or 0)
        stake = bought * avg
        size = float(p.get("size", 0) or 0)
        cur = float(p.get("curPrice", 0) or 0)
        cid = p.get("conditionId", "")

        # Find related fills for this conditionId+outcome to compute clusters
        leg_fills = [f for f in fills_for_event
                     if f.get("conditionId") == cid
                     and (f.get("outcome") or "").lower() == (p.get("outcome") or "").lower()]
        clusters = cluster_fills(leg_fills) if leg_fills else []

        legs.append({
            "market_title": p.get("title", ""),
            "outcome": p.get("outcome", ""),
            "conditionId": cid,
            "stake_usdc": round(stake, 2),
            "avg_price": round(avg, 4),
            "cur_price": round(cur, 4),
            "shares_held": round(size, 2),
            "shares_bought": round(bought, 2),
            "n_fills_visible": len(leg_fills),
            "n_clusters": len(clusters),
            "clusters": sorted(clusters, key=lambda c: -c["stake"]),
        })

    # Sometimes there are recent fills on legs not in current /positions (sold or below threshold)
    # Add those as zero-position phantom legs for completeness
    pos_keys = {(p.get("conditionId"), (p.get("outcome") or "").lower()) for p in pos_for_event}
    extra_fills = [f for f in fills_for_event if (f.get("conditionId"), (f.get("outcome") or "").lower()) not in pos_keys]
    if extra_fills:
        by_leg = defaultdict(list)
        for f in extra_fills:
            by_leg[(f.get("conditionId"), f.get("title", ""), f.get("outcome", ""))].append(f)
        for (cid, title, outcome), fls in by_leg.items():
            clusters = cluster_fills(fls)
            stake = sum(float(f.get("usdcSize", 0) or 0) for f in fls)
            legs.append({
                "market_title": title,
                "outcome": outcome,
                "conditionId": cid,
                "stake_usdc": round(stake, 2),
                "avg_price": None,
                "cur_price": None,
                "shares_held": 0.0,
                "shares_bought": None,
                "n_fills_visible": len(fls),
                "n_clusters": len(clusters),
                "clusters": sorted(clusters, key=lambda c: -c["stake"]),
                "note": "no current position (sold or below threshold)",
            })

    legs.sort(key=lambda x: -x["stake_usdc"])
    total_stake = sum(l["stake_usdc"] for l in legs)
    for l in legs:
        l["share_of_game"] = round(l["stake_usdc"] / total_stake, 4) if total_stake else 0.0

    return {
        "slug": slug,
        "n_legs": len(legs),
        "total_stake": round(total_stake, 2),
        "hauptbet": legs[0] if legs else None,
        "legs": legs,
        "data_sources": {
            "positions_count": len(pos_for_event),
            "activity_fills_visible": len(fills_for_event),
            "activity_capped": len(acts) >= 500,
        },
    }


def print_human(result: dict) -> None:
    print(f"Slug: {result['slug']}")
    ds = result.get("data_sources", {})
    if ds.get("activity_capped"):
        print(f"  ⚠️  /activity is capped at 500 records — older fills may be missing from cluster view")
        print(f"     Stake $ comes from /positions (truth), clusters are from {ds.get('activity_fills_visible',0)} visible fills only")
    if result["n_legs"] == 0:
        print("  Cannae heeft geen open positie of recent activiteit op deze game.")
        return
    print(f"  Legs: {result['n_legs']}  Total stake: ${result['total_stake']:,.2f}")
    print()
    print(f"  {'Market':<45} {'Outcome':<14} {'Stake':>10} {'Share':>7} {'Avg':>6} {'Cur':>6} {'Clusters':>9}")
    print(f"  {'-'*45} {'-'*14} {'-'*10} {'-'*7} {'-'*6} {'-'*6} {'-'*9}")
    for i, leg in enumerate(result["legs"]):
        marker = "★" if i == 0 else " "
        avg = f"{leg['avg_price']:.3f}" if leg.get("avg_price") is not None else "  —  "
        cur = f"{leg['cur_price']:.3f}" if leg.get("cur_price") is not None else "  —  "
        cluster_info = f"{leg['n_clusters']}/{leg['n_fills_visible']}f"
        print(f"  {marker}{leg['market_title'][:44]:<44} {leg['outcome'][:14]:<14} ${leg['stake_usdc']:>8,.0f} {leg['share_of_game']*100:>6.1f}% {avg:>6} {cur:>6} {cluster_info:>9}")
        if leg.get("note"):
            print(f"     ↳ {leg['note']}")
    print()
    h = result["hauptbet"]
    print(f"  Hauptbet: {h['market_title']} / {h['outcome']}  (${h['stake_usdc']:,.0f}, {h['share_of_game']*100:.0f}% van game)")
    if h.get("clusters"):
        print(f"  Hauptbet clusters (price → stake):")
        for c in h["clusters"][:10]:
            ts_first = time.strftime("%m-%d %H:%M", time.gmtime(c["first_ts"])) if c["first_ts"] else "?"
            print(f"    @{c['price']:.3f}  ${c['stake']:>8,.0f}  ({c['n_fills']} fills, first {ts_first})")


def main():
    p = argparse.ArgumentParser(description="Lookup Cannae's leg-mix voor een game slug")
    p.add_argument("slug", help="Event slug, bv. ucl-fcb1-atm1-2026-04-08")
    p.add_argument("--json", action="store_true", help="Output als JSON")
    args = p.parse_args()
    result = lookup(args.slug)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_human(result)


if __name__ == "__main__":
    sys.exit(main())
