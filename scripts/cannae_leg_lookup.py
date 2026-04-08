#!/usr/bin/env python3
"""
cannae_leg_lookup.py — Live SSOT lookup van Cannae's actuele leg-mix per game.

Doel: voor een gegeven event slug, geef terug:
  - alle legs (market_title × outcome) waar Cannae op heeft ingezet
  - totale stake $ per leg
  - leg share (% van totale game stake)
  - hauptbet (leg met hoogste stake)
  - timestamp van eerste/laatste trade

Achtergrond: zonder deze lookup heb je geen betrouwbare grond om te zeggen
"Cannae heeft hoge conviction op leg X". RUS-311 reconciliatie liet zien
dat manual labels op basis van losse snapshots in 6 van de 8 gevallen fout
waren — soms zelfs de tegenovergestelde kant.

Data source: Polymarket data-api /activity endpoint. Geen lokale cache,
geen join met snapshots — altijd live truth.

Usage:
  python3 scripts/cannae_leg_lookup.py nba-cha-bos-2026-04-07
  python3 scripts/cannae_leg_lookup.py nba-uta-nop-2026-04-07 --json
  python3 scripts/cannae_leg_lookup.py nba-mia-tor-2026-04-07 --before 2026-04-07T22:00
"""
import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict

API = "https://data-api.polymarket.com"
CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"


def fetch_activity(wallet: str, limit: int = 500):
    url = f"{API}/activity?user={wallet}&limit={limit}&type=TRADE"
    req = urllib.request.Request(url, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def lookup(slug: str, before_ts: int | None = None) -> dict:
    acts = fetch_activity(CANNAE)
    matches = [
        a for a in acts
        if (a.get("eventSlug") or "").lower() == slug.lower()
        and (before_ts is None or a.get("timestamp", 0) <= before_ts)
    ]

    if not matches:
        return {"slug": slug, "n_trades": 0, "legs": [], "total_stake": 0.0}

    # Group by (market_title, outcome) — that's a "leg"
    legs = defaultdict(lambda: {"stake": 0.0, "shares": 0.0, "n": 0, "first_ts": None, "last_ts": None, "prices": []})
    for a in matches:
        key = (a.get("title", ""), a.get("outcome", ""))
        leg = legs[key]
        leg["stake"] += float(a.get("usdcSize", 0) or 0)
        leg["shares"] += float(a.get("size", 0) or 0)
        leg["n"] += 1
        leg["prices"].append(float(a.get("price", 0) or 0))
        ts = a.get("timestamp", 0)
        leg["first_ts"] = ts if leg["first_ts"] is None else min(leg["first_ts"], ts)
        leg["last_ts"] = ts if leg["last_ts"] is None else max(leg["last_ts"], ts)

    total_stake = sum(l["stake"] for l in legs.values())
    out_legs = []
    for (title, outcome), l in legs.items():
        avg_price = sum(l["prices"]) / len(l["prices"]) if l["prices"] else 0.0
        out_legs.append({
            "market_title": title,
            "outcome": outcome,
            "stake_usdc": round(l["stake"], 2),
            "shares": round(l["shares"], 2),
            "avg_price": round(avg_price, 4),
            "n_trades": l["n"],
            "share_of_game": round(l["stake"] / total_stake, 4) if total_stake else 0.0,
            "first_ts": l["first_ts"],
            "last_ts": l["last_ts"],
        })
    out_legs.sort(key=lambda x: -x["stake_usdc"])

    return {
        "slug": slug,
        "n_trades": len(matches),
        "n_legs": len(out_legs),
        "total_stake": round(total_stake, 2),
        "hauptbet": out_legs[0] if out_legs else None,
        "legs": out_legs,
    }


def print_human(result: dict) -> None:
    print(f"Slug: {result['slug']}")
    if result["n_trades"] == 0:
        print("  Cannae heeft geen trades op deze game.")
        return
    print(f"  Trades: {result['n_trades']}  Legs: {result['n_legs']}  Total stake: ${result['total_stake']:,.2f}")
    print()
    print(f"  {'Market':<45} {'Outcome':<12} {'Stake':>10} {'Share':>7} {'Avg':>6} {'#':>4}")
    print(f"  {'-'*45} {'-'*12} {'-'*10} {'-'*7} {'-'*6} {'-'*4}")
    for i, leg in enumerate(result["legs"]):
        marker = "★" if i == 0 else " "
        print(f"  {marker}{leg['market_title'][:44]:<44} {leg['outcome'][:12]:<12} ${leg['stake_usdc']:>8,.0f} {leg['share_of_game']*100:>6.1f}% {leg['avg_price']:>6.3f} {leg['n_trades']:>4}")
    print()
    h = result["hauptbet"]
    print(f"  Hauptbet: {h['market_title']} / {h['outcome']}  (${h['stake_usdc']:,.0f}, {h['share_of_game']*100:.0f}% van game)")
    if h["first_ts"]:
        print(f"  Eerste trade: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(h['first_ts']))}")


def main():
    p = argparse.ArgumentParser(description="Lookup Cannae's leg-mix voor een game slug")
    p.add_argument("slug", help="Event slug, bv. nba-cha-bos-2026-04-07")
    p.add_argument("--json", action="store_true", help="Output als JSON")
    p.add_argument("--before", help="Alleen trades op of voor deze tijd (ISO of unix ts)")
    args = p.parse_args()

    before_ts = None
    if args.before:
        try:
            before_ts = int(args.before)
        except ValueError:
            before_ts = int(time.mktime(time.strptime(args.before[:19], "%Y-%m-%dT%H:%M:%S")))

    result = lookup(args.slug, before_ts=before_ts)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)


if __name__ == "__main__":
    sys.exit(main())
