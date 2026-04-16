#!/usr/bin/env python3
"""Analyze WHEN RN1 places trades relative to game start."""
import json, urllib.request, time
from datetime import datetime, timezone
from collections import defaultdict

API = "https://data-api.polymarket.com"
ADDR = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea".lower()

def api_get(url):
    time.sleep(0.15)
    req = urllib.request.Request(url, headers={"User-Agent": "S/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

# Load schedule
sched = json.load(open("/opt/bottie/data/schedule_cache.json"))
slug_cids = {}
slug_starts = {}
for g in sched:
    s = g.get("event_slug", "")
    slug_cids[s] = g.get("condition_ids", [])
    slug_starts[s] = g.get("start_time", "")

# Known both-sides events
events = [
    "atp-martine-sonego-2026-04-14",
    "atp-ivashka-sweeny-2026-04-12",
    "nhl-col-edm-2026-04-13",
    "atp-tokuda-wong-2026-04-13",
    "atp-xiao-ellis-2026-04-12",
    "mlb-nym-lad-2026-04-13",
    "atp-bolt-mccabe-2026-04-12",
    "atp-dellie-prado-2026-04-13",
    "atp-gray-binda-2026-04-12",
    "dota2-sar1-heroic-2026-04-13",
]

print("Event                                  Kick    Outcome                1st trade    Last trade   vs kick  #tx   avg    shares")
print("-" * 130)

pre_game_count = 0
during_count = 0

for slug in events:
    cids = slug_cids.get(slug, [])
    start_str = slug_starts.get(slug, "")
    if not cids or not start_str:
        continue
    try:
        gs = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except:
        continue

    gs_str = gs.strftime("%H:%M")

    for cid in cids[:3]:
        # Fetch ALL trades on this market, filter for RN1
        try:
            all_trades = api_get(f"{API}/trades?market={cid}&limit=500")
        except:
            continue

        rn1_buys = [t for t in all_trades
                    if (t.get("proxyWallet") or "").lower() == ADDR
                    and (t.get("side") or "").upper() == "BUY"]

        if not rn1_buys:
            continue

        by_outcome = defaultdict(list)
        for t in rn1_buys:
            by_outcome[t.get("outcome", "?")].append(t)

        for outcome, trades in by_outcome.items():
            timestamps = [int(t.get("timestamp", 0)) for t in trades]
            first_ts = min(timestamps)
            last_ts = max(timestamps)
            first_dt = datetime.fromtimestamp(first_ts, tz=timezone.utc)
            last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)

            delta_min = (first_ts - gs.timestamp()) / 60
            if delta_min < -5:
                pre_game_count += 1
            else:
                during_count += 1

            total_size = sum(float(t.get("size", 0)) for t in trades)
            total_cost = sum(float(t.get("price", 0)) * float(t.get("size", 0)) for t in trades)
            avg_price = total_cost / total_size if total_size > 0 else 0

            delta_str = "%+.0fm" % delta_min
            first_str = first_dt.strftime("%m-%d %H:%M")
            last_str = last_dt.strftime("%m-%d %H:%M")

            print("%-40s %5s   %-22s %12s %12s %8s %4d  %4.0fc  %8.0fsh" % (
                slug[:39], gs_str, outcome[:21],
                first_str, last_str, delta_str,
                len(trades), avg_price * 100, total_size))

    print()

print("=== PATTERN ===")
print("Pre-game (>5min before kick): %d" % pre_game_count)
print("At/during game:               %d" % during_count)
