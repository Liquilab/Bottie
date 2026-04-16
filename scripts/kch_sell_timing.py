#!/usr/bin/env python3
"""Find all kch123 sell transactions on-chain and correlate with game times."""
import json, urllib.request, datetime, time

KCH = "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee"
API_KEY = "S19F6VQFIYY994495MMXW8HKSRIAHNX6PG"

# PM contracts on Polygon
CTF_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
NEG_RISK_EXCHANGE = "0xc5d563a7060c6be80d4a9c0a3b66f0b0716af5f8"

def fetch(u):
    req = urllib.request.Request(u, headers={"User-Agent": "B/1"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def etherscan_v2(params):
    base = "https://api.etherscan.io/v2/api?chainid=137"
    qs = "&".join("%s=%s" % (k, v) for k, v in params.items())
    url = "%s&%s&apikey=%s" % (base, qs, API_KEY)
    time.sleep(0.25)  # rate limit
    return fetch(url)

# Step 1: Get ALL normal transactions (these contain CLOB sell calls)
print("=== FETCHING ALL TRANSACTIONS ===")
all_txs = []
page = 1
while page <= 50:
    data = etherscan_v2({
        "module": "account",
        "action": "txlist",
        "address": KCH,
        "sort": "desc",
        "page": str(page),
        "offset": "1000",
    })
    results = data.get("result", [])
    if not isinstance(results, list) or len(results) == 0:
        break
    all_txs.extend(results)
    print("  Page %d: %d txs (total: %d)" % (page, len(results), len(all_txs)))
    if len(results) < 1000:
        break
    page += 1

print("Total transactions: %d" % len(all_txs))

# Step 2: Filter for interactions with PM exchange contracts
# Sells go TO the CTF exchange or Neg Risk exchange
pm_txs = [tx for tx in all_txs if
    tx.get("to", "").lower() in (CTF_EXCHANGE, NEG_RISK_EXCHANGE) or
    tx.get("from", "").lower() in (CTF_EXCHANGE, NEG_RISK_EXCHANGE)]

print("PM exchange transactions: %d" % len(pm_txs))

# Step 3: Also get internal transactions (token transfers from exchange TO wallet = sell proceeds)
print("\n=== FETCHING INTERNAL TRANSACTIONS ===")
all_internal = []
page = 1
while page <= 20:
    data = etherscan_v2({
        "module": "account",
        "action": "txlistinternal",
        "address": KCH,
        "sort": "desc",
        "page": str(page),
        "offset": "1000",
    })
    results = data.get("result", [])
    if not isinstance(results, list) or len(results) == 0:
        break
    all_internal.extend(results)
    print("  Page %d: %d internal txs (total: %d)" % (page, len(results), len(all_internal)))
    if len(results) < 1000:
        break
    page += 1

# Step 4: Get ALL token transfers (both USDC and conditional tokens)
print("\n=== FETCHING ALL TOKEN TRANSFERS ===")
all_tokens = []
page = 1
while page <= 50:
    data = etherscan_v2({
        "module": "account",
        "action": "tokentx",
        "address": KCH,
        "sort": "desc",
        "page": str(page),
        "offset": "1000",
    })
    results = data.get("result", [])
    if not isinstance(results, list) or len(results) == 0:
        break
    all_tokens.extend(results)
    print("  Page %d: %d token txs (total: %d)" % (page, len(results), len(all_tokens)))
    if len(results) < 1000:
        break
    page += 1

print("\nTotal token transfers: %d" % len(all_tokens))

# Step 5: Find USDC INCOMING to wallet (= sell proceeds or redemptions)
usdc_in = [tx for tx in all_tokens
    if tx.get("to", "").lower() == KCH.lower()
    and "usdc" in tx.get("tokenSymbol", "").lower()]

print("\n=== USDC INCOMING (sells + redemptions): %d ===" % len(usdc_in))
for tx in sorted(usdc_in, key=lambda x: -int(x.get("timeStamp", 0)))[:30]:
    ts = int(tx.get("timeStamp", 0))
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    decimals = int(tx.get("tokenDecimal", 6))
    val = int(tx.get("value", 0)) / (10 ** decimals)
    fr = tx.get("from", "")[:14]
    txhash = tx.get("hash", "")[:16]
    print("  %s  +$%10.2f  from=%s  tx=%s" % (dt, val, fr, txhash))

# Step 6: Find large USDC incoming (likely sell proceeds, not small MM)
print("\n=== LARGE USDC IN (>$1000, likely sells): ===" )
large_usdc = [tx for tx in usdc_in if int(tx.get("value", 0)) / 1e6 > 1000]
for tx in sorted(large_usdc, key=lambda x: -int(x.get("timeStamp", 0))):
    ts = int(tx.get("timeStamp", 0))
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    val = int(tx.get("value", 0)) / 1e6
    fr = tx.get("from", "")[:14]
    txhash = tx.get("hash", "")[:16]
    print("  %s  +$%10.2f  from=%s  tx=%s" % (dt, val, fr, txhash))

print("\nDone. Total: %d normal txs, %d internal, %d token transfers" % (
    len(all_txs), len(all_internal), len(all_tokens)))
