#!/usr/bin/env python3
"""Live monitor: watch RN1 build positions on live games.

Tracks per conditionId (not eventSlug) to avoid sub-market confusion.
Only reports genuine new positions and size increases (buys).
"""
import json, urllib.request, time
from datetime import datetime, timezone
from collections import defaultdict

API = "https://data-api.polymarket.com"
RN1 = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
POLL_INTERVAL = 60

def api_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "S/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

# State: conditionId → {iv, size, outcome, slug, title}
prev_state = {}

def poll():
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%H:%M:%S")

    try:
        positions = api_get(f"{API}/positions?user={RN1}&limit=500&sizeThreshold=0.01")
    except Exception as e:
        print("[%s] Error: %s" % (now_str, e), flush=True)
        return

    curr_state = {}
    for p in positions:
        cid = p.get("conditionId", "") or ""
        if not cid:
            continue
        iv = float(p.get("initialValue", 0) or 0)
        size = float(p.get("size", 0))
        if size < 0.01:
            continue
        curr_state[cid] = {
            "iv": iv,
            "size": size,
            "outcome": p.get("outcome", ""),
            "slug": p.get("eventSlug", "") or "",
            "title": (p.get("title", "") or "")[:55],
            "avg": iv / size if size > 0 else 0,
        }

    if not prev_state:
        # First poll — just record state, don't print everything
        prev_state.update(curr_state)
        print("[%s] Initial snapshot: %d positions tracked" % (now_str, len(curr_state)), flush=True)
        return

    # Compare: find NEW positions and INCREASED positions (buys only)
    for cid, curr in curr_state.items():
        if cid not in prev_state:
            # Genuinely new position
            print("[%s] NEW: %-20s | %s | $%.0f | %.0f sh @ %.0fc | %s" % (
                now_str, curr["outcome"][:20], curr["slug"][:35],
                curr["iv"], curr["size"], curr["avg"] * 100, curr["title"]), flush=True)
        else:
            prev = prev_state[cid]
            delta_iv = curr["iv"] - prev["iv"]
            delta_size = curr["size"] - prev["size"]

            if delta_iv > 0.50:
                # Position grew = BUY
                new_avg = delta_iv / delta_size if delta_size > 0 else 0
                print("[%s] BUY: %-20s | %s | +$%.0f (+%.0f sh @ %.0fc) → total $%.0f %.0f sh" % (
                    now_str, curr["outcome"][:20], curr["slug"][:35],
                    delta_iv, delta_size, new_avg * 100,
                    curr["iv"], curr["size"]), flush=True)
            elif delta_iv < -0.50:
                # Position shrank = SELL or resolution
                print("[%s] OUT: %-20s | %s | $%.0f → $%.0f (%.0f sh → %.0f sh)" % (
                    now_str, curr["outcome"][:20], curr["slug"][:35],
                    prev["iv"], curr["iv"], prev["size"], curr["size"]), flush=True)

    # Check for disappeared positions (fully resolved/sold)
    for cid, prev in prev_state.items():
        if cid not in curr_state and prev["iv"] > 1:
            print("[%s] GONE: %-20s | %s | was $%.0f %.0f sh" % (
                now_str, prev["outcome"][:20], prev["slug"][:35],
                prev["iv"], prev["size"]), flush=True)

    prev_state.clear()
    prev_state.update(curr_state)


print("=== RN1 LIVE MONITOR v2 (per conditionId) ===", flush=True)
print("Polling every %ds. Only genuine buys/sells." % POLL_INTERVAL, flush=True)
print(flush=True)

poll()  # initial snapshot
print("\n--- Monitoring ---\n", flush=True)

while True:
    time.sleep(POLL_INTERVAL)
    poll()
