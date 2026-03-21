#!/usr/bin/env python3
"""
Download ALL of Cannae's closed positions via the data-api.
Resume-capable: skips already downloaded offsets.

Output: research/cannae_trades/cannae_closed_full.csv
"""
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
DATA_API = "https://data-api.polymarket.com"
BATCH = 50
OUT_DIR = Path(__file__).parent.parent / "research" / "cannae_trades"
OUT_CSV = OUT_DIR / "cannae_closed_full.csv"
PROGRESS_FILE = OUT_DIR / "closed_progress.json"

FIELDS = [
    "timestamp", "date", "condition_id", "event_slug", "title", "outcome",
    "outcome_index", "avg_price", "total_bought", "realized_pnl", "cur_price",
    "won", "end_date",
]


def fetch(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "Bottie/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
            elif e.code in (400, 404):
                return []
            elif attempt == retries - 1:
                raise
            else:
                time.sleep(2)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2)
    return []


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"offset": 0, "total": 0}


def save_progress(offset, total):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"offset": offset, "total": total}, f)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    progress = load_progress()
    start_offset = progress["offset"]

    # If resuming, open in append mode
    if start_offset > 0 and OUT_CSV.exists():
        print(f"Resuming from offset {start_offset} ({progress['total']} already downloaded)")
        mode = "a"
        write_header = False
    else:
        start_offset = 0
        mode = "w"
        write_header = True

    all_records = []
    offset = start_offset
    total = progress["total"]

    with open(OUT_CSV, mode, newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(FIELDS)

        while True:
            url = f"{DATA_API}/closed-positions?user={CANNAE}&limit={BATCH}&offset={offset}&sortBy=TIMESTAMP"
            data = fetch(url)

            if not data:
                break

            for r in data:
                ts = r.get("timestamp", 0)
                from datetime import datetime, timezone
                if ts and ts > 1000000000:
                    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    date_str = ""

                cur_price = float(r.get("curPrice", 0) or 0)
                won = 1 if cur_price >= 0.99 else 0

                writer.writerow([
                    ts,
                    date_str,
                    r.get("conditionId", ""),
                    r.get("eventSlug", ""),
                    r.get("title", ""),
                    r.get("outcome", ""),
                    r.get("outcomeIndex", ""),
                    r.get("avgPrice", 0),
                    r.get("totalBought", 0),
                    r.get("realizedPnl", 0),
                    cur_price,
                    won,
                    r.get("endDate", ""),
                ])

            total += len(data)
            offset += BATCH

            if offset % 2000 == 0:
                print(f"  offset={offset}: {total} positions downloaded", flush=True)
                save_progress(offset, total)
                f.flush()

            if len(data) < BATCH:
                break

            time.sleep(0.3)

    save_progress(offset, total)
    print(f"\nDONE: {total} closed positions → {OUT_CSV}")


if __name__ == "__main__":
    main()
