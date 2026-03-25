#!/usr/bin/env python3
"""
Set up DuckDB over the Polymarket data lake parquet files.

Creates a persistent DuckDB database with views for:
  - markets      : all market metadata
  - trades       : all trades (unioned across parquet files)
  - prices       : all price history (unioned across parquet files)

Then runs 5 example queries and prints results.

Usage:
  python3 setup_duckdb.py                         # setup + run examples
  python3 setup_duckdb.py --db /tmp/pm.duckdb     # custom db path
  python3 setup_duckdb.py --queries-only          # skip setup, just run queries
  python3 setup_duckdb.py --interactive           # open DuckDB REPL after setup

Requirements:
  pip install duckdb pandas
"""

import argparse
import sys
import textwrap
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("ERROR: duckdb not installed. Run: pip install duckdb")
    sys.exit(1)

BASE_DIR     = Path(__file__).parent / "data_lake"
MARKETS_FILE = BASE_DIR / "markets.parquet"
TRADES_DIR   = BASE_DIR / "trades"
PRICES_DIR   = BASE_DIR / "prices"
DEFAULT_DB   = BASE_DIR / "polymarket.duckdb"


# ── Setup views ───────────────────────────────────────────────────────────────

def setup_views(con: duckdb.DuckDBPyConnection) -> None:
    """Create views over all parquet files."""

    # markets view (single file)
    if MARKETS_FILE.exists():
        con.execute(f"""
            CREATE OR REPLACE VIEW markets AS
            SELECT * FROM read_parquet('{MARKETS_FILE}')
        """)
        n = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        print(f"  ✓ markets view   — {n:,} rows")
    else:
        print(f"  ✗ markets.parquet not found at {MARKETS_FILE}")

    # trades view (glob over trades/*.parquet)
    trades_glob = str(TRADES_DIR / "*.parquet")
    trade_files = list(TRADES_DIR.glob("*.parquet")) if TRADES_DIR.exists() else []
    if trade_files:
        con.execute(f"""
            CREATE OR REPLACE VIEW trades AS
            SELECT * FROM read_parquet('{trades_glob}', filename=true)
        """)
        n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        print(f"  ✓ trades view    — {n:,} rows  ({len(trade_files):,} files)")
    else:
        print(f"  ✗ No trade parquet files found in {TRADES_DIR}")

    # prices view (glob over prices/*.parquet)
    prices_glob = str(PRICES_DIR / "*.parquet")
    price_files = list(PRICES_DIR.glob("*.parquet")) if PRICES_DIR.exists() else []
    if price_files:
        con.execute(f"""
            CREATE OR REPLACE VIEW prices AS
            SELECT * FROM read_parquet('{prices_glob}', filename=true)
        """)
        n = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        print(f"  ✓ prices view    — {n:,} rows  ({len(price_files):,} files)")
    else:
        print(f"  ✗ No price parquet files found in {PRICES_DIR}")


# ── Example queries ───────────────────────────────────────────────────────────

QUERIES = [

    # Q1: Resolved markets per category
    (
        "Q1 — Resolved markets per category",
        """
        SELECT
            COALESCE(category, 'unknown')           AS category,
            COUNT(*)                                AS total_markets,
            SUM(CASE WHEN resolved THEN 1 ELSE 0 END) AS resolved,
            ROUND(
                100.0 * SUM(CASE WHEN resolved THEN 1 ELSE 0 END) / COUNT(*), 1
            )                                       AS pct_resolved
        FROM markets
        GROUP BY category
        ORDER BY resolved DESC
        """,
    ),

    # Q2: Average volume per category
    (
        "Q2 — Average volume per category",
        """
        SELECT
            COALESCE(category, 'unknown')   AS category,
            COUNT(*)                        AS markets,
            ROUND(AVG(TRY_CAST(volume AS DOUBLE)), 2)  AS avg_volume,
            ROUND(SUM(TRY_CAST(volume AS DOUBLE)), 2)  AS total_volume
        FROM markets
        WHERE TRY_CAST(volume AS DOUBLE) IS NOT NULL
        GROUP BY category
        ORDER BY total_volume DESC
        """,
    ),

    # Q3: Markets where final price deviated >20% from price 24h before resolution
    # Uses prices table: finds last price per token, and price closest to 24h before end
    (
        "Q3 — Markets: final price >20% away from price 24h before resolution",
        """
        WITH token_prices AS (
            SELECT
                tokenId,
                CAST(t AS BIGINT)    AS ts,
                CAST(p AS DOUBLE)    AS price
            FROM prices
            WHERE TRY_CAST(t AS BIGINT) IS NOT NULL
              AND TRY_CAST(p AS DOUBLE) IS NOT NULL
        ),
        market_tokens AS (
            -- Unnest clobTokenIds (stored as JSON array string or already array)
            SELECT
                conditionId,
                question,
                endDateIso,
                EPOCH(TRY_CAST(endDateIso AS TIMESTAMP)) AS end_ts,
                UNNEST(
                    TRY_CAST(clobTokenIds AS VARCHAR[])
                ) AS tokenId
            FROM markets
            WHERE resolved = true
              AND endDateIso IS NOT NULL
              AND clobTokenIds IS NOT NULL
        ),
        final_prices AS (
            SELECT
                mt.conditionId,
                mt.question,
                mt.end_ts,
                tp.price                              AS final_price,
                tp.ts                                 AS final_ts
            FROM market_tokens mt
            JOIN token_prices tp ON tp.tokenId = mt.tokenId
            WHERE ABS(tp.ts - mt.end_ts) = (
                SELECT MIN(ABS(tp2.ts - mt.end_ts))
                FROM token_prices tp2
                WHERE tp2.tokenId = tp.tokenId
            )
        ),
        day_before_prices AS (
            SELECT
                mt.conditionId,
                tp.price                              AS day_before_price,
                tp.ts                                 AS day_before_ts
            FROM market_tokens mt
            JOIN token_prices tp ON tp.tokenId = mt.tokenId
            WHERE ABS(tp.ts - (mt.end_ts - 86400)) = (
                SELECT MIN(ABS(tp2.ts - (mt.end_ts - 86400)))
                FROM token_prices tp2
                WHERE tp2.tokenId = tp.tokenId
            )
        )
        SELECT
            fp.conditionId,
            fp.question,
            ROUND(dbp.day_before_price, 3)            AS price_24h_before,
            ROUND(fp.final_price, 3)                  AS final_price,
            ROUND(ABS(fp.final_price - dbp.day_before_price), 3) AS deviation
        FROM final_prices fp
        JOIN day_before_prices dbp ON fp.conditionId = dbp.conditionId
        WHERE ABS(fp.final_price - dbp.day_before_price) > 0.20
        ORDER BY deviation DESC
        LIMIT 20
        """,
    ),

    # Q4: Top 10 wallets by trade volume
    (
        "Q4 — Top 10 wallets by trade volume",
        """
        SELECT
            COALESCE(maker, takerFill, 'unknown')   AS wallet,
            COUNT(*)                                 AS n_trades,
            ROUND(SUM(TRY_CAST(size AS DOUBLE)), 2)  AS total_size,
            ROUND(AVG(TRY_CAST(price AS DOUBLE)), 4) AS avg_price,
            MIN(TRY_CAST(timestamp AS BIGINT))       AS first_trade_ts,
            MAX(TRY_CAST(timestamp AS BIGINT))       AS last_trade_ts
        FROM trades
        WHERE COALESCE(maker, takerFill) IS NOT NULL
        GROUP BY COALESCE(maker, takerFill, 'unknown')
        ORDER BY total_size DESC NULLS LAST
        LIMIT 10
        """,
    ),

    # Q5: Cross-market — events where sum of YES prices deviates from 1.00
    # Finds markets grouped by groupItemTitle (event) with >1 outcome
    (
        "Q5 — Cross-market: events where YES-prices sum deviates from 1.00",
        """
        WITH latest_prices AS (
            -- Get the most recent price per token
            SELECT
                tokenId,
                price AS latest_price
            FROM (
                SELECT
                    tokenId,
                    CAST(p AS DOUBLE) AS price,
                    ROW_NUMBER() OVER (
                        PARTITION BY tokenId
                        ORDER BY TRY_CAST(t AS BIGINT) DESC
                    ) AS rn
                FROM prices
                WHERE TRY_CAST(p AS DOUBLE) IS NOT NULL
            )
            WHERE rn = 1
        ),
        market_yes_prices AS (
            -- For each market, get the YES token price
            -- YES token is typically the first clobTokenId
            SELECT
                m.groupItemTitle,
                m.conditionId,
                m.question,
                lp.latest_price        AS yes_price
            FROM markets m
            CROSS JOIN LATERAL (
                SELECT tokenId
                FROM (
                    VALUES (
                        LIST_ELEMENT(
                            TRY_CAST(m.clobTokenIds AS VARCHAR[]), 1
                        )
                    ) t(tokenId)
                ) WHERE tokenId IS NOT NULL
            ) tok
            LEFT JOIN latest_prices lp ON lp.tokenId = tok.tokenId
            WHERE m.resolved = false
              AND m.groupItemTitle IS NOT NULL
              AND lp.latest_price IS NOT NULL
        ),
        event_sums AS (
            SELECT
                groupItemTitle          AS event,
                COUNT(*)                AS n_outcomes,
                ROUND(SUM(yes_price), 4) AS sum_yes_prices,
                ROUND(ABS(SUM(yes_price) - 1.0), 4) AS deviation_from_1
            FROM market_yes_prices
            GROUP BY groupItemTitle
            HAVING COUNT(*) >= 2
        )
        SELECT *
        FROM event_sums
        ORDER BY deviation_from_1 DESC
        LIMIT 20
        """,
    ),
]


def run_queries(con: duckdb.DuckDBPyConnection, verbose: bool = True) -> None:
    for title, sql in QUERIES:
        print(f"\n{'─'*70}")
        print(f"  {title}")
        print(f"{'─'*70}")
        try:
            df = con.execute(textwrap.dedent(sql)).df()
            if df.empty:
                print("  (no results)")
            else:
                print(df.to_string(index=False, max_rows=25))
        except Exception as e:
            print(f"  ⚠  Query failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB),
                        help=f"DuckDB file path (default: {DEFAULT_DB})")
    parser.add_argument("--queries-only", action="store_true",
                        help="Skip view setup, only run example queries")
    parser.add_argument("--interactive", action="store_true",
                        help="Drop into DuckDB CLI after setup")
    args = parser.parse_args()

    db_path = args.db
    print(f"DuckDB: {db_path}")
    con = duckdb.connect(db_path)

    if not args.queries_only:
        print("\nCreating views…")
        setup_views(con)

    print("\nRunning example queries…")
    run_queries(con)

    # Print useful ad-hoc query hints
    print(f"\n{'═'*70}")
    print("  Database ready. Useful queries:")
    print(f"{'═'*70}")
    print(f"""
  -- Open in DuckDB CLI:
  duckdb {db_path}

  -- Quick counts:
  SELECT COUNT(*) FROM markets;
  SELECT COUNT(*) FROM trades;
  SELECT COUNT(*) FROM prices;

  -- Schema inspection:
  DESCRIBE markets;
  DESCRIBE trades;
  DESCRIBE prices;

  -- Trades for a specific market:
  SELECT * FROM trades WHERE market = '0xabc...' LIMIT 20;

  -- Price history for a token:
  SELECT *, to_timestamp(CAST(t AS BIGINT)) AS dt FROM prices
  WHERE tokenId = '123...'
  ORDER BY t DESC LIMIT 20;

  -- Markets resolving this week:
  SELECT question, endDateIso, volume FROM markets
  WHERE endDateIso >= current_date
    AND endDateIso <= current_date + INTERVAL 7 DAYS
  ORDER BY endDateIso;
""")

    if args.interactive:
        import subprocess
        subprocess.run(["duckdb", db_path])

    con.close()


if __name__ == "__main__":
    main()
