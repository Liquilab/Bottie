#!/usr/bin/env python3
"""
RUS-234 Fase 1: Download price history for sport ML+Draw markets.

Uses existing data_lake/markets.parquet to identify relevant markets,
then downloads price candles via CLOB /prices-history.

Saves per-token parquet files in data_lake/prices/{tokenId}.parquet.
Resume-safe: skips already-downloaded tokens.

Usage:
  python3 download_sport_prices.py                   # all sport ML+Draw since 2024-06
  python3 download_sport_prices.py --since 2025-01   # custom start date
  python3 download_sport_prices.py --fidelity 1      # 1-minute candles (vs default 60)
  python3 download_sport_prices.py --limit 100       # first N markets only (for testing)
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

CLOB_API = "https://clob.polymarket.com"
BASE_DIR = Path(__file__).parent / "data_lake"
PRICES_DIR = BASE_DIR / "prices"
MARKETS_FILE = BASE_DIR / "markets.parquet"
CHECKPOINT_FILE = BASE_DIR / "sport_prices_checkpoint.json"


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


def parse_token_ids(clob_ids_raw) -> list[str]:
    """Parse clobTokenIds from market row (can be JSON string or list)."""
    if not clob_ids_raw:
        return []
    if isinstance(clob_ids_raw, list):
        return [str(t) for t in clob_ids_raw if t]
    if isinstance(clob_ids_raw, str):
        try:
            parsed = json.loads(clob_ids_raw)
            return [str(t) for t in parsed if t]
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def get_sport_markets(con: duckdb.DuckDBPyConnection, since: str, limit: int | None = None) -> list[dict]:
    """Query sport ML+Draw markets from markets.parquet."""
    limit_clause = f"LIMIT {limit}" if limit else ""

    query = f"""
        SELECT
            id,
            question,
            slug,
            sportsMarketType,
            clobTokenIds,
            volumeNum,
            endDate,
            closedTime,
            conditionId,
            outcomes,
            outcomePrices
        FROM '{MARKETS_FILE}'
        WHERE (
            sportsMarketType IN ('moneyline')
            OR slug LIKE '%-draw%'
            OR (question LIKE '%draw%' AND sportsMarketType IS NULL
                AND (slug LIKE '%ucl%' OR slug LIKE '%epl%' OR slug LIKE '%bun%'
                     OR slug LIKE '%lal%' OR slug LIKE '%sea%' OR slug LIKE '%fl1%'
                     OR slug LIKE '%tur%' OR slug LIKE '%uel%' OR slug LIKE '%bra%'
                     OR slug LIKE '%mex%' OR slug LIKE '%nhl%' OR slug LIKE '%nba%'))
        )
        AND closed = true
        AND volumeNum > 500
        AND clobTokenIds IS NOT NULL
        AND startDateIso >= '{since}'
        ORDER BY endDate DESC
        {limit_clause}
    """
    rows = con.execute(query).fetchall()
    columns = [desc[0] for desc in con.execute(query).description]

    # Re-run to get column names properly
    result = con.execute(query)
    columns = [desc[0] for desc in result.description]
    # Already fetched, re-execute
    result = con.execute(query)
    rows = result.fetchall()

    markets = []
    for row in rows:
        m = dict(zip(columns, row))
        markets.append(m)
    return markets


def download_prices(token_id: str, fidelity: int = 60) -> list[dict] | None:
    """Download price candles for a single token."""
    try:
        data = fetch(f"{CLOB_API}/prices-history", {
            "market": token_id,
            "interval": "max",
            "fidelity": fidelity,
        })
    except Exception as e:
        return None

    history = data if isinstance(data, list) else data.get("history", [])
    if not history:
        return None
    return history


def save_checkpoint(state: dict):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(state, f)


def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"completed_tokens": 0, "total_records": 0}


def main():
    parser = argparse.ArgumentParser(description="RUS-234: Download sport price history")
    parser.add_argument("--since", default="2024-06-01",
                        help="Start date for markets (default: 2024-06-01)")
    parser.add_argument("--fidelity", type=int, default=60,
                        help="Candle width in minutes (1=1min, 60=1hr, default: 60)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max markets to process (for testing)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download existing files")
    args = parser.parse_args()

    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    if not MARKETS_FILE.exists():
        print(f"ERROR: {MARKETS_FILE} not found. Run download_data_lake.py --markets-only first.")
        sys.exit(1)

    con = duckdb.connect()

    print(f"Querying sport ML+Draw markets since {args.since}…", flush=True)
    markets = get_sport_markets(con, args.since, args.limit)
    print(f"Found {len(markets):,} markets", flush=True)

    # Collect all token IDs with metadata
    tokens = []
    for m in markets:
        tids = parse_token_ids(m.get("clobTokenIds"))
        outcomes_raw = m.get("outcomes", "[]")
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
        except:
            outcomes = []

        for i, tid in enumerate(tids):
            outcome = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
            tokens.append({
                "token_id": tid,
                "market_id": m.get("id"),
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "sport_type": m.get("sportsMarketType", "draw" if "draw" in (m.get("slug") or "") else "unknown"),
                "outcome": outcome,
                "condition_id": m.get("conditionId", ""),
                "volume": m.get("volumeNum", 0),
                "end_date": m.get("endDate", ""),
            })

    print(f"Total tokens to download: {len(tokens):,}", flush=True)

    # Count already downloaded
    already = sum(1 for t in tokens if (PRICES_DIR / f"{t['token_id']}.parquet").exists())
    remaining = len(tokens) - already if not args.force else len(tokens)
    print(f"Already downloaded: {already:,}, remaining: {remaining:,}", flush=True)
    print(f"Estimated time: {remaining * 0.15 / 60:.0f} minutes\n", flush=True)

    # Download
    n_ok = n_skip = n_empty = n_err = 0
    total_records = 0
    import pandas as pd

    for i, t in enumerate(tokens):
        tid = t["token_id"]
        pfile = PRICES_DIR / f"{tid}.parquet"

        if pfile.exists() and not args.force:
            n_skip += 1
            continue

        history = download_prices(tid, args.fidelity)
        if history is None:
            n_err += 1
        elif len(history) == 0:
            n_empty += 1
        else:
            df = pd.DataFrame(history)
            df["tokenId"] = tid
            df["marketId"] = t["market_id"]
            df["question"] = t["question"]
            df["slug"] = t["slug"]
            df["sportType"] = t["sport_type"]
            df["outcome"] = t["outcome"]
            df["conditionId"] = t["condition_id"]
            df.to_parquet(pfile, index=False)
            n_ok += 1
            total_records += len(df)

        # Progress
        done = n_ok + n_skip + n_empty + n_err
        if done % 100 == 0 or done == len(tokens):
            pct = done / len(tokens) * 100
            print(
                f"  [{done:6d}/{len(tokens)}] {pct:5.1f}%  "
                f"ok:{n_ok} skip:{n_skip} empty:{n_empty} err:{n_err}  "
                f"records:{total_records:,}",
                flush=True,
            )

        time.sleep(0.1)  # Rate limit: 10 req/sec

    print(f"\n{'='*60}")
    print(f"  DONE — RUS-234 Price Download")
    print(f"{'='*60}")
    print(f"  Markets:       {len(markets):,}")
    print(f"  Tokens total:  {len(tokens):,}")
    print(f"  Downloaded:    {n_ok:,}")
    print(f"  Skipped (exist): {n_skip:,}")
    print(f"  Empty:         {n_empty:,}")
    print(f"  Errors:        {n_err:,}")
    print(f"  Total records: {total_records:,}")
    print(f"  Data dir:      {PRICES_DIR}")

    # Save token index for fase 2
    index_file = BASE_DIR / "sport_token_index.json"
    with open(index_file, "w") as f:
        json.dump(tokens, f, indent=2, default=str)
    print(f"  Token index:   {index_file}")


if __name__ == "__main__":
    main()
