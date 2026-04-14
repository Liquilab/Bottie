#!/usr/bin/env python3
"""Retroactive flow imbalance analysis for resolved football games.

Measures pre-kickoff trade flow imbalance on Polymarket win markets
and correlates with actual outcomes.

Flow = sum(price * size) for BUY trades on each team's Yes token
in the 30 minutes before kickoff.

Usage:
    # Run locally, SSH to VPS for trades.jsonl:
    python scripts/retro_flow_imbalance.py

    # Use a local copy of trades.jsonl:
    python scripts/retro_flow_imbalance.py --local /path/to/trades.jsonl
"""
import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VPS = "root@78.141.222.227"
TRADES_PATH = "/opt/bottie/data/trades.jsonl"
DATA_BASE = "https://data-api.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
RATE_LIMIT = 0.2  # seconds between API calls
LOOKBACK_DAYS = 14
PRE_KICKOFF_MINUTES = 30
BET_SIZE = 38.0  # $38 = 2.5% of $1500

FOOTBALL_PREFIXES = {
    "epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por",
    "bra", "itc", "ere", "es2", "bl2", "sea", "elc", "mex", "fr2", "aus",
    "spl", "efl", "tur", "uef", "ucl", "cdr", "acn", "cde", "ssc", "fif",
}
US_SKIP_PREFIXES = {"nba", "nhl", "mlb", "nfl", "cbb", "ncaa"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_trades_ssh():
    """Fetch trades.jsonl from VPS via SSH."""
    print(f"Fetching trades.jsonl from {VPS}:{TRADES_PATH} ...")
    result = subprocess.run(
        ["ssh", VPS, f"cat {TRADES_PATH}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"SSH error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    trades = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return trades


def load_trades_local(path):
    """Load trades.jsonl from local file."""
    print(f"Loading trades from {path} ...")
    trades = []
    with open(path) as f:
        for line in f:
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return trades


def league_of(slug):
    slug = slug.replace("-more-markets", "")
    return slug.split("-", 1)[0] if slug else ""


def is_football(slug):
    prefix = league_of(slug)
    return prefix in FOOTBALL_PREFIXES and prefix not in US_SKIP_PREFIXES


def api_get(url, params=None):
    """GET with rate limiting and error handling."""
    time.sleep(RATE_LIMIT)
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  API error: {e}", file=sys.stderr)
        return None


def get_event_info(event_slug):
    """Fetch event from Gamma API. Returns (start_date_unix, markets_dict) or None."""
    data = api_get(f"{GAMMA_BASE}/events", params={"slug": event_slug})
    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    event = data[0]
    start_str = event.get("startTime") or event.get("endDate") or event.get("startDate")
    if not start_str:
        return None

    # Parse ISO date
    try:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except Exception:
        return None
    start_unix = start_dt.timestamp()

    # Extract win markets: question contains "win" but not "draw"
    markets = {}
    for m in event.get("markets", []):
        q = (m.get("question") or "").lower()
        if "win" not in q or "draw" in q:
            continue
        cid = m.get("conditionId")
        token_ids = m.get("clobTokenIds")
        if not cid or not token_ids:
            continue
        # clobTokenIds is [yes_token, no_token]
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except Exception:
                continue
        markets[cid] = {
            "question": m.get("question", ""),
            "yes_token": token_ids[0] if len(token_ids) > 0 else None,
            "no_token": token_ids[1] if len(token_ids) > 1 else None,
        }

    return start_unix, markets


def fetch_market_trades(condition_id):
    """Fetch trade history from Data API (public, no auth). Returns list of trades."""
    all_trades = []
    # Paginate with offset
    for offset in range(0, 2000, 500):
        data = api_get(f"{DATA_BASE}/trades", params={
            "market": condition_id, "limit": 500, "offset": offset
        })
        if not data or not isinstance(data, list) or len(data) == 0:
            break
        all_trades.extend(data)
        if len(data) < 500:
            break
    return all_trades


def compute_flow(trades, yes_token, start_unix, window_minutes=30):
    """Compute bullish flow (BUY on Yes token) in the window before kickoff.
    Data API fields: timestamp (unix), side, asset (token ID), outcome, size, price.
    Returns sum of price*size for BUY trades on the Yes-outcome side."""
    window_start = start_unix - (window_minutes * 60)
    flow = 0.0
    n_trades = 0
    for t in trades:
        ts = t.get("timestamp")
        if ts is None:
            continue
        try:
            ts = float(ts)
        except (ValueError, TypeError):
            continue
        if ts < window_start or ts > start_unix:
            continue
        side = (t.get("side") or "").upper()
        outcome = (t.get("outcome") or "").lower()
        # BUY on Yes outcome = bullish for this team
        if side == "BUY" and outcome == "yes":
            try:
                price = float(t.get("price", 0))
                size = float(t.get("size", 0))
                flow += price * size
                n_trades += 1
            except (ValueError, TypeError):
                continue
    return flow, n_trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Retro flow imbalance analysis")
    parser.add_argument("--local", help="Path to local trades.jsonl (skip SSH)")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS,
                        help=f"Lookback days (default {LOOKBACK_DAYS})")
    args = parser.parse_args()

    # 1. Load trades
    if args.local:
        raw_trades = load_trades_local(args.local)
    else:
        raw_trades = load_trades_ssh()

    print(f"Loaded {len(raw_trades)} raw trades")

    # Filter: resolved football, last N days
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    cutoff_str = cutoff.isoformat()

    resolved = []
    for t in raw_trades:
        if t.get("dry_run"):
            continue
        if t.get("result") not in ("win", "loss"):
            continue
        slug = t.get("event_slug", "")
        if not slug or slug.endswith("-more-markets"):
            continue
        if not is_football(slug):
            continue
        resolved_at = t.get("resolved_at") or t.get("created_at") or ""
        if resolved_at < cutoff_str:
            continue
        resolved.append(t)

    print(f"Resolved football trades (last {args.days}d): {len(resolved)}")

    # Group by event_slug to get unique games
    games = defaultdict(list)
    for t in resolved:
        slug = t["event_slug"].replace("-more-markets", "")
        games[slug].append(t)

    print(f"Unique football games: {len(games)}")
    print()

    # 2-7. Analyze each game
    results = []
    skipped = defaultdict(int)

    for slug, trades in sorted(games.items()):
        print(f"Processing {slug} ...", end=" ", flush=True)

        # Determine actual winners from our trade data
        actual_winners = {}  # market_title -> won (bool)
        for t in trades:
            title = t.get("market_title", "")
            if "win" not in title.lower() or "draw" in title.lower():
                continue
            outcome = t.get("outcome", "")
            result = t.get("result", "")
            # outcome=No, result=win → team did NOT win (No resolved true)
            # outcome=No, result=loss → team DID win (No resolved false)
            # outcome=Yes, result=win → team DID win
            # outcome=Yes, result=loss → team did NOT win
            if outcome == "Yes":
                team_won = result == "win"
            else:  # No
                team_won = result == "loss"
            actual_winners[title] = team_won

        # Get event info from Gamma
        event_info = get_event_info(slug)
        if not event_info:
            print("SKIP (no Gamma data)")
            skipped["no_gamma"] += 1
            continue

        start_unix, win_markets = event_info
        if not win_markets:
            print("SKIP (no win markets)")
            skipped["no_win_markets"] += 1
            continue

        # Fetch CLOB trades and compute flow per team
        team_flows = {}  # question -> (flow, team_won)
        for cid, minfo in win_markets.items():
            if not minfo["yes_token"]:
                continue
            clob_trades = fetch_market_trades(cid)
            flow, n = compute_flow(clob_trades, minfo["yes_token"],
                                   start_unix, PRE_KICKOFF_MINUTES)
            question = minfo["question"]
            # Match to actual outcome
            won = None
            for title, w in actual_winners.items():
                # Fuzzy match: check if the key words overlap
                if question.lower().replace("?", "").strip() in title.lower().replace("?", "").strip() or \
                   title.lower().replace("?", "").strip() in question.lower().replace("?", "").strip():
                    won = w
                    break
            # Fallback: try matching team name from question
            if won is None:
                q_words = set(question.lower().split())
                for title, w in actual_winners.items():
                    t_words = set(title.lower().split())
                    if len(q_words & t_words) >= 3:
                        won = w
                        break
            team_flows[question] = {"flow": flow, "n_trades": n, "won": won}

        # Need at least 2 teams with flow data
        teams_with_flow = {k: v for k, v in team_flows.items() if v["flow"] > 0}
        if len(teams_with_flow) < 2:
            print(f"SKIP (only {len(teams_with_flow)} teams with flow)")
            skipped["insufficient_flow"] += 1
            continue

        # Find strongest and weakest
        sorted_teams = sorted(teams_with_flow.items(), key=lambda x: x[1]["flow"], reverse=True)
        strongest_name, strongest = sorted_teams[0]
        weakest_name, weakest = sorted_teams[-1]

        flow_ratio = strongest["flow"] / weakest["flow"] if weakest["flow"] > 0 else 999.0

        # Did flow predict the winner?
        flow_predicted = strongest_name  # flow says this team wins
        flow_correct = strongest["won"] is True

        # Actual winner name
        actual_winner = "Draw/Unknown"
        for name, info in team_flows.items():
            if info["won"] is True:
                actual_winner = name
                break
        # Check if draw (no team won)
        if all(v["won"] is False for v in team_flows.values() if v["won"] is not None):
            actual_winner = "Draw"

        # Opponent No ROI: bet $38 on opponent No of the flow-predicted team
        # "Opponent No" = No on the weakest team (the one flow says will lose)
        # If the weakest team indeed loses → No resolves to $1 → profit
        # weakest["won"] is the result for the weakest team
        opp_no_result = None
        opp_no_pnl = 0.0
        opp_no_price = None
        if weakest["won"] is not None and flow_ratio >= 2.0:
            # We'd buy No on the weakest team
            # No price ~ 1 - (weakest_flow proportion) — but we don't know the price
            # Use a simplification: assume No price = average price we'd pay
            # For a fair estimate, use the implied price from flow ratio
            # Actually, we need the actual market price. Fetch from CLOB last price.
            # Simplification: assume No price is the complement of the yes mid
            # Just use a flat assumption based on the flow being a bullish signal
            # Better: use the last traded price from our trades data
            weak_yes_price = None
            for t in trades:
                title = t.get("market_title", "")
                if weakest_name.lower().replace("?", "").strip() in title.lower().replace("?", "").strip() or \
                   title.lower().replace("?", "").strip() in weakest_name.lower().replace("?", "").strip():
                    if t.get("outcome") == "Yes":
                        weak_yes_price = float(t.get("price", 0))
                    elif t.get("outcome") == "No":
                        weak_yes_price = 1.0 - float(t.get("price", 0))
                    break

            if weak_yes_price and weak_yes_price > 0:
                opp_no_price = 1.0 - weak_yes_price
                shares = BET_SIZE / opp_no_price if opp_no_price > 0 else 0
                if weakest["won"] is False:
                    # Weakest lost → No on weakest resolves $1 → profit
                    opp_no_pnl = shares * 1.0 - BET_SIZE
                    opp_no_result = "win"
                else:
                    # Weakest won → No on weakest resolves $0 → loss
                    opp_no_pnl = -BET_SIZE
                    opp_no_result = "loss"

        results.append({
            "slug": slug,
            "flow_ratio": flow_ratio,
            "strongest": strongest_name,
            "strongest_flow": strongest["flow"],
            "weakest": weakest_name,
            "weakest_flow": weakest["flow"],
            "flow_correct": flow_correct,
            "actual_winner": actual_winner,
            "opp_no_result": opp_no_result,
            "opp_no_pnl": opp_no_pnl,
            "opp_no_price": opp_no_price,
        })
        mark = "OK" if flow_correct else "MISS"
        print(f"ratio={flow_ratio:.1f}x  {mark}")

    # ---------------------------------------------------------------------------
    # 8. Output table
    # ---------------------------------------------------------------------------
    print()
    print("=" * 120)
    print("FLOW IMBALANCE ANALYSIS — pre-kickoff 30min BUY flow on Yes tokens")
    print("=" * 120)
    print(f"{'Game':<40} {'Ratio':>6} {'Flow Predicted':>25} {'Actual Winner':>25} {'Hit':>4}")
    print("-" * 120)

    correct = 0
    total = 0
    for r in sorted(results, key=lambda x: x["flow_ratio"], reverse=True):
        short_slug = r["slug"][:38]
        pred = r["strongest"][:23]
        actual = r["actual_winner"][:23]
        hit = "Y" if r["flow_correct"] else "N"
        if r["flow_correct"]:
            correct += 1
        total += 1
        print(f"{short_slug:<40} {r['flow_ratio']:>5.1f}x {pred:>25} {actual:>25} {hit:>4}")

    print("-" * 120)
    acc = correct / total * 100 if total else 0
    print(f"Overall: {correct}/{total} correct ({acc:.1f}%)")
    print()

    # ---------------------------------------------------------------------------
    # 9. Accuracy by flow_ratio bucket
    # ---------------------------------------------------------------------------
    buckets = {
        "1-2x": (1.0, 2.0),
        "2-3x": (2.0, 3.0),
        "3-5x": (3.0, 5.0),
        "5x+":  (5.0, 999.0),
    }

    print("=" * 80)
    print("ACCURACY BY FLOW RATIO BUCKET")
    print("=" * 80)
    print(f"{'Bucket':<10} {'N':>5} {'Correct':>8} {'Accuracy':>10}")
    print("-" * 40)

    for label, (lo, hi) in buckets.items():
        bucket_games = [r for r in results if lo <= r["flow_ratio"] < hi]
        n = len(bucket_games)
        c = sum(1 for r in bucket_games if r["flow_correct"])
        a = c / n * 100 if n else 0
        print(f"{label:<10} {n:>5} {c:>8} {a:>9.1f}%")

    print()

    # ---------------------------------------------------------------------------
    # ROI: opponent No strategy for ratio >= 2:1
    # ---------------------------------------------------------------------------
    eligible = [r for r in results if r["flow_ratio"] >= 2.0 and r["opp_no_result"] is not None]
    print("=" * 80)
    print(f"OPPONENT NO STRATEGY — bet ${BET_SIZE:.0f} on No of weakest team when ratio >= 2:1")
    print("=" * 80)
    if eligible:
        total_pnl = sum(r["opp_no_pnl"] for r in eligible)
        total_stake = BET_SIZE * len(eligible)
        wins = sum(1 for r in eligible if r["opp_no_result"] == "win")
        losses = sum(1 for r in eligible if r["opp_no_result"] == "loss")
        wr = wins / len(eligible) * 100 if eligible else 0
        roi = total_pnl / total_stake * 100 if total_stake else 0

        print(f"{'Game':<40} {'Ratio':>6} {'No Price':>9} {'Result':>8} {'PnL':>10}")
        print("-" * 80)
        for r in sorted(eligible, key=lambda x: x["flow_ratio"], reverse=True):
            short = r["slug"][:38]
            price_str = f"{r['opp_no_price']:.2f}" if r["opp_no_price"] else "?"
            print(f"{short:<40} {r['flow_ratio']:>5.1f}x {price_str:>9} {r['opp_no_result']:>8} {r['opp_no_pnl']:>+10.2f}")
        print("-" * 80)
        print(f"Trades: {len(eligible)}  W: {wins}  L: {losses}  WR: {wr:.1f}%")
        print(f"Total stake: ${total_stake:,.0f}  PnL: ${total_pnl:+,.2f}  ROI: {roi:+.2f}%")
    else:
        print("No eligible games found (need ratio >= 2:1 with price data)")

    print()
    print(f"Skipped: {dict(skipped)}")


if __name__ == "__main__":
    main()
