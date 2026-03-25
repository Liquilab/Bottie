#!/usr/bin/env python3
"""
RUS-234 Fase 2: Build take-profit decision table from historical price paths.

For each sport game (event), reconstructs the price paths of all ML+Draw tokens,
then analyzes: given entry price and current price at time T, what is the
probability of reaching 100ct? What is the probability of reversal below entry?
What is the optimal action (hold vs sell)?

Input:  data_lake/prices/*.parquet (from download_sport_prices.py)
        data_lake/sport_token_index.json
Output: data_lake/decision_table.parquet
        data_lake/game_analysis.parquet

Usage:
  python3 build_decision_table.py
  python3 build_decision_table.py --min-records 50   # min price records per token
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).parent / "data_lake"
PRICES_DIR = BASE_DIR / "prices"
INDEX_FILE = BASE_DIR / "sport_token_index.json"


def load_price_path(token_id: str) -> pd.DataFrame | None:
    """Load price history for a single token."""
    pfile = PRICES_DIR / f"{token_id}.parquet"
    if not pfile.exists():
        return None
    df = pd.read_parquet(pfile)
    if len(df) < 10:
        return None

    # Normalize columns
    if "t" in df.columns:
        df["timestamp"] = pd.to_numeric(df["t"], errors="coerce")
    elif "timestamp" in df.columns:
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")

    if "p" in df.columns:
        df["price"] = pd.to_numeric(df["p"], errors="coerce")
    elif "price" in df.columns:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")

    df = df.dropna(subset=["timestamp", "price"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def group_tokens_by_event(index: list[dict]) -> dict[str, list[dict]]:
    """Group tokens by event (slug base without outcome suffix)."""
    events = {}
    for t in index:
        slug = t.get("slug", "")
        # Extract base event slug: strip outcome suffix
        # e.g. "ucl-fcb1-new-2026-03-18-fcb1" → "ucl-fcb1-new-2026-03-18"
        # e.g. "ucl-fcb1-new-2026-03-18-draw" → "ucl-fcb1-new-2026-03-18"
        condition_id = t.get("condition_id", "")

        # Use condition_id grouping: same condition = same game leg
        # But we want to group by EVENT (all legs of one game)
        # Best heuristic: strip last segment of slug that's an outcome
        parts = slug.rsplit("-", 1)
        if len(parts) == 2 and parts[1] in ("draw", "fcb1", "new", "tot", "atm1",
                                               "bay1", "ata1", "liv1", "gal", "spread",
                                               "total", "btts", "home", "away"):
            base = parts[0]
        else:
            # Try stripping known suffixes more aggressively
            base = slug

        # Alternative: group by end_date + first N chars of slug
        end_date = t.get("end_date", "")[:10]
        slug_prefix = slug[:30] if slug else ""
        event_key = f"{end_date}_{slug_prefix}" if end_date else slug

        if event_key not in events:
            events[event_key] = []
        events[event_key].append(t)

    return events


def analyze_price_path(df: pd.DataFrame) -> dict:
    """Analyze a single token's price path for take-profit signals.

    Returns statistics about price behavior:
    - max_price: highest price reached
    - min_price: lowest price reached
    - final_price: last recorded price (near resolution)
    - resolved_to_1: did it resolve to ~1.00?
    - resolved_to_0: did it resolve to ~0.00?
    - time_at_max: when (as % of total time) did it reach max
    - entry_price: price at start of available data
    - drawdown_from_max: how much did it drop from max before end
    """
    if len(df) < 5:
        return {}

    prices = df["price"].values
    timestamps = df["timestamp"].values

    entry_price = prices[0]
    final_price = prices[-1]
    max_price = prices.max()
    min_price = prices.min()
    max_idx = prices.argmax()
    min_idx = prices.argmin()

    total_time = timestamps[-1] - timestamps[0]
    time_at_max = (timestamps[max_idx] - timestamps[0]) / total_time if total_time > 0 else 0

    resolved_to_1 = final_price >= 0.95
    resolved_to_0 = final_price <= 0.05

    # Drawdown from max
    post_max_prices = prices[max_idx:]
    drawdown_from_max = max_price - post_max_prices.min() if len(post_max_prices) > 0 else 0

    # MFE (Maximum Favorable Excursion) from entry
    mfe = max_price - entry_price

    # MAE (Maximum Adverse Excursion) from entry
    mae = entry_price - min_price

    # Price at various time percentiles
    n = len(prices)
    p25 = prices[int(n * 0.25)] if n > 4 else prices[0]
    p50 = prices[int(n * 0.50)] if n > 2 else prices[0]
    p75 = prices[int(n * 0.75)] if n > 4 else prices[-1]
    p90 = prices[int(n * 0.90)] if n > 10 else prices[-1]

    return {
        "entry_price": round(entry_price, 4),
        "final_price": round(final_price, 4),
        "max_price": round(max_price, 4),
        "min_price": round(min_price, 4),
        "mfe": round(mfe, 4),
        "mae": round(mae, 4),
        "time_at_max_pct": round(time_at_max, 4),
        "drawdown_from_max": round(drawdown_from_max, 4),
        "resolved_to_1": resolved_to_1,
        "resolved_to_0": resolved_to_0,
        "n_records": len(df),
        "duration_hours": round(total_time / 3600, 1) if total_time > 0 else 0,
        "price_at_25pct": round(p25, 4),
        "price_at_50pct": round(p50, 4),
        "price_at_75pct": round(p75, 4),
        "price_at_90pct": round(p90, 4),
    }


def build_take_profit_table(analyses: list[dict]) -> pd.DataFrame:
    """Build the decision table: for each entry_price bucket × current_price bucket,
    what is the probability of reaching 1.00, reversal probability, and optimal action.

    Entry price buckets: 0.10 to 0.90 in steps of 0.05
    Current price thresholds: entry + 0.05, +0.10, +0.15, +0.20, +0.30, +0.40
    Time buckets: 0-25%, 25-50%, 50-75%, 75-100% of game time
    """
    if not analyses:
        return pd.DataFrame()

    df = pd.DataFrame(analyses)

    # Only analyze tokens that resolved clearly
    df = df[df["resolved_to_1"] | df["resolved_to_0"]].copy()

    if len(df) < 50:
        print(f"  Warning: only {len(df)} resolved tokens, need more data for reliable table")

    rows = []
    entry_buckets = np.arange(0.10, 0.90, 0.05)

    for entry_low in entry_buckets:
        entry_high = entry_low + 0.05
        subset = df[(df["entry_price"] >= entry_low) & (df["entry_price"] < entry_high)]
        if len(subset) < 5:
            continue

        n_total = len(subset)
        n_resolved_1 = subset["resolved_to_1"].sum()
        p_win = n_resolved_1 / n_total

        # For each current price threshold (how high above entry)
        for delta in [0.05, 0.10, 0.15, 0.20, 0.30, 0.40]:
            threshold = entry_low + delta
            if threshold >= 0.99:
                continue

            # How many reached this threshold?
            reached = subset[subset["max_price"] >= threshold]
            n_reached = len(reached)
            if n_reached < 3:
                continue

            # Of those that reached threshold, how many eventually resolved to 1?
            n_reached_and_won = reached["resolved_to_1"].sum()
            p_win_after_reaching = n_reached_and_won / n_reached

            # Average further gain if held to resolution (for winners)
            winners_after = reached[reached["resolved_to_1"]]
            avg_further_gain = (1.0 - threshold) if len(winners_after) > 0 else 0

            # Average loss if held to resolution (for losers)
            losers_after = reached[reached["resolved_to_0"]]
            avg_loss = threshold if len(losers_after) > 0 else 0

            # Expected value of HOLDING from threshold
            ev_hold = p_win_after_reaching * (1.0 - threshold) - (1 - p_win_after_reaching) * threshold

            # Expected value of SELLING at threshold
            ev_sell = threshold - (entry_low + 0.025)  # profit = current - entry (midpoint)

            # Optimal action
            action = "SELL" if ev_sell > ev_hold else "HOLD"

            # Average time at max (proxy for when profits peak)
            avg_time_at_max = reached["time_at_max_pct"].mean()

            rows.append({
                "entry_price_low": round(entry_low, 2),
                "entry_price_high": round(entry_high, 2),
                "current_price_threshold": round(threshold, 2),
                "delta_from_entry": round(delta, 2),
                "n_sample": n_total,
                "n_reached_threshold": n_reached,
                "pct_reached": round(n_reached / n_total * 100, 1),
                "p_win_at_entry": round(p_win, 3),
                "p_win_after_reaching": round(p_win_after_reaching, 3),
                "ev_hold": round(ev_hold, 4),
                "ev_sell": round(ev_sell, 4),
                "optimal_action": action,
                "avg_time_at_max_pct": round(avg_time_at_max, 3),
            })

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="RUS-234: Build take-profit decision table")
    parser.add_argument("--min-records", type=int, default=20,
                        help="Minimum price records per token (default: 20)")
    args = parser.parse_args()

    if not INDEX_FILE.exists():
        print(f"ERROR: {INDEX_FILE} not found. Run download_sport_prices.py first.")
        sys.exit(1)

    with open(INDEX_FILE) as f:
        index = json.load(f)

    print(f"Loaded {len(index):,} tokens from index", flush=True)

    # Analyze each token's price path
    analyses = []
    n_loaded = n_skip = 0

    for i, t in enumerate(index):
        tid = t["token_id"]
        df = load_price_path(tid)
        if df is None or len(df) < args.min_records:
            n_skip += 1
            continue

        stats = analyze_price_path(df)
        if stats:
            stats["token_id"] = tid
            stats["question"] = t.get("question", "")
            stats["slug"] = t.get("slug", "")
            stats["sport_type"] = t.get("sport_type", "")
            stats["outcome"] = t.get("outcome", "")
            stats["volume"] = t.get("volume", 0)
            analyses.append(stats)
            n_loaded += 1

        if (i + 1) % 1000 == 0:
            print(f"  [{i+1:6d}/{len(index)}] loaded:{n_loaded} skipped:{n_skip}", flush=True)

    print(f"\nAnalyzed {n_loaded:,} tokens ({n_skip:,} skipped)", flush=True)

    # Save individual token analyses
    df_analysis = pd.DataFrame(analyses)
    analysis_file = BASE_DIR / "game_analysis.parquet"
    df_analysis.to_parquet(analysis_file, index=False)
    print(f"✓ Saved {analysis_file} ({len(df_analysis):,} rows)", flush=True)

    # Print summary stats
    print(f"\n{'='*60}")
    print(f"  PRICE PATH STATISTICS")
    print(f"{'='*60}")
    resolved_1 = df_analysis["resolved_to_1"].sum()
    resolved_0 = df_analysis["resolved_to_0"].sum()
    neither = len(df_analysis) - resolved_1 - resolved_0
    print(f"  Resolved to 1.00: {resolved_1:,} ({resolved_1/len(df_analysis)*100:.1f}%)")
    print(f"  Resolved to 0.00: {resolved_0:,} ({resolved_0/len(df_analysis)*100:.1f}%)")
    print(f"  Unresolved/mid:   {neither:,} ({neither/len(df_analysis)*100:.1f}%)")
    print(f"  Avg MFE: {df_analysis['mfe'].mean():.4f}")
    print(f"  Avg MAE: {df_analysis['mae'].mean():.4f}")
    print(f"  Avg duration: {df_analysis['duration_hours'].mean():.1f} hours")
    print(f"  Avg time at max: {df_analysis['time_at_max_pct'].mean()*100:.1f}% of game time")

    # Build decision table
    print(f"\n{'='*60}")
    print(f"  BUILDING DECISION TABLE")
    print(f"{'='*60}")

    # Split by sport type
    for sport_type in ["moneyline", "draw", "all"]:
        if sport_type == "all":
            subset = analyses
        else:
            subset = [a for a in analyses if a.get("sport_type") == sport_type]

        if len(subset) < 50:
            print(f"\n  {sport_type}: skipped (only {len(subset)} tokens)")
            continue

        dt = build_take_profit_table(subset)
        if len(dt) > 0:
            dt["sport_type"] = sport_type
            dt_file = BASE_DIR / f"decision_table_{sport_type}.parquet"
            dt.to_parquet(dt_file, index=False)
            print(f"\n  {sport_type}: {len(dt)} decision rules")
            print(f"  Saved: {dt_file}")

            # Print the most interesting rules
            sells = dt[dt["optimal_action"] == "SELL"].sort_values("ev_sell", ascending=False)
            if len(sells) > 0:
                print(f"\n  Top SELL signals ({sport_type}):")
                print(f"  {'Entry':<8} {'Current':<10} {'Δ':<6} {'P(win@entry)':<14} {'P(win@curr)':<14} {'EV hold':<10} {'EV sell':<10} {'Action':<8} {'n'}")
                for _, r in sells.head(10).iterrows():
                    print(f"  {r['entry_price_low']:.2f}-{r['entry_price_high']:.2f}"
                          f"  {r['current_price_threshold']:.2f}"
                          f"      {r['delta_from_entry']:.2f}"
                          f"   {r['p_win_at_entry']:.3f}"
                          f"          {r['p_win_after_reaching']:.3f}"
                          f"          {r['ev_hold']:+.4f}"
                          f"     {r['ev_sell']:+.4f}"
                          f"    {r['optimal_action']}"
                          f"    {r['n_reached_threshold']}")

    print(f"\n{'='*60}")
    print(f"  DONE — RUS-234 Decision Table")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
