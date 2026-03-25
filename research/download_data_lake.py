#!/usr/bin/env python3
"""
Download Polymarket market data to a local parquet data lake.

Data lake layout (relative to script location):
  data_lake/
    markets.parquet              — all market metadata from Gamma API
    trades/{conditionId}.parquet — trades per market from CLOB API
    prices/{tokenId}.parquet     — price candles per token (fidelity in minutes)

Usage:
  python3 download_data_lake.py                      # full download
  python3 download_data_lake.py --markets-only       # only market metadata
  python3 download_data_lake.py --limit 5000         # first N markets only
  python3 download_data_lake.py --fidelity 60        # 1h candles (default)
  python3 download_data_lake.py --skip-prices        # skip price history
  python3 download_data_lake.py --resume             # skip already-downloaded files

Resume behaviour: existing .parquet files are never overwritten unless --force.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

BASE_DIR   = Path(__file__).parent / "data_lake"
MARKETS_FILE = BASE_DIR / "markets.parquet"
TRADES_DIR   = BASE_DIR / "trades"
PRICES_DIR   = BASE_DIR / "prices"


# ── HTTP helper ───────────────────────────────────────────────────────────────

def fetch(url: str, params: dict | None = None, retries: int = 3) -> list | dict:
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PM-DataLake/1.0", "Accept": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * (attempt + 1)
                print(f"  rate-limited, sleeping {wait}s…", flush=True)
                time.sleep(wait)
            elif attempt == retries - 1:
                raise
            else:
                time.sleep(1)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return []


# ── Token ID parsing ──────────────────────────────────────────────────────────

def parse_token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [str(t) for t in parsed if t]
        except (json.JSONDecodeError, TypeError):
            return []
    return []


# ── Step 1: Markets ───────────────────────────────────────────────────────────

def download_markets(limit: int | None = None) -> pd.DataFrame:
    all_markets: list[dict] = []
    offset = 0
    page_size = 500
    print("Downloading markets from Gamma API…", flush=True)

    while True:
        if limit and len(all_markets) >= limit:
            break
        try:
            data = fetch(f"{GAMMA_API}/markets", {
                "limit": page_size,
                "offset": offset,
                "order": "id",
                "ascending": "true",
            })
        except Exception as e:
            print(f"  ⚠  Error at offset {offset}: {e}", flush=True)
            break

        # empty array = stop signal
        if not data:
            break
        rows = data if isinstance(data, list) else [data]
        all_markets.extend(rows)
        print(f"  offset={offset:7d} → {len(all_markets):7d} markets", flush=True)

        if len(rows) < page_size:
            break
        offset += page_size
        time.sleep(0.1)

    if limit:
        all_markets = all_markets[:limit]

    print(f"  Total: {len(all_markets)} markets", flush=True)
    return pd.DataFrame(all_markets)


# ── Step 2: Trades ────────────────────────────────────────────────────────────

def download_trades(condition_id: str) -> pd.DataFrame | None:
    trades: list[dict] = []
    offset = 0
    page_size = 1000

    while True:
        try:
            data = fetch(f"{CLOB_API}/trades", {
                "market": condition_id,   # NOTE: param name is "market"
                "limit": page_size,
                "offset": offset,
            })
        except Exception:
            break

        records = data if isinstance(data, list) else data.get("data", [])
        if not records:
            break
        trades.extend(records)
        if len(records) < page_size:
            break
        offset += page_size
        time.sleep(0.05)

    return pd.DataFrame(trades) if trades else None


# ── Step 3: Price history ─────────────────────────────────────────────────────

def download_prices(token_id: str, fidelity: int = 60) -> pd.DataFrame | None:
    try:
        data = fetch(f"{CLOB_API}/prices-history", {
            "market": token_id,
            "interval": "max",
            "fidelity": fidelity,
        })
    except Exception:
        return None

    history = data if isinstance(data, list) else data.get("history", [])
    if not history:
        return None
    df = pd.DataFrame(history)
    df["tokenId"] = token_id
    return df


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download Polymarket data lake")
    parser.add_argument("--markets-only", action="store_true",
                        help="Only download market metadata, skip trades+prices")
    parser.add_argument("--skip-prices", action="store_true",
                        help="Download trades but skip price history")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of markets to process")
    parser.add_argument("--fidelity", type=int, default=60,
                        help="Price candle width in minutes (default: 60)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if parquet already exists")
    args = parser.parse_args()

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_DIR.mkdir(exist_ok=True)
    PRICES_DIR.mkdir(exist_ok=True)

    # ── Step 1: Markets ────────────────────────────────────────────────────
    if not MARKETS_FILE.exists() or args.force:
        df_markets = download_markets(limit=args.limit)
        df_markets.to_parquet(MARKETS_FILE, index=False)
        print(f"✓ Saved {MARKETS_FILE} ({len(df_markets):,} rows)", flush=True)
    else:
        df_markets = pd.read_parquet(MARKETS_FILE)
        if args.limit:
            df_markets = df_markets.head(args.limit)
        print(f"✓ Loaded existing {MARKETS_FILE} ({len(df_markets):,} rows)", flush=True)

    if args.markets_only:
        print("Done (--markets-only).")
        return

    # ── Step 2+3: Trades + Prices per market ──────────────────────────────
    n = len(df_markets)
    n_trades_ok = n_trades_skip = n_trades_err = 0
    n_prices_ok = n_prices_skip = n_prices_err = 0

    for i, row in df_markets.iterrows():
        cid = str(row.get("conditionId") or row.get("id") or "").strip()
        if not cid:
            continue

        # Trades
        trades_file = TRADES_DIR / f"{cid}.parquet"
        if trades_file.exists() and not args.force:
            n_trades_ok += 1
        else:
            df_t = download_trades(cid)
            if df_t is not None and len(df_t) > 0:
                df_t.to_parquet(trades_file, index=False)
                n_trades_ok += 1
            elif df_t is None:
                n_trades_err += 1
            else:
                n_trades_skip += 1  # 0-trade market (resolved with no activity)

        # Price history per token
        if not args.skip_prices:
            for tid in parse_token_ids(dict(row)):
                pfile = PRICES_DIR / f"{tid}.parquet"
                if pfile.exists() and not args.force:
                    n_prices_ok += 1
                else:
                    df_p = download_prices(tid, fidelity=args.fidelity)
                    if df_p is not None and len(df_p) > 0:
                        df_p.to_parquet(pfile, index=False)
                        n_prices_ok += 1
                    elif df_p is None:
                        n_prices_err += 1
                    else:
                        n_prices_skip += 1

        if (i + 1) % 200 == 0 or (i + 1) == n:
            pct = (i + 1) / n * 100
            print(
                f"  [{i+1:6d}/{n}] {pct:4.0f}%  "
                f"trades: {n_trades_ok}✓ {n_trades_skip}∅ {n_trades_err}✗  "
                f"prices: {n_prices_ok}✓ {n_prices_skip}∅ {n_prices_err}✗",
                flush=True,
            )
        time.sleep(0.05)

    print("\n── Summary ──────────────────────────────────────────")
    print(f"  Markets  : {n:,}")
    print(f"  Trades   : {n_trades_ok:,} downloaded  {n_trades_skip:,} no-trades  {n_trades_err:,} errors")
    print(f"  Prices   : {n_prices_ok:,} downloaded  {n_prices_skip:,} no-history  {n_prices_err:,} errors")
    print(f"  Data lake: {BASE_DIR}")


if __name__ == "__main__":
    main()
