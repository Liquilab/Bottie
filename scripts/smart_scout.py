#!/usr/bin/env python3
"""Smart Wallet Scout — finds profitable wallets via Data API.

Scans recent trades on Polymarket sports markets, groups by wallet,
computes ROI + conviction sizing, and ranks by actionable edge.

Only considers wallets active in the last 7 days.
Output: data/smart_scout.json + stdout summary.

Usage:
    python scripts/smart_scout.py                    # all football
    python scripts/smart_scout.py --sport nba        # NBA
    python scripts/smart_scout.py --days 14          # 14-day window
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
RATE_LIMIT = 0.12

FOOTBALL_PREFIXES = {
    "epl", "bun", "lal", "fl1", "sea", "ucl", "uel", "ere", "por",
    "elc", "es2", "mex", "arg", "bra", "aus", "tur", "mls", "fif",
    "ukr1", "rus", "fr2", "col", "den", "spl", "bl2",
}
NBA_PREFIXES = {"nba", "cbb"}

def api_get(url):
    time.sleep(RATE_LIMIT)
    req = urllib.request.Request(url, headers={"User-Agent": "Scout/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch_recent_market_trades(condition_id, limit=200):
    """Fetch recent trades on a market from Data API."""
    try:
        return api_get(f"{DATA_API}/trades?market={condition_id}&limit={limit}")
    except:
        return []


def resolve_market(condition_id):
    """Check if market resolved and who won."""
    try:
        mkt = api_get(f"{CLOB_API}/markets/{condition_id}")
        tokens = mkt.get("tokens", [])
        for tok in tokens:
            if tok.get("winner") == True:
                return tok.get("outcome")
        return None  # not resolved
    except:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default="football", choices=["football", "nba"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--min-games", type=int, default=5)
    parser.add_argument("--min-cost", type=float, default=50, help="Min total cost to consider wallet")
    args = parser.parse_args()

    prefixes = FOOTBALL_PREFIXES if args.sport == "football" else NBA_PREFIXES
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    cutoff_ts = cutoff.timestamp()

    print(f"Smart Scout — {args.sport} — last {args.days} days")
    print(f"Cutoff: {cutoff.date()}")
    print()

    # Step 1: Get condition_ids from VPS schedule cache (has all games with cids)
    print("Loading schedule cache from VPS...")

    condition_ids = []  # (cid, slug, question)
    seen_slugs = set()

    import subprocess
    try:
        result = subprocess.run(
            ["ssh", "-T", "-o", "ConnectTimeout=10", "root@78.141.222.227",
             "cat /opt/bottie/data/schedule_cache.json"],
            capture_output=True, text=True, timeout=20,
        )
        sched = json.loads(result.stdout)
    except Exception as ex:
        print(f"  Error loading schedule: {ex}")
        sched = []

    now = datetime.now(timezone.utc)
    for g in sched:
        slug = g.get("event_slug", "")
        sport = slug.split("-")[0]
        if sport not in prefixes:
            continue
        if "-more-markets" in slug:
            continue
        st = g.get("start_time", "")
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if dt < cutoff:
                continue
            if dt > now:
                continue  # not yet played
        except:
            continue
        seen_slugs.add(slug)
        for cid in g.get("condition_ids", []):
            condition_ids.append((cid, slug, ""))

    print(f"Found {len(condition_ids)} markets across {len(seen_slugs)} events")
    print()

    # Step 2: For each market, fetch trades and collect per-wallet stats
    # wallet -> {cid -> {cost, shares, outcome, won}}
    wallet_trades = defaultdict(lambda: defaultdict(lambda: {
        "cost": 0, "shares": 0, "outcome": "", "slug": "", "question": ""
    }))

    for i, (cid, slug, question) in enumerate(condition_ids):
        if i % 20 == 0:
            print(f"  Scanning market {i+1}/{len(condition_ids)}...", end="\r", flush=True)

        # Resolve
        winner = resolve_market(cid)
        if winner is None:
            continue  # not resolved yet

        # Fetch trades
        trades = fetch_recent_market_trades(cid, limit=500)

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

            wt = wallet_trades[wallet][cid]
            wt["cost"] += price * size
            wt["shares"] += size
            wt["outcome"] = outcome
            wt["slug"] = slug
            wt["question"] = question
            wt["winner"] = winner
            wt["won"] = (outcome == winner)

    print(f"\n  Scanned {len(condition_ids)} markets, found {len(wallet_trades)} wallets")
    print()

    # Step 3: Aggregate per wallet
    results = []
    for wallet, positions in wallet_trades.items():
        games = set()
        total_cost = 0
        total_pnl = 0
        wins = 0
        losses = 0
        costs_list = []  # per-position costs for median split

        for cid, pos in positions.items():
            if pos["cost"] < 0.01:
                continue
            slug = pos["slug"]
            games.add(slug)
            total_cost += pos["cost"]
            won = pos["won"]
            if won:
                pnl = pos["shares"] - pos["cost"]
                wins += 1
            else:
                pnl = -pos["cost"]
                losses += 1
            total_pnl += pnl
            costs_list.append((pos["cost"], won))

        n_games = len(games)
        n_trades = wins + losses

        if n_trades < args.min_games:
            continue
        if total_cost < args.min_cost:
            continue

        # Conviction split: WR above/below median bet size
        if costs_list:
            costs_list.sort(key=lambda x: x[0])
            median_cost = costs_list[len(costs_list) // 2][0]
            above = [c for c in costs_list if c[0] >= median_cost]
            below = [c for c in costs_list if c[0] < median_cost]
            wr_above = sum(1 for _, w in above if w) / len(above) * 100 if above else 0
            wr_below = sum(1 for _, w in below if w) / len(below) * 100 if below else 0
        else:
            wr_above = wr_below = 0
            median_cost = 0

        wr = wins / n_trades * 100
        roi = total_pnl / total_cost * 100 if total_cost > 0 else 0
        avg_cost = total_cost / n_trades

        results.append({
            "wallet": wallet,
            "games": n_games,
            "trades": n_trades,
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 1),
            "total_cost": round(total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "roi": round(roi, 1),
            "avg_cost": round(avg_cost, 2),
            "median_cost": round(median_cost, 2),
            "wr_above_median": round(wr_above, 1),
            "wr_below_median": round(wr_below, 1),
            "conviction_gap": round(wr_above - wr_below, 1),
        })

    # Sort by ROI (profitable first), then by volume
    results.sort(key=lambda r: (-r["roi"], -r["total_cost"]))

    # Filter: only profitable wallets with conviction gap
    profitable = [r for r in results if r["roi"] > 5]

    print(f"{'Wallet':<14s} {'G':>3s} {'W':>3s} {'L':>3s} {'WR':>5s} {'Cost':>7s} {'PnL':>8s} {'ROI':>6s} {'Avg$':>6s} {'WR>med':>6s} {'WR<med':>6s} {'Gap':>5s}")
    print("-" * 95)
    for r in profitable[:30]:
        print(f"{r['wallet'][:12]:<14s} {r['games']:>3d} {r['wins']:>3d} {r['losses']:>3d} {r['wr']:>4.0f}% ${r['total_cost']:>6.0f} ${r['total_pnl']:>+7.0f} {r['roi']:>+5.0f}% ${r['avg_cost']:>5.0f} {r['wr_above_median']:>5.0f}% {r['wr_below_median']:>5.0f}% {r['conviction_gap']:>+4.0f}")

    print(f"\nTotal profitable wallets (>5% ROI, >={args.min_games}g, >=${args.min_cost} cost): {len(profitable)}")

    # Highlight: wallets with conviction gap > 20% (WR above median >> WR below)
    print()
    print("=== HIGH CONVICTION WALLETS (WR gap > 20%) ===")
    high_conv = [r for r in profitable if r["conviction_gap"] > 20]
    for r in high_conv[:15]:
        print(f"  {r['wallet'][:12]}  {r['wins']}W/{r['losses']}L ({r['wr']:.0f}%)  ROI {r['roi']:+.0f}%  avg ${r['avg_cost']:.0f}  big={r['wr_above_median']:.0f}% small={r['wr_below_median']:.0f}%  gap={r['conviction_gap']:+.0f}%")

    # Save
    out_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    out_file = os.path.join(out_dir, "smart_scout.json")
    with open(out_file, "w") as f:
        json.dump({
            "sport": args.sport,
            "days": args.days,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "wallets_scanned": len(wallet_trades),
            "profitable": profitable[:50],
            "high_conviction": high_conv[:20],
        }, f, indent=2)
    print(f"\nSaved to {out_file}")


if __name__ == "__main__":
    main()
