import json

with open("/opt/bottie/data/trades.jsonl") as f:
    trades = [json.loads(l) for l in f if l.strip()]

LEADS = {
    "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b",  # Cannae
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",  # sovereign2013
}

open_trades = [t for t in trades if t.get("filled") and not t.get("result")]
lead_trades = [t for t in open_trades if t.get("copy_wallet") in LEADS]
non_lead = [t for t in open_trades if t.get("copy_wallet") not in LEADS]

total_cost = sum(t.get("size_usdc", 0) or 0 for t in non_lead)
print(f"Open trades totaal: {len(open_trades)}")
print(f"Van Cannae/sovereign: {len(lead_trades)} (HOUDEN)")
print(f"Te sluiten: {len(non_lead)} (${total_cost:.2f} vastgelegd)")
print()

print(f"--- TE SLUITEN ({len(non_lead)} trades) ---")
header = f"{'Titel':<55s} {'Outcome':<15s} {'ct':>4s} {'$':>7s} {'Wallet':<12s}"
print(header)
print("-" * 96)
for t in sorted(non_lead, key=lambda x: x.get("timestamp", "")):
    title = (t.get("market_title") or "?")[:54]
    outcome = (t.get("outcome") or "?")[:14]
    price = t.get("price", 0)
    cost = t.get("size_usdc", 0) or 0
    wallet = t.get("copy_wallet")
    if not wallet:
        wallet = "manual"
    else:
        wallet = wallet[:12]
    print(f"{title:<55s} {outcome:<15s} {price*100:>3.0f}ct ${cost:>6.2f} {wallet:<12s}")

print()
print(f"--- HOUDEN ({len(lead_trades)} trades) ---")
for t in sorted(lead_trades, key=lambda x: x.get("timestamp", "")):
    title = (t.get("market_title") or "?")[:54]
    outcome = (t.get("outcome") or "?")[:14]
    w = t.get("copy_wallet", "")
    name = "Cannae" if "7ea571" in w else "sovereign"
    cost = t.get("size_usdc", 0) or 0
    print(f"  {title:<55s} {outcome:<15s} ${cost:.2f} ({name})")
