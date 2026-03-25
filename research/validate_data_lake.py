#!/usr/bin/env python3
"""
Validate the Polymarket data lake after download.

Checks:
  1. Are all parquet files readable?
  2. How many markets have trades? How many have price history?
  3. Which markets have NO trades (gaps)?
  4. Time period coverage (first + last trade date per market)
  5. Schema consistency across trade files

Output: validation_report.json (in data_lake/)

Usage:
  python3 validate_data_lake.py
  python3 validate_data_lake.py --verbose    # also list all gaps
  python3 validate_data_lake.py --sample 50  # check first 50 trade files only
"""

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BASE_DIR     = Path(__file__).parent / "data_lake"
MARKETS_FILE = BASE_DIR / "markets.parquet"
TRADES_DIR   = BASE_DIR / "trades"
PRICES_DIR   = BASE_DIR / "prices"
REPORT_FILE  = BASE_DIR / "validation_report.json"


def check_parquet(path: Path) -> tuple[bool, str, int]:
    """Try to read a parquet file. Returns (ok, error_msg, row_count)."""
    try:
        df = pd.read_parquet(path)
        return True, "", len(df)
    except Exception as e:
        return False, str(e), 0


def ts_to_iso(ts) -> str | None:
    """Convert a unix timestamp (int/float/str) to ISO date string."""
    if ts is None:
        return None
    try:
        t = float(ts)
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)[:10]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="Print every gap")
    parser.add_argument("--sample", type=int, default=None,
                        help="Only check first N trade files (for speed)")
    args = parser.parse_args()

    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_lake_path": str(BASE_DIR.resolve()),
        "markets": {},
        "trades": {},
        "prices": {},
        "gaps": [],
        "errors": [],
        "summary": {},
    }

    errors: list[str] = []

    # ── 1. markets.parquet ────────────────────────────────────────────────
    print("Checking markets.parquet…", flush=True)
    if not MARKETS_FILE.exists():
        msg = f"MISSING: {MARKETS_FILE}"
        print(f"  ✗ {msg}")
        errors.append(msg)
        report["markets"] = {"exists": False}
    else:
        ok, err, nrows = check_parquet(MARKETS_FILE)
        if not ok:
            errors.append(f"markets.parquet unreadable: {err}")
            report["markets"] = {"exists": True, "readable": False, "error": err}
        else:
            df_m = pd.read_parquet(MARKETS_FILE)
            categories = {}
            if "category" in df_m.columns:
                categories = df_m["category"].value_counts().to_dict()
            resolved_count = 0
            if "resolved" in df_m.columns:
                resolved_count = int(df_m["resolved"].sum())
            report["markets"] = {
                "exists": True,
                "readable": True,
                "row_count": nrows,
                "columns": list(df_m.columns),
                "resolved_count": resolved_count,
                "unresolved_count": nrows - resolved_count,
                "categories": {str(k): int(v) for k, v in categories.items()},
            }
            print(f"  ✓ {nrows:,} markets  ({resolved_count:,} resolved)")

    # ── 2. trades/ directory ──────────────────────────────────────────────
    print("\nChecking trades/…", flush=True)
    trade_files = sorted(TRADES_DIR.glob("*.parquet")) if TRADES_DIR.exists() else []
    if args.sample:
        trade_files = trade_files[: args.sample]

    n_trade_ok = n_trade_err = 0
    trade_row_counts: list[int] = []
    trade_col_sets: list[frozenset] = []
    all_first_dates: list[str] = []
    all_last_dates:  list[str] = []
    bad_trade_files: list[dict] = []

    for f in trade_files:
        ok, err, nrows = check_parquet(f)
        if not ok:
            n_trade_err += 1
            bad_trade_files.append({"file": f.name, "error": err})
            errors.append(f"trades/{f.name}: {err}")
            continue

        n_trade_ok += 1
        trade_row_counts.append(nrows)

        df_t = pd.read_parquet(f)
        trade_col_sets.append(frozenset(df_t.columns))

        # Time range
        for ts_col in ("timestamp", "created_at", "createdAt", "transactedAt"):
            if ts_col in df_t.columns and nrows > 0:
                try:
                    series = pd.to_datetime(df_t[ts_col], unit="s", utc=True, errors="coerce")
                    if series.notna().any():
                        all_first_dates.append(series.min().strftime("%Y-%m-%d"))
                        all_last_dates.append(series.max().strftime("%Y-%m-%d"))
                except Exception:
                    pass
                break

    # Check which markets have NO trade file
    gaps: list[str] = []
    if MARKETS_FILE.exists() and not df_m.empty:
        trade_cids = {f.stem for f in TRADES_DIR.glob("*.parquet")} if TRADES_DIR.exists() else set()
        cid_col = "conditionId" if "conditionId" in df_m.columns else "id"
        if cid_col in df_m.columns:
            for cid in df_m[cid_col].dropna():
                cid = str(cid).strip()
                if cid and cid not in trade_cids:
                    gaps.append(cid)

    # Schema consistency
    schema_variants = len(set(trade_col_sets))
    most_common_schema = list(sorted(trade_col_sets, key=lambda s: trade_col_sets.count(s), reverse=True)[0]) if trade_col_sets else []

    report["trades"] = {
        "files_found": len(trade_files),
        "files_readable": n_trade_ok,
        "files_unreadable": n_trade_err,
        "unreadable_files": bad_trade_files,
        "total_rows": sum(trade_row_counts),
        "avg_rows_per_market": round(sum(trade_row_counts) / max(n_trade_ok, 1), 1),
        "min_rows": min(trade_row_counts) if trade_row_counts else 0,
        "max_rows": max(trade_row_counts) if trade_row_counts else 0,
        "schema_variants": schema_variants,
        "most_common_columns": sorted(most_common_schema),
        "earliest_trade": min(all_first_dates) if all_first_dates else None,
        "latest_trade": max(all_last_dates) if all_last_dates else None,
    }

    report["gaps"] = gaps[:500]  # cap at 500 in report
    report["gaps_total"] = len(gaps)

    print(f"  ✓ {n_trade_ok:,} readable  ✗ {n_trade_err} unreadable  ∅ {len(gaps):,} gaps")
    if all_first_dates:
        print(f"  Time range: {min(all_first_dates)} → {max(all_last_dates)}")
    if args.verbose and gaps:
        print(f"\n  GAPS (markets without trades):")
        for g in gaps[:100]:
            print(f"    {g}")
        if len(gaps) > 100:
            print(f"    … and {len(gaps)-100} more")

    # ── 3. prices/ directory ──────────────────────────────────────────────
    print("\nChecking prices/…", flush=True)
    price_files = sorted(PRICES_DIR.glob("*.parquet")) if PRICES_DIR.exists() else []

    n_price_ok = n_price_err = 0
    price_row_counts: list[int] = []
    bad_price_files: list[dict] = []

    for f in price_files:
        ok, err, nrows = check_parquet(f)
        if not ok:
            n_price_err += 1
            bad_price_files.append({"file": f.name, "error": err})
            errors.append(f"prices/{f.name}: {err}")
        else:
            n_price_ok += 1
            price_row_counts.append(nrows)

    report["prices"] = {
        "files_found": len(price_files),
        "files_readable": n_price_ok,
        "files_unreadable": n_price_err,
        "unreadable_files": bad_price_files,
        "total_rows": sum(price_row_counts),
        "avg_rows_per_token": round(sum(price_row_counts) / max(n_price_ok, 1), 1),
    }
    print(f"  ✓ {n_price_ok:,} readable  ✗ {n_price_err} unreadable")

    # ── 4. Summary ────────────────────────────────────────────────────────
    total_markets = report["markets"].get("row_count", 0)
    pct_with_trades = n_trade_ok / max(total_markets, 1) * 100
    pct_with_prices = n_price_ok / max(total_markets, 1) * 100

    report["errors"] = errors
    report["summary"] = {
        "total_markets": total_markets,
        "markets_with_trades": n_trade_ok,
        "markets_without_trades": len(gaps),
        "markets_with_prices": n_price_ok,
        "pct_with_trades": round(pct_with_trades, 1),
        "pct_with_prices": round(pct_with_prices, 1),
        "total_errors": len(errors),
        "status": "OK" if not errors else "WARNINGS",
    }

    # ── 5. Write report ───────────────────────────────────────────────────
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n── Validation Report ────────────────────────────────")
    print(f"  Total markets    : {total_markets:,}")
    print(f"  With trades      : {n_trade_ok:,}  ({pct_with_trades:.0f}%)")
    print(f"  Without trades   : {len(gaps):,}")
    print(f"  With prices      : {n_price_ok:,}")
    print(f"  Errors           : {len(errors)}")
    print(f"  Status           : {report['summary']['status']}")
    print(f"\n  Report saved to  : {REPORT_FILE}")

    sys.exit(0 if not errors else 1)


if __name__ == "__main__":
    main()
