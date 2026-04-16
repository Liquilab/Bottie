#!/usr/bin/env python3
"""Map RN1's recent on-chain trades to markets and measure timing vs kickoff."""
import json, urllib.request, time, sys
from datetime import datetime, timezone
from collections import defaultdict

KEY = "FYHII55HD9YXI3TR1CGIFFS2TGQ5NQAEWJ"
RN1 = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
GAMMA = "https://gamma-api.polymarket.com"

# Get recent 1000 transfers
print("Fetching on-chain transfers...", flush=True)
url = "https://api.etherscan.io/v2/api?chainid=137&module=account&action=token1155tx&address=%s&page=1&offset=1000&sort=desc&apikey=%s" % (RN1, KEY)
data = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "S/1"}), timeout=30).read())
results = data.get("result", [])
print("Got %d transfers" % len(results), flush=True)

# Group inbound by tokenID
by_token = defaultdict(lambda: {"first": 9999999999, "last": 0, "count": 0, "total_val": 0})
for tx in results:
    if tx.get("to", "").lower() != RN1.lower():
        continue
    tid = tx.get("tokenID", "")
    ts = int(tx.get("timeStamp", 0))
    val = int(tx.get("tokenValue", "0"))
    t = by_token[tid]
    t["first"] = min(t["first"], ts)
    t["last"] = max(t["last"], ts)
    t["count"] += 1
    t["total_val"] += val

print("%d unique tokens" % len(by_token), flush=True)

# Load schedule for start times
sched = json.load(open("/opt/bottie/data/schedule_cache.json"))
slug_starts = {g["event_slug"]: g.get("start_time", "") for g in sched}

# Map tokens to markets via Gamma
print("\nMapping to markets...\n", flush=True)
found = 0
for tid, info in sorted(by_token.items(), key=lambda x: x[1]["first"]):
    if found >= 20:
        break
    time.sleep(0.4)
    try:
        gurl = "%s/markets?clob_token_ids=%%5B%%22%s%%22%%5D&limit=1" % (GAMMA, tid)
        gdata = json.loads(urllib.request.urlopen(urllib.request.Request(gurl, headers={"User-Agent": "S/1", "Accept": "application/json"}), timeout=10).read())
        if not gdata or not isinstance(gdata, list) or not gdata:
            continue
        m = gdata[0]
        question = m.get("question", "")[:55]
        event_slug = m.get("eventSlug", "") or ""
        closed = m.get("closed", False)
        outcome = m.get("groupItemTitle", "") or m.get("outcome", "") or ""

        start_time = slug_starts.get(event_slug, "")
        first_dt = datetime.fromtimestamp(info["first"], tz=timezone.utc)
        last_dt = datetime.fromtimestamp(info["last"], tz=timezone.utc)
        shares = info["total_val"] / 1e6

        delta_str = "?"
        if start_time:
            try:
                gs = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                delta_min = (info["first"] - gs.timestamp()) / 60
                delta_str = "%+.0fm" % delta_min
            except:
                pass

        found += 1
        res = "RESOLVED" if closed else "OPEN"
        print("%2d. %s  [%s]" % (found, question, res), flush=True)
        print("    outcome: %s" % outcome[:30], flush=True)
        print("    1st buy: %s  last: %s  vs kick: %s" % (first_dt.strftime("%m-%d %H:%M"), last_dt.strftime("%m-%d %H:%M"), delta_str), flush=True)
        print("    %d fills, %.0f shares, event: %s" % (info["count"], shares, event_slug[:35]), flush=True)
        print(flush=True)
    except Exception as e:
        continue

print("Done. Found %d mappable trades." % found, flush=True)
