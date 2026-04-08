#!/usr/bin/env python3
"""
cannae_historical_recon.py — Reconstrueer Cannae's leg-mix + hauptbet voor RESOLVED games.

Companion to cannae_leg_lookup.py:
  - cannae_leg_lookup.py     → LIVE games via PM /positions API (current open holdings)
  - cannae_historical_recon.py → RESOLVED games via VPS local snapshot (winner known)

Source of truth: /opt/bottie/data/cannae/current_positions.json on VPS (refreshed ~06:00 UTC daily).
This file contains all Cannae open + recently-resolved positions with avgPrice, totalBought,
and curPrice (which becomes 1.0/0.0 after market resolution).

KRITISCHE LES (2026-04-08): NOOIT per-conditionId /activity scrapen voor historical reconstruction.
Eén SSH-call naar dit bestand >> honderden API-calls. Cost: 1 dag verspilde tijd voordat ik
besefte dat de bot dit snapshot al maandenlang elke ochtend maakt.

Win/loss bepaling: curPrice > 0.5 = WIN (na resolution = 1.0), curPrice < 0.5 = LOSS (= 0.0).
Hauptbet bepaling: per (slug, title, outcome) sum totalBought*avgPrice, sort desc, top = hauptbet.
Hypothetical $X copy PnL: WIN → X*(1-avg)/avg, LOSS → -X.

Usage:
  # Run on VPS (reads /opt/bottie/data/cannae/current_positions.json directly)
  python3 scripts/cannae_historical_recon.py nba-min-ind-2026-04-07 nba-chi-was-2026-04-07

  # Run locally (SSH-fetches the file from VPS)
  python3 scripts/cannae_historical_recon.py --remote nba-min-ind-2026-04-07

  # Custom hypothetical stake (default $10)
  python3 scripts/cannae_historical_recon.py --stake 25 nba-uta-nop-2026-04-07
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict

VPS = "root@78.141.222.227"
SNAPSHOT = "/opt/bottie/data/cannae/current_positions.json"


def load_snapshot(remote: bool) -> list:
    if remote:
        out = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", VPS, f"cat {SNAPSHOT}"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            sys.exit(f"SSH fetch failed: {out.stderr}")
        return json.loads(out.stdout)
    try:
        return json.load(open(SNAPSHOT))
    except FileNotFoundError:
        sys.exit(f"{SNAPSHOT} not found locally — use --remote to fetch from VPS")


def reconstruct(positions: list, slugs: list, stake: float) -> dict:
    by_slug = defaultdict(lambda: defaultdict(lambda: {"buy_usd": 0.0, "shares": 0.0, "cur": 0.0}))
    for p in positions:
        s = p.get("eventSlug") or ""
        if s not in slugs:
            continue
        key = (p.get("title", ""), p.get("outcome", ""))
        avg = float(p.get("avgPrice", 0) or 0)
        bought = float(p.get("totalBought", 0) or 0)
        b = by_slug[s][key]
        b["buy_usd"] += bought * avg
        b["shares"] += bought
        b["cur"] = float(p.get("curPrice", 0) or 0)

    games = []
    total_pnl = 0.0
    total_stake = 0.0
    wins = losses = 0
    for slug in slugs:
        legs_raw = by_slug.get(slug, {})
        legs = []
        for (title, outcome), b in legs_raw.items():
            if b["buy_usd"] < 20:
                continue
            avg = b["buy_usd"] / b["shares"] if b["shares"] > 0 else 0
            legs.append({
                "title": title, "outcome": outcome,
                "stake": round(b["buy_usd"], 2), "avg": round(avg, 4),
                "cur": round(b["cur"], 4),
                "won": b["cur"] > 0.5,
            })
        if not legs:
            games.append({"slug": slug, "found": False})
            continue
        legs.sort(key=lambda x: -x["stake"])
        total = sum(l["stake"] for l in legs)
        for l in legs:
            l["share"] = round(l["stake"] / total, 4) if total else 0
        h = legs[0]
        ratio = (1.0 - h["avg"]) / h["avg"] if h["won"] else -1.0
        pnl = stake * ratio
        total_pnl += pnl
        total_stake += stake
        if h["won"]:
            wins += 1
        else:
            losses += 1
        games.append({
            "slug": slug, "found": True, "total_cannae": round(total, 2),
            "n_legs": len(legs), "legs": legs, "hauptbet": h,
            "hypo_stake": stake, "hypo_pnl": round(pnl, 2),
        })

    return {
        "games": games,
        "summary": {
            "n_games": len([g for g in games if g.get("found")]),
            "stake": round(total_stake, 2),
            "pnl": round(total_pnl, 2),
            "roi_pct": round(total_pnl / total_stake * 100, 2) if total_stake else 0,
            "wins": wins, "losses": losses,
        },
    }


def print_human(result: dict) -> None:
    for g in result["games"]:
        if not g.get("found"):
            print(f"\n{g['slug']}: NO DATA")
            continue
        print(f"\n=== {g['slug']}  Cannae total ${g['total_cannae']:,.0f}  ({g['n_legs']} legs) ===")
        for i, l in enumerate(g["legs"][:5]):
            marker = "★" if i == 0 else " "
            print(f"  {marker} ${l['stake']:>9,.0f} ({l['share']*100:5.1f}%)  "
                  f"{l['title'][:42]:42} {l['outcome'][:10]:10} avg@{l['avg']:.3f} cur@{l['cur']:.3f} → "
                  f"{'WIN' if l['won'] else 'LOSS'}")
        h = g["hauptbet"]
        sign = "+" if g["hypo_pnl"] >= 0 else ""
        print(f"  Hypo ${g['hypo_stake']} on hauptbet @{h['avg']:.3f}: {sign}${g['hypo_pnl']:.2f}")
    s = result["summary"]
    print()
    print("=" * 78)
    print(f"$/{s['n_games']} games  stake ${s['stake']:.0f}  PnL ${s['pnl']:+.2f}  "
          f"ROI {s['roi_pct']:+.1f}%  {s['wins']}W/{s['losses']}L")


def main():
    p = argparse.ArgumentParser(description="Reconstruct Cannae's hauptbet on resolved games via VPS local snapshot")
    p.add_argument("slugs", nargs="+", help="Event slugs (e.g. nba-min-ind-2026-04-07)")
    p.add_argument("--remote", action="store_true", help="SSH-fetch snapshot from VPS instead of reading local")
    p.add_argument("--stake", type=float, default=10.0, help="Hypothetical $ stake per game (default $10)")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args()

    positions = load_snapshot(args.remote)
    result = reconstruct(positions, args.slugs, args.stake)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)


if __name__ == "__main__":
    sys.exit(main())
