#!/usr/bin/env python3
"""ELK full history analysis via Polygonscan + CLOB resolution."""
import json, urllib.request, time
from datetime import datetime, timezone
from collections import defaultdict

KEY = "FYHII55HD9YXI3TR1CGIFFS2TGQ5NQAEWJ"
ELK = "0xead152b855effa6b5b5837f53b24c0756830c76a"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

def api_get(url, timeout=30):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "S/1", "Accept": "application/json"}),
        timeout=timeout).read())

# Step 1: Get ALL on-chain transfers (newest first, we need recent ones that have market data)
print("Fetching ELK on-chain transfers (newest first)...", flush=True)
all_tx = []
for page in range(1, 30):
    url = "https://api.etherscan.io/v2/api?chainid=137&module=account&action=token1155tx&address=%s&page=%d&offset=1000&sort=desc&apikey=%s" % (ELK, page, KEY)
    time.sleep(1.5)
    try:
        data = api_get(url)
        results = data.get("result", [])
        if not isinstance(results, list) or not results:
            break
        all_tx.extend(results)
        oldest = datetime.fromtimestamp(int(results[-1].get("timeStamp", 0)), tz=timezone.utc)
        print("  Page %d: %d tx, oldest: %s" % (page, len(results), oldest.strftime("%Y-%m-%d")), flush=True)
        if len(results) < 1000:
            break
    except Exception as e:
        print("  Page %d error: %s" % (page, e), flush=True)
        break

print("Total: %d transfers" % len(all_tx), flush=True)

# Step 2: Group inbound transfers by tokenID
by_token = defaultdict(lambda: {"first": 9999999999, "last": 0, "count": 0, "total_val": 0})
for tx in all_tx:
    if tx.get("to", "").lower() != ELK.lower():
        continue
    tid = tx.get("tokenID", "")
    ts = int(tx.get("timeStamp", 0))
    val = int(tx.get("tokenValue", "0"))
    t = by_token[tid]
    t["first"] = min(t["first"], ts)
    t["last"] = max(t["last"], ts)
    t["count"] += 1
    t["total_val"] += val

print("Unique tokens (positions): %d" % len(by_token), flush=True)

# Step 3: Load resolution cache
cache = json.load(open("/opt/bottie/data/resolution_cache.json"))

# Step 4: For each token, find the conditionId via CLOB book endpoint
# The token_id IS the CLOB token — we can get the conditionId by checking
# current positions (for active ones) or by trying the CLOB market lookup
#
# Strategy: build a token->conditionId map from current positions first,
# then for unknown tokens, try the CLOB /book endpoint (if it returns data,
# the token is still active)

# Load current positions for mapping
print("Loading current positions for token mapping...", flush=True)
pos = api_get("https://data-api.polymarket.com/positions?user=%s&limit=500&sizeThreshold=0.01" % ELK)
token_to_info = {}
for p in pos:
    asset = p.get("asset", "")
    if asset:
        token_to_info[asset] = {
            "cid": p.get("conditionId", ""),
            "outcome": p.get("outcome", ""),
            "slug": p.get("eventSlug", "") or "",
            "title": (p.get("title", "") or "")[:45],
        }

print("Mapped %d tokens from current positions" % len(token_to_info), flush=True)

# Step 5: For tokens not in current positions, check if they're in the resolution cache
# by trying to look up via the CLOB book endpoint (returns conditionId)
unmapped = [tid for tid in by_token if tid not in token_to_info]
print("Unmapped tokens: %d — checking CLOB..." % len(unmapped), flush=True)

mapped_via_clob = 0
for i, tid in enumerate(unmapped):
    if i % 50 == 0 and i > 0:
        print("  %d/%d (found %d)..." % (i, len(unmapped), mapped_via_clob), flush=True)
    time.sleep(0.12)
    try:
        book = api_get("%s/book?token_id=%s" % (CLOB, tid), timeout=10)
        # Book response has market field or we can extract conditionId
        if book and "market" in book:
            cid = book.get("market", "")
            if cid:
                # Check resolution
                if cid not in cache:
                    time.sleep(0.12)
                    try:
                        mkt = api_get("%s/markets/%s" % (CLOB, cid), timeout=10)
                        closed = mkt.get("closed", False)
                        winner = None
                        for tok in mkt.get("tokens", []):
                            if tok.get("winner") is True:
                                winner = tok.get("outcome")
                        cache[cid] = {"resolved": closed and winner is not None, "winner": winner}
                    except:
                        cache[cid] = None

                # Try to get question/slug from market
                question = ""
                try:
                    if cid not in token_to_info:
                        mkt = api_get("%s/markets/%s" % (CLOB, cid), timeout=10)
                        question = mkt.get("question", "")[:45]
                except:
                    pass

                token_to_info[tid] = {
                    "cid": cid,
                    "outcome": "",  # don't know from book
                    "slug": "",
                    "title": question,
                }
                mapped_via_clob += 1
    except:
        continue

json.dump(cache, open("/opt/bottie/data/resolution_cache.json", "w"))
print("Mapped %d additional tokens via CLOB" % mapped_via_clob, flush=True)
print("Total mapped: %d of %d" % (len(token_to_info), len(by_token)), flush=True)

# Step 6: Analyze resolved positions
print("\n=== RESULTS ===\n", flush=True)

wins = 0
losses = 0
open_count = 0
unmapped_count = 0
win_pnl = 0
loss_pnl = 0

resolved_trades = []

for tid, info in by_token.items():
    mapping = token_to_info.get(tid)
    if not mapping:
        unmapped_count += 1
        continue

    cid = mapping["cid"]
    c = cache.get(cid, {})
    if not c or not c.get("resolved"):
        open_count += 1
        continue

    # Determine if this token won
    winner = c.get("winner", "")
    outcome = mapping.get("outcome", "")

    # If outcome is empty (CLOB lookup), try to determine from token position in market
    # For now, we can check: if the token has value (still exists in positions), it might have won
    # This is imperfect — skip if no outcome
    if not outcome:
        unmapped_count += 1
        continue

    won = outcome == winner
    shares = info["total_val"] / 1e6
    avg_price = 0.5  # we don't have exact price from on-chain, estimate
    # Better: use initialValue from positions if available
    iv = shares * avg_price  # rough estimate

    if won:
        wins += 1
        pnl = shares * (1.0 - avg_price)
        win_pnl += pnl
    else:
        losses += 1
        pnl = -iv
        loss_pnl += pnl

    resolved_trades.append({
        "won": won, "shares": shares, "pnl": pnl,
        "title": mapping["title"][:40], "slug": mapping["slug"][:30],
    })

total = wins + losses
wr = wins / total * 100 if total > 0 else 0
print("Resolved: %d (W:%d L:%d) WR: %.1f%%" % (total, wins, losses, wr), flush=True)
print("Open: %d  Unmapped: %d" % (open_count, unmapped_count), flush=True)
print("Note: PnL estimates use avg_price=50c (no exact price from on-chain)", flush=True)
