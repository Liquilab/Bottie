#!/usr/bin/env python3
"""Crypto Up/Down Scout — finds wallets with edge on 5-min crypto markets.

Scans BTC/ETH/SOL/XRP 5-min up/down markets, finds wallets that:
1. Bet one side only per window (not spread farmers)
2. Buy at 5-95¢ (not dust, not riskless)
3. Win >55% over 20+ bets

Usage:
    python scripts/crypto_scout.py              # default: last 24h
    python scripts/crypto_scout.py --hours 48   # last 48h
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RATE_LIMIT = 0.12
PREFIXES = ["btc-updown-5m", "eth-updown-5m", "sol-updown-5m", "xrp-updown-5m"]


def api_get(url):
    for attempt in range(3):
        try:
            time.sleep(RATE_LIMIT)
            req = urllib.request.Request(url, headers={"User-Agent": "CryptoScout/1", "Accept": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5)
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--min-bets", type=int, default=20)
    parser.add_argument("--min-wr", type=float, default=55.0)
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    cutoff_ts = cutoff.timestamp()

    print(f"Crypto Up/Down Scout — last {args.hours}h", flush=True)
    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M')} UTC", flush=True)
    print(flush=True)

    # Step 1: Find recent resolved crypto 5-min markets from schedule cache
    import subprocess
    try:
        result = subprocess.run(
            ["ssh", "-T", "-o", "ConnectTimeout=10", "root@78.141.222.227",
             "cat /opt/bottie/data/schedule_cache.json"],
            capture_output=True, text=True, timeout=20,
        )
        sched = json.loads(result.stdout)
    except:
        # Fallback: search Gamma API
        sched = []

    # Filter to crypto updown markets
    now = datetime.now(timezone.utc)
    condition_ids = []  # (cid, slug)
    for g in sched:
        slug = g.get("event_slug", "")
        if not any(slug.startswith(p) for p in PREFIXES):
            continue
        try:
            st = datetime.fromisoformat(g["start_time"].replace("Z", "+00:00"))
            if st < cutoff or st > now:
                continue
        except:
            continue
        for cid in g.get("condition_ids", []):
            condition_ids.append((cid, slug))

    # Crypto 5-min markets use timestamp-based slugs: btc-updown-5m-{unix_ts}
    # Generate all 5-min windows in the time range and fetch from Gamma
    if not condition_ids:
        print("Generating crypto 5-min slugs and fetching from Gamma...", flush=True)
        base_ts = int(cutoff.timestamp())
        base_ts = base_ts - (base_ts % 300)  # round to 5-min
        end_ts = int(now.timestamp())
        total_windows = (end_ts - base_ts) // 300

        # Only scan BTC (most liquid), sample every 6th window (every 30 min)
        # to keep API calls manageable. Full scan would be 288 calls/day/coin.
        step = max(1, total_windows // 50)  # ~50 samples
        scanned = 0
        for i in range(0, total_windows, step):
            ts = base_ts + i * 300
            for prefix in ["btc-updown-5m", "eth-updown-5m"]:
                slug = f"{prefix}-{ts}"
                events = api_get(f"{GAMMA_API}/events?slug={slug}")
                if events and isinstance(events, list) and len(events) > 0:
                    for m in events[0].get("markets", []):
                        cid = m.get("conditionId", "")
                        if cid:
                            condition_ids.append((cid, slug))
            scanned += 1
            if scanned % 10 == 0:
                print(f"  {scanned} windows scanned, {len(condition_ids)} markets found...", end="\r", flush=True)
        print(flush=True)

    print(f"Found {len(condition_ids)} crypto 5-min markets", flush=True)

    if not condition_ids:
        print("No markets found. Exiting.", flush=True)
        return

    # Step 2: For each market, fetch trades + resolution
    # wallet -> list of {slug, side, price, won}
    wallet_bets = defaultdict(list)

    for i, (cid, slug) in enumerate(condition_ids):
        if i % 20 == 0:
            print(f"  Scanning {i+1}/{len(condition_ids)}...", end="\r", flush=True)

        # Resolve
        mkt = api_get(f"{CLOB_API}/markets/{cid}")
        if not mkt or not isinstance(mkt, dict):
            continue
        tokens = mkt.get("tokens", [])
        winner = None
        for tok in tokens:
            if tok.get("winner") == True:
                winner = tok.get("outcome")
                break
        if not winner:
            continue

        # Fetch trades
        trades = api_get(f"{DATA_API}/trades?market={cid}&limit=500")
        if not trades or not isinstance(trades, list):
            continue

        for t in trades:
            ts = float(t.get("timestamp", 0))
            if ts < cutoff_ts:
                continue
            side = (t.get("side") or "").upper()
            if side != "BUY":
                continue

            wallet = (t.get("proxyWallet") or "").lower()
            if not wallet:
                continue

            outcome = t.get("outcome", "")
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))

            # Filter: price 5-95¢
            if price < 0.05 or price > 0.95:
                continue

            wallet_bets[wallet].append({
                "slug": slug,
                "outcome": outcome,
                "price": price,
                "size": size,
                "cost": price * size,
                "won": outcome == winner,
            })

    print(f"\n  Found {len(wallet_bets)} wallets with bets in range", flush=True)
    print(flush=True)

    # Step 3: Filter wallets
    results = []
    for wallet, bets in wallet_bets.items():
        # Check: does this wallet bet both sides in the same slug? (spread farmer)
        slugs_sides = defaultdict(set)
        for b in bets:
            slugs_sides[b["slug"]].add(b["outcome"])

        # Count how many slugs have both sides
        both_sides = sum(1 for sides in slugs_sides.values() if len(sides) > 1)
        one_side = sum(1 for sides in slugs_sides.values() if len(sides) == 1)
        total_slugs = len(slugs_sides)

        # Skip if >30% of bets are on both sides (spread farmer)
        if total_slugs > 0 and both_sides / total_slugs > 0.30:
            continue

        # Only count one-side bets for WR
        clean_bets = [b for b in bets if len(slugs_sides[b["slug"]]) == 1]
        if len(clean_bets) < args.min_bets:
            continue

        wins = sum(1 for b in clean_bets if b["won"])
        losses = len(clean_bets) - wins
        wr = wins / len(clean_bets) * 100
        total_cost = sum(b["cost"] for b in clean_bets)
        total_pnl = sum(
            (b["size"] - b["cost"]) if b["won"] else -b["cost"]
            for b in clean_bets
        )
        avg_price = sum(b["price"] for b in clean_bets) / len(clean_bets)
        avg_cost = total_cost / len(clean_bets)

        if wr < args.min_wr:
            continue

        # Coin breakdown
        coin_stats = defaultdict(lambda: {"w": 0, "l": 0})
        for b in clean_bets:
            coin = b["slug"].split("-")[0]  # btc, eth, sol, xrp
            if b["won"]:
                coin_stats[coin]["w"] += 1
            else:
                coin_stats[coin]["l"] += 1

        results.append({
            "wallet": wallet,
            "bets": len(clean_bets),
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 1),
            "total_cost": round(total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "roi": round(total_pnl / total_cost * 100, 1) if total_cost > 0 else 0,
            "avg_price": round(avg_price, 3),
            "avg_cost": round(avg_cost, 2),
            "both_sides_pct": round(both_sides / total_slugs * 100, 0) if total_slugs > 0 else 0,
            "coins": {k: f"{v['w']}W/{v['l']}L" for k, v in coin_stats.items()},
        })

    results.sort(key=lambda r: (-r["wr"], -r["bets"]))

    print(f"{'Wallet':<14s} {'Bets':>4s} {'W':>3s} {'L':>3s} {'WR':>5s} {'Cost':>7s} {'PnL':>8s} {'ROI':>6s} {'AvgP':>5s} {'2side%':>6s} {'Coins'}", flush=True)
    print("-" * 95, flush=True)
    for r in results[:30]:
        coins = " ".join(f"{k}:{v}" for k, v in r["coins"].items())
        print(f"{r['wallet'][:12]:<14s} {r['bets']:>4d} {r['wins']:>3d} {r['losses']:>3d} {r['wr']:>4.0f}% ${r['total_cost']:>6.0f} ${r['total_pnl']:>+7.0f} {r['roi']:>+5.0f}% {r['avg_price']:>4.0%} {r['both_sides_pct']:>5.0f}% {coins}", flush=True)

    print(f"\nTotal wallets with >{args.min_wr}% WR, >={args.min_bets} bets, 5-95¢, one-side: {len(results)}", flush=True)

    # Save
    out_file = os.path.join(os.path.dirname(__file__), "..", "data", "crypto_scout.json")
    with open(out_file, "w") as f:
        json.dump({
            "hours": args.hours,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "markets_scanned": len(condition_ids),
            "wallets_scanned": len(wallet_bets),
            "results": results[:50],
        }, f, indent=2)
    print(f"Saved to {out_file}", flush=True)


if __name__ == "__main__":
    main()
