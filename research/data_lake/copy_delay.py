#!/usr/bin/env python3
"""
Copy Delay Analysis — measures actual delay, price impact, liquidity, and exit signals.

Joins our bot trades (data/trades.jsonl) with Cannae's trade history
(data_lake/trades/{cannae}.parquet) to compute:
  A) Copy delay and price impact per league
  B) Liquidity (trade density around Cannae entries)
  C) Exit signals (Cannae SELLs on open positions)

Output: data/copy_delay_report.json
Telegram: weekly summary on Sundays.

Usage:
  python3 copy_delay.py              # full analysis
  python3 copy_delay.py --no-telegram  # skip telegram
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent  # research/data_lake/
PROJECT_ROOT = BASE_DIR.parent.parent
TRADES_DIR = BASE_DIR / "trades"

sys.path.insert(0, str(BASE_DIR.parent))
from lib.pm_api import detect_league, send_telegram

# Cannae default
CANNAE_DEFAULT = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
OUTPUT = PROJECT_ROOT / "data" / "copy_delay_report.json"


def get_cannae_address() -> str:
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            for w in cfg.get("wallets", []):
                if "cannae" in (w.get("name") or "").lower():
                    return w.get("address", CANNAE_DEFAULT).lower()
        except Exception:
            pass
    return CANNAE_DEFAULT


def load_our_trades() -> pd.DataFrame:
    """Load our bot trades from trades.jsonl."""
    trades_file = PROJECT_ROOT / "data" / "trades.jsonl"
    if not trades_file.exists():
        print(f"ERROR: {trades_file} not found", flush=True)
        sys.exit(1)

    records = []
    with open(trades_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    df = pd.DataFrame(records)
    if len(df) == 0:
        return df

    # Parse timestamp to unix
    df["our_ts"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True).astype("int64") // 10**9
    # Only filled BUY trades
    df = df[(df["filled"] == True) & (df["side"] == "BUY")].copy()

    # Filter to active leagues (voetbal only — see strategy_current.md)
    ACTIVE_LEAGUES = {
        "epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa",
        "por", "bra", "itc", "ere", "es2", "bl2", "sea", "elc", "mex",
        "fr2", "aus", "spl", "efl", "tur",
    }
    if "event_slug" in df.columns:
        def slug_matches_league(slug):
            s = str(slug or "").lower()
            return any(s.startswith(f"{lg}-") for lg in ACTIVE_LEAGUES)
        before = len(df)
        df = df[df["event_slug"].apply(slug_matches_league)].copy()
        filtered = before - len(df)
        if filtered > 0:
            print(f"  Filtered {filtered} non-football trades", flush=True)

    return df


def load_cannae_trades(address: str) -> pd.DataFrame:
    """Load Cannae's trades from parquet."""
    parquet_path = TRADES_DIR / f"{address}.parquet"
    if not parquet_path.exists():
        print(f"ERROR: {parquet_path} not found. Run download_trades.py first.", flush=True)
        sys.exit(1)

    df = pd.read_parquet(parquet_path)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    return df


def analyze_copy_delay(our: pd.DataFrame, cannae: pd.DataFrame) -> dict:
    """A) Match our trades to Cannae's and compute delay + price impact.

    Cannae places GTC ask-1ct orders that fill in many small chunks.
    We track two delay metrics:
    - delay_from_last: time since Cannae's most recent fill before ours (pipeline speed)
    - delay_from_first: time since Cannae's first fill on this market
    """
    cannae_buys = cannae[cannae["side"] == "BUY"].copy()
    cannae_buys = cannae_buys.sort_values("timestamp")

    matches = []
    for _, row in our.iterrows():
        cid = row.get("condition_id", "")
        our_ts = row["our_ts"]
        our_price = float(row.get("price", 0))
        title = row.get("market_title", "")
        # Use OUR event_slug for league detection (Cannae on-chain data has no slug)
        our_slug = str(row.get("event_slug", "") or "")

        c_trades = cannae_buys[cannae_buys["conditionId"] == cid]
        c_before = c_trades[c_trades["timestamp"] < our_ts]

        if len(c_before) == 0:
            continue

        # Last fill before ours (pipeline speed)
        c_latest = c_before.iloc[-1]
        last_fill_ts = int(c_latest["timestamp"])
        # First fill on this market
        first_fill_ts = int(c_trades["timestamp"].min())
        # Cannae fill stats on this market
        cannae_fills = len(c_trades)
        cannae_shares = float(c_trades["size"].sum())

        c_price_raw = c_latest.get("price")
        c_price = float(c_price_raw) if c_price_raw is not None and str(c_price_raw) != "" else None

        delay_from_last = our_ts - last_fill_ts
        delay_from_first = our_ts - first_fill_ts
        if c_price is not None and c_price > 0:
            impact_ct = round((our_price - c_price) * 100, 2)
        else:
            impact_ct = None
        league = detect_league(title, our_slug)

        matches.append({
            "conditionId": cid,
            "title": title,
            "league": league,
            "last_fill_ts": last_fill_ts,
            "first_fill_ts": first_fill_ts,
            "our_ts": our_ts,
            "delay_s": delay_from_last,
            "delay_from_first_s": delay_from_first,
            "cannae_fills": cannae_fills,
            "cannae_shares": round(cannae_shares, 1),
            "cannae_price": c_price,
            "our_price": our_price,
            "impact_ct": impact_ct,
        })

    return {"matches": matches}


def analyze_liquidity(cannae: pd.DataFrame) -> list[dict]:
    """B) Per conditionId: trade density around Cannae entries."""
    cannae_buys = cannae[cannae["side"] == "BUY"].copy()
    thin = []

    for cid, group in cannae_buys.groupby("conditionId"):
        if len(group) == 0:
            continue
        first_entry = group.sort_values("timestamp").iloc[0]
        ts = int(first_entry["timestamp"])

        # Count all Cannae trades within 1h of first entry (proxy for market activity)
        window = cannae[
            (cannae["conditionId"] == cid)
            & (cannae["timestamp"] >= ts - 3600)
            & (cannae["timestamp"] <= ts + 3600)
        ]
        trade_count_1h = len(window)

        # Volume in 5min after entry on same side
        vol_5m = cannae[
            (cannae["conditionId"] == cid)
            & (cannae["side"] == "BUY")
            & (cannae["timestamp"] >= ts)
            & (cannae["timestamp"] <= ts + 300)
        ]["size"].sum()

        if trade_count_1h <= 2:
            slug = first_entry.get("eventSlug", "")
            thin.append({
                "conditionId": cid,
                "title": first_entry.get("title", ""),
                "league": detect_league(first_entry.get("title", ""), slug),
                "trades_1h": trade_count_1h,
                "volume_5m": round(float(vol_5m), 2),
            })

    return thin


def analyze_exits(cannae: pd.DataFrame, days: int = 7) -> list[dict]:
    """C) Cannae SELL trades in last N days = exit signals."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    sells = cannae[(cannae["side"] == "SELL") & (cannae["timestamp"] >= cutoff)].copy()

    exits = []
    for _, row in sells.iterrows():
        exits.append({
            "conditionId": row.get("conditionId", ""),
            "title": row.get("title", ""),
            "eventSlug": row.get("eventSlug", ""),
            "outcome": row.get("outcome", ""),
            "price": float(row.get("price", 0)),
            "size": float(row.get("size", 0)),
            "timestamp": int(row.get("timestamp", 0)),
        })

    return exits


def build_report(matches: list[dict], thin: list[dict], exits: list[dict]) -> dict:
    """Build structured report from analysis results."""
    if not matches:
        return {
            "overall": {"matched": 0, "median_delay_s": None, "median_impact_ct": None},
            "by_league": {},
            "thin_markets": thin[:20],
            "exits_7d": exits[:50],
            "recommendations": ["No matched trades found — run download_trades.py first"],
        }

    df = pd.DataFrame(matches)

    # Overall stats
    # delay_s = time since Cannae's last fill before ours (pipeline + stability)
    df_impact = df.dropna(subset=["impact_ct"])
    overall = {
        "matched": len(df),
        "matched_with_price": len(df_impact),
        "median_delay_s": int(df["delay_s"].median()),
        "p10_delay_s": int(df["delay_s"].quantile(0.1)),
        "p25_delay_s": int(df["delay_s"].quantile(0.25)),
        "p90_delay_s": int(df["delay_s"].quantile(0.9)),
        "median_delay_from_first_s": int(df["delay_from_first_s"].median()) if "delay_from_first_s" in df.columns else None,
        "median_cannae_fills": int(df["cannae_fills"].median()) if "cannae_fills" in df.columns else None,
        "median_impact_ct": round(float(df_impact["impact_ct"].median()), 2) if len(df_impact) > 0 else None,
        "mean_impact_ct": round(float(df_impact["impact_ct"].mean()), 2) if len(df_impact) > 0 else None,
    }

    # Per league
    by_league = {}
    for league, group in df.groupby("league"):
        g_impact = group.dropna(subset=["impact_ct"])
        by_league[league] = {
            "count": len(group),
            "median_delay_s": int(group["delay_s"].median()),
            "p25_delay_s": int(group["delay_s"].quantile(0.25)),
            "median_impact_ct": round(float(g_impact["impact_ct"].median()), 2) if len(g_impact) > 0 else None,
            "mean_impact_ct": round(float(g_impact["impact_ct"].mean()), 2) if len(g_impact) > 0 else None,
        }

    # Recommendations
    recs = []
    leagues_with_impact = {k: v for k, v in by_league.items() if v.get("mean_impact_ct") is not None}
    for league, stats in sorted(leagues_with_impact.items(), key=lambda x: x[1]["mean_impact_ct"], reverse=True):
        if stats["mean_impact_ct"] > 2.0 and stats["count"] >= 5:
            recs.append(f"Drop {league}: {stats['mean_impact_ct']}ct avg impact ({stats['count']} trades)")
    for league, stats in sorted(leagues_with_impact.items(), key=lambda x: x[1]["mean_impact_ct"]):
        if stats["mean_impact_ct"] < 1.0 and stats["count"] >= 5:
            recs.append(f"Focus {league}: {stats['mean_impact_ct']}ct avg impact ({stats['count']} trades)")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "by_league": by_league,
        "thin_markets": thin[:20],
        "exits_7d": exits[:50],
        "recommendations": recs,
    }


def format_telegram_summary(report: dict) -> str:
    """Format weekly telegram summary."""
    o = report["overall"]
    lines = [
        "Copy Delay Weekly Report",
        "",
        f"Matched trades: {o['matched']}",
        f"Median delay: {o.get('median_delay_s', '?')}s",
        f"Median impact: {o.get('median_impact_ct', '?')}ct",
        f"P90 delay: {o.get('p90_delay_s', '?')}s",
        "",
        "Per league:",
    ]

    for league, stats in sorted(
        report.get("by_league", {}).items(),
        key=lambda x: x[1]["count"],
        reverse=True,
    ):
        lines.append(
            f"  {league}: {stats['count']}x, "
            f"delay {stats['median_delay_s']}s, "
            f"impact {stats['median_impact_ct']}ct"
        )

    exits = report.get("exits_7d", [])
    if exits:
        lines.extend(["", f"Cannae exits (7d): {len(exits)}"])

    recs = report.get("recommendations", [])
    if recs:
        lines.extend(["", "Recommendations:"])
        for r in recs[:5]:
            lines.append(f"  - {r}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Copy delay analysis")
    parser.add_argument("--no-telegram", action="store_true", help="Skip telegram alert")
    args = parser.parse_args()

    cannae_addr = get_cannae_address()
    print(f"Cannae address: {cannae_addr[:10]}...", flush=True)

    # Load data
    print("Loading our trades...", flush=True)
    our = load_our_trades()
    print(f"  {len(our)} filled BUY trades", flush=True)

    print("Loading Cannae trades...", flush=True)
    cannae = load_cannae_trades(cannae_addr)
    print(f"  {len(cannae)} Cannae trades", flush=True)

    # A) Copy delay
    print("\nAnalyzing copy delay...", flush=True)
    result = analyze_copy_delay(our, cannae)
    matches = result["matches"]
    print(f"  {len(matches)} matched trades", flush=True)

    # B) Liquidity
    print("Analyzing liquidity...", flush=True)
    thin = analyze_liquidity(cannae)
    print(f"  {len(thin)} thin markets found", flush=True)

    # C) Exit signals
    print("Analyzing exit signals...", flush=True)
    exits = analyze_exits(cannae, days=7)
    print(f"  {len(exits)} exits in last 7d", flush=True)

    # Build and save report
    report = build_report(matches, thin, exits)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2))
    print(f"\nReport saved to {OUTPUT}", flush=True)

    # Print summary
    o = report["overall"]
    print(f"\n--- Summary ---")
    print(f"Matched: {o['matched']}")
    if o["matched"] > 0:
        print(f"Median delay: {o['median_delay_s']}s, P90: {o['p90_delay_s']}s")
        print(f"Median impact: {o['median_impact_ct']}ct, Mean: {o['mean_impact_ct']}ct")
    for r in report.get("recommendations", []):
        print(f"  -> {r}")

    # Telegram on Sundays
    if not args.no_telegram:
        now = datetime.now(timezone.utc)
        if now.weekday() == 6:  # Sunday
            msg = format_telegram_summary(report)
            if send_telegram(msg):
                print("\nTelegram weekly summary sent.", flush=True)
            else:
                print("\nTelegram send failed (check env vars).", flush=True)
        else:
            print(f"\nSkipping telegram (not Sunday, weekday={now.weekday()}).", flush=True)


if __name__ == "__main__":
    main()
