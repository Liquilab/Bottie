"""Analyse: O/U en ML prijs rond kickoff.

Hypothese: O/U shares dalen fors (tot 50%) net na kickoff.
Check: vergelijk prijs bij T-30, T-5, T-0, T+5, T+10 voor O/U vs ML.

Data:
- Cannae trades: cannae_all_trades.csv (condition_id, title, timestamp)
- Game start times: markets.parquet (conditionId, gameStartTime)
- Price history: CLOB /prices-history API (1-min candles)
- Token mapping: token_condition_map.json
"""

import json
import time
import statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta

import duckdb
import httpx
import pandas as pd

DATA = Path(__file__).parent

# 1. Load Cannae trades
trades = pd.read_csv(DATA / "cannae_trades/cannae_all_trades.csv")

# Classify market type
def classify(title) -> str:
    if not isinstance(title, str):
        return "unknown"
    t = title.lower()
    if any(k in t for k in ["points o/u", "rebounds o/u", "assists o/u",
                             "points over/under", "rebounds over/under", "assists over/under"]):
        if ": " in t:
            return "player_prop"
    if "o/u" in t or "over/under" in t:
        return "ou"
    if "spread" in t:
        return "spread"
    if "draw" in t or "btts" in t or "both teams" in t:
        return "other"
    return "win"

trades["mt"] = trades["title"].apply(classify)

# 2. Get unique conditions for O/U and win
ou_conditions = set(trades[trades["mt"] == "ou"]["condition_id"].unique())
win_conditions = set(trades[trades["mt"] == "win"]["condition_id"].unique())
all_conditions = ou_conditions | win_conditions

print(f"O/U conditions: {len(ou_conditions)}")
print(f"Win conditions: {len(win_conditions)}")

# 3. Join with game start times from markets.parquet
con = duckdb.connect()
markets = con.execute("""
    SELECT conditionId, gameStartTime, question, slug
    FROM 'data_lake/markets.parquet'
    WHERE gameStartTime IS NOT NULL
""").fetchdf()

# Build condition → gameStartTime map
game_times = {}
for _, row in markets.iterrows():
    cid = row["conditionId"]
    if cid in all_conditions:
        try:
            gst = pd.Timestamp(row["gameStartTime"]).timestamp()
            game_times[cid] = gst
        except Exception:
            pass

print(f"Conditions with gameStartTime: {len(game_times)}")
print(f"  O/U with time: {len(ou_conditions & set(game_times.keys()))}")
print(f"  Win with time: {len(win_conditions & set(game_times.keys()))}")

# 4. Load token_condition_map (tokenId → conditionId)
with open(DATA / "data_lake/token_condition_map.json") as f:
    token_to_cond = json.load(f)

# Invert: conditionId → list of tokenIds
cond_to_tokens = {}
for token_id, cond_id in token_to_cond.items():
    cond_to_tokens.setdefault(cond_id, []).append(token_id)

# 5. Check which conditions have local price data
prices_dir = DATA / "data_lake/prices"
conditions_with_prices = set()
token_to_file = {}
for cid in all_conditions & set(game_times.keys()):
    tokens = cond_to_tokens.get(cid, [])
    for tid in tokens:
        pfile = prices_dir / f"{tid}.parquet"
        if pfile.exists():
            conditions_with_prices.add(cid)
            token_to_file.setdefault(cid, []).append((tid, pfile))

print(f"Conditions with local price data: {len(conditions_with_prices)}")

# 6. For conditions without local data, fetch from API (limit to avoid rate limits)
CLOB = "https://clob.polymarket.com"
client = httpx.Client(timeout=15)

conditions_to_analyze = (all_conditions & set(game_times.keys()))
need_api = conditions_to_analyze - conditions_with_prices

print(f"Need API fetch for: {len(need_api)} conditions")

# Fetch price history from API for missing conditions (1-min fidelity around kickoff)
api_prices = {}
fetched = 0
MAX_FETCH = 100  # rate limit safety

for cid in need_api:
    if fetched >= MAX_FETCH:
        break
    tokens = cond_to_tokens.get(cid, [])
    if not tokens:
        continue

    # Use first token
    tid = tokens[0]
    try:
        resp = client.get(f"{CLOB}/prices-history", params={
            "market": tid, "interval": "max", "fidelity": 1
        })
        if resp.status_code == 200:
            data = resp.json()
            if data and "history" in data:
                api_prices[cid] = [(p["t"], p["p"]) for p in data["history"]]
                fetched += 1
        time.sleep(0.2)  # rate limit
    except Exception as e:
        pass

print(f"Fetched from API: {fetched}")

# 7. Extract prices at key moments relative to kickoff
def get_price_at_offset(price_series, kickoff_ts, offset_minutes):
    """Get price closest to kickoff + offset_minutes."""
    target = kickoff_ts + (offset_minutes * 60)
    best = None
    best_dist = float("inf")
    for ts, price in price_series:
        dist = abs(ts - target)
        if dist < best_dist:
            best_dist = dist
            best = price
    # Only return if within 5 minutes of target
    if best_dist <= 300:
        return best
    return None

def load_local_prices(cid):
    """Load price series from local parquet."""
    files = token_to_file.get(cid, [])
    if not files:
        return None
    tid, pfile = files[0]
    try:
        df = pd.read_parquet(pfile)
        # Find timestamp and price columns
        ts_col = [c for c in df.columns if "t" in c.lower() or "time" in c.lower()]
        p_col = [c for c in df.columns if "p" in c.lower() or "price" in c.lower()]
        if ts_col and p_col:
            return list(zip(df[ts_col[0]].values, df[p_col[0]].values))
    except Exception:
        pass
    return None

# 8. Analyze price movement around kickoff
offsets = [-30, -10, -5, 0, 5, 10, 15, 20]
results = {"ou": [], "win": []}

for cid in conditions_to_analyze:
    if cid not in game_times:
        continue

    kickoff = game_times[cid]
    mt = "ou" if cid in ou_conditions else "win"

    # Get price series
    if cid in api_prices:
        series = api_prices[cid]
    else:
        series = load_local_prices(cid)

    if not series:
        continue

    # Get Cannae's entry price for this condition
    cond_trades = trades[trades["condition_id"] == cid]
    entry_price = cond_trades["price"].mean()

    # Get prices at each offset
    prices_at = {}
    for off in offsets:
        p = get_price_at_offset(series, kickoff, off)
        if p is not None:
            prices_at[off] = float(p)

    if 0 not in prices_at or -5 not in prices_at:
        continue  # need at least kickoff and T-5

    results[mt].append({
        "condition_id": cid,
        "title": cond_trades["title"].iloc[0],
        "entry_price": round(entry_price, 4),
        "kickoff": kickoff,
        "prices": prices_at,
    })

print(f"\nAnalyseerbaar: O/U={len(results['ou'])}, Win={len(results['win'])}")

# 9. Calculate statistics
print("\n" + "="*70)
print("PRIJS ROND KICKOFF — O/U vs WIN (ML)")
print("="*70)

for mt_label, mt_key in [("O/U (TOTALS)", "ou"), ("WIN (MONEYLINE)", "win")]:
    data = results[mt_key]
    if not data:
        print(f"\n{mt_label}: geen data")
        continue

    print(f"\n{mt_label} — {len(data)} markten")
    print("-" * 60)

    for off in offsets:
        changes = []
        for d in data:
            if off in d["prices"] and -5 in d["prices"]:
                # Change relative to T-5 price
                p_ref = d["prices"][-5]
                p_now = d["prices"][off]
                if p_ref > 0:
                    pct_change = ((p_now - p_ref) / p_ref) * 100
                    changes.append(pct_change)

        if changes:
            avg = statistics.mean(changes)
            med = statistics.median(changes)
            neg = sum(1 for c in changes if c < -5)  # >5% drop
            big_neg = sum(1 for c in changes if c < -20)  # >20% drop
            print(f"  T{off:+3d}: avg={avg:+6.1f}%  med={med:+6.1f}%  "
                  f"n={len(changes):3d}  drops>5%={neg}  drops>20%={big_neg}")

# 10. Show individual examples of big drops (O/U)
print("\n" + "="*70)
print("VOORBEELDEN: GROOTSTE O/U DALINGEN NA KICKOFF")
print("="*70)

if results["ou"]:
    drops = []
    for d in results["ou"]:
        if 5 in d["prices"] and -5 in d["prices"]:
            p_before = d["prices"][-5]
            p_after = d["prices"][5]
            if p_before > 0:
                drop_pct = ((p_after - p_before) / p_before) * 100
                drops.append((drop_pct, d))

    drops.sort(key=lambda x: x[0])
    for drop_pct, d in drops[:15]:
        p_str = "  ".join(f"T{k:+d}={v:.2f}" for k, v in sorted(d["prices"].items()))
        print(f"  {drop_pct:+6.1f}% | {d['title'][:50]}")
        print(f"         {p_str}")
        print()

# Same for win
print("\n" + "="*70)
print("VOORBEELDEN: GROOTSTE WIN/ML DALINGEN NA KICKOFF")
print("="*70)

if results["win"]:
    drops = []
    for d in results["win"]:
        if 5 in d["prices"] and -5 in d["prices"]:
            p_before = d["prices"][-5]
            p_after = d["prices"][5]
            if p_before > 0:
                drop_pct = ((p_after - p_before) / p_before) * 100
                drops.append((drop_pct, d))

    drops.sort(key=lambda x: x[0])
    for drop_pct, d in drops[:15]:
        p_str = "  ".join(f"T{k:+d}={v:.2f}" for k, v in sorted(d["prices"].items()))
        print(f"  {drop_pct:+6.1f}% | {d['title'][:50]}")
        print(f"         {p_str}")
        print()
