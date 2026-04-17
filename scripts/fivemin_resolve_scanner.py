#!/usr/bin/env python3
"""Resolve scanner for fivemin-bot.

Reads PM /closed-positions for the Crypto 5M funder and appends missing resolutions
to trades.jsonl. Idempotent — dedupes by condition_id. Runs via cron, independent
of the bot's in-memory active_windows dict (which resets on every restart and
caused a 15h gap where fills resolved on-chain but were never recorded locally).

Schema match with check_resolution() in fivemin_bot.py:
  {timestamp, coin, title, condition_id, winner, fills: {outcome: shares},
   pnl, result: WIN|LOSS, cost}
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

FUNDER = os.environ.get("FIVEMIN_FUNDER", "0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a")
TRADES_FILE = Path(os.environ.get(
    "FIVEMIN_TRADES", "/opt/bottie-test/data/fivemin_bot/trades.jsonl"
))
API = "https://data-api.polymarket.com"

COIN_MAP = {
    "Bitcoin": "BTC", "Ethereum": "ETH", "Solana": "SOL",
    "XRP": "XRP", "Dogecoin": "DOGE", "BNB": "BNB", "Hype": "HYPE",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fivemin-scanner/1"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch_all_closed(funder):
    items, offset = [], 0
    while True:
        page = fetch(f"{API}/closed-positions?user={funder}&limit=100&offset={offset}")
        if not page:
            break
        items.extend(page)
        if len(page) < 100:
            break
        offset += len(page)
    return items


def existing_condition_ids(path):
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text().splitlines():
        try:
            ids.add(json.loads(line)["condition_id"])
        except Exception:
            continue
    return ids


def coin_from_title(title):
    for prefix, code in COIN_MAP.items():
        if title.startswith(prefix):
            return code
    return "UNKNOWN"


def build_record(cid, entries):
    """Collapse multiple outcome entries for the same conditionId into one trade record."""
    title = entries[0].get("title", "")
    # Winner = outcome with curPrice == 1 (resolved to $1). Fallback: positive realizedPnl.
    winner = None
    for e in entries:
        if float(e.get("curPrice", 0)) >= 0.999:
            winner = e.get("outcome")
            break
    if winner is None:
        for e in entries:
            if float(e.get("realizedPnl", 0)) > 0:
                winner = e.get("outcome")
                break
    if winner is None:
        return None  # unresolved — skip

    fills = {}
    total_pnl = 0.0
    total_cost = 0.0
    for e in entries:
        outcome = e.get("outcome")
        shares = float(e.get("totalBought", 0))
        avg = float(e.get("avgPrice", 0))
        pnl = float(e.get("realizedPnl", 0))
        if outcome and shares > 0:
            fills[outcome] = fills.get(outcome, 0.0) + shares
            total_cost += shares * avg
            total_pnl += pnl

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coin": coin_from_title(title),
        "title": title,
        "condition_id": cid,
        "winner": winner,
        "fills": fills,
        "pnl": round(total_pnl, 4),
        "result": "WIN" if total_pnl > 0 else "LOSS",
        "cost": round(total_cost, 4),
        "source": "scanner",  # mark backfilled records so we can tell them apart
    }


def main():
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    known = existing_condition_ids(TRADES_FILE)

    all_closed = fetch_all_closed(FUNDER)
    by_cid = defaultdict(list)
    for p in all_closed:
        cid = p.get("conditionId")
        title = p.get("title", "")
        if not cid or "Up or Down" not in title:
            continue
        by_cid[cid].append(p)

    new_records = []
    for cid, entries in by_cid.items():
        if cid in known:
            continue
        rec = build_record(cid, entries)
        if rec is None:
            continue
        new_records.append(rec)

    if not new_records:
        print(f"scanner: no new resolutions (known={len(known)}, checked={len(by_cid)})")
        return

    # Sort oldest-first by PM timestamp for natural ordering
    new_records.sort(key=lambda r: r["title"])
    with TRADES_FILE.open("a") as f:
        for rec in new_records:
            f.write(json.dumps(rec) + "\n")

    wins = sum(1 for r in new_records if r["result"] == "WIN")
    losses = len(new_records) - wins
    pnl = sum(r["pnl"] for r in new_records)
    print(f"scanner: appended {len(new_records)} records | {wins}W/{losses}L | PnL=${pnl:+.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"scanner: ERROR {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
