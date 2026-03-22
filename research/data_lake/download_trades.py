#!/usr/bin/env python3
"""
Download trade history per wallet via Polygon on-chain data (ANKR RPC).

Uses eth_getLogs for TransferSingle events on the Polymarket CTF contract.
No offset limits — gets full trade history.
Enriches with data-api for price/title/eventSlug where available.

Output: data_lake/trades/{address}.parquet

Usage:
  python3 download_trades.py                  # Cannae + scout candidates
  python3 download_trades.py --days 30        # last 30 days only
  python3 download_trades.py --wallet 0xABC   # single wallet
  python3 download_trades.py --skip-enrich    # on-chain only, no data-api
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

# --- Config ---
ANKR_RPC = "https://rpc.ankr.com/polygon/d7e57b7d62eaba6b7c434153660caddfc0a9445537e9073bcc3823b4f8080bc8"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
DATA_API = "https://data-api.polymarket.com"

ZERO_ADDRESS = "0x" + "0" * 64
BLOCKS_PER_DAY = 43200  # Polygon ~2s blocks
DEFAULT_DAYS_BACK = 90
BLOCK_CHUNK = 100  # ANKR Polygon limit is ~100-150 blocks for getLogs
SHARES_DECIMALS = 6  # Verified: raw_value / 10^6 = shares

BASE_DIR = Path(__file__).parent
TRADES_DIR = BASE_DIR / "trades"
PROJECT_ROOT = BASE_DIR.parent.parent
TOKEN_MAP_FILE = BASE_DIR / "token_condition_map.json"
BLOCK_TS_CACHE = BASE_DIR / "block_timestamps.json"

CANNAE_DEFAULT = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"


# --- RPC helpers ---

def rpc_call(method: str, params: list, retries: int = 3):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(ANKR_RPC, data=payload, headers={"Content-Type": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            if "error" in result:
                err = result["error"]
                msg = str(err.get("message", ""))
                if "limit" in msg.lower() or "range" in msg.lower() or "exceed" in msg.lower() or "too large" in msg.lower():
                    raise ValueError(f"Range too large: {msg}")
                raise RuntimeError(f"RPC error: {err}")
            return result.get("result")
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(1 * (attempt + 1))
    return None


def rpc_batch(calls: list[tuple[str, list]], batch_size: int = 50) -> list:
    all_results = []
    for i in range(0, len(calls), batch_size):
        batch = calls[i:i + batch_size]
        payload = json.dumps([
            {"jsonrpc": "2.0", "method": m, "params": p, "id": j}
            for j, (m, p) in enumerate(batch)
        ]).encode()
        req = urllib.request.Request(ANKR_RPC, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                results = json.loads(resp.read())
            results.sort(key=lambda x: x.get("id", 0))
            all_results.extend([r.get("result") for r in results])
        except Exception as e:
            print(f"  Batch RPC error: {e}", flush=True)
            all_results.extend([None] * len(batch))
        time.sleep(0.1)
    return all_results


def pad_address(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").zfill(64)


def get_current_block() -> int:
    result = rpc_call("eth_blockNumber", [])
    return int(result, 16)


# --- On-chain event fetching ---

def get_logs_safe(from_block: int, to_block: int, topics: list, depth: int = 0) -> list:
    """Fetch logs, auto-split on range/size errors. Recursive binary split."""
    try:
        params = [{
            "address": CTF_CONTRACT,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": topics,
        }]
        result = rpc_call("eth_getLogs", params)
        return result if isinstance(result, list) else []
    except (ValueError, RuntimeError):
        # Range too large or too many results — split in half
        if to_block - from_block < 10 or depth > 15:
            return []
        mid = (from_block + to_block) // 2
        left = get_logs_safe(from_block, mid, topics, depth + 1)
        right = get_logs_safe(mid + 1, to_block, topics, depth + 1)
        return left + right


def fetch_transfer_events(address: str, from_block: int, to_block: int) -> list[dict]:
    """Fetch all TransferSingle events where address is sender or receiver."""
    padded = pad_address(address)
    all_events = []
    total_blocks = to_block - from_block

    for direction, topic_filter in [
        ("BUY",  [TRANSFER_SINGLE_TOPIC, None, None, padded]),  # to=address
        ("SELL", [TRANSFER_SINGLE_TOPIC, None, padded, None]),  # from=address
    ]:
        block = from_block
        direction_events = 0
        while block <= to_block:
            chunk_end = min(block + BLOCK_CHUNK - 1, to_block)
            logs = get_logs_safe(block, chunk_end, topic_filter)

            for log in logs:
                data = log.get("data", "0x")
                if len(data) < 130:
                    continue
                topics = log.get("topics", [])

                # Skip mint/burn (zero address)
                from_topic = topics[2] if len(topics) > 2 else None
                to_topic = topics[3] if len(topics) > 3 else None
                if from_topic == ZERO_ADDRESS or to_topic == ZERO_ADDRESS:
                    continue

                token_id = int(data[2:66], 16)
                raw_value = int(data[66:130], 16)
                shares = raw_value / (10 ** SHARES_DECIMALS)

                all_events.append({
                    "token_id": str(token_id),
                    "size": shares,
                    "side": direction,
                    "block_number": int(log["blockNumber"], 16),
                    "tx_hash": log["transactionHash"],
                    "log_index": int(log.get("logIndex", "0x0"), 16),
                })
                direction_events += 1

            pct = (chunk_end - from_block) / max(total_blocks, 1) * 100
            # Print every ~5%
            prev_pct = (block - 1 - from_block) / max(total_blocks, 1) * 100
            if int(pct / 5) > int(prev_pct / 5):
                print(f"    [{direction}] {pct:.0f}% ({direction_events} events)", flush=True)

            block = chunk_end + 1

        print(f"    [{direction}] done: {direction_events} events", flush=True)

    return all_events


# --- Block timestamps ---

def resolve_block_timestamps(events: list[dict]) -> dict[int, int]:
    cache: dict[int, int] = {}
    if BLOCK_TS_CACHE.exists():
        try:
            cache = {int(k): v for k, v in json.loads(BLOCK_TS_CACHE.read_text()).items()}
        except Exception:
            pass

    blocks = set(e["block_number"] for e in events)
    uncached = sorted(b for b in blocks if b not in cache)

    if uncached:
        print(f"  Fetching timestamps for {len(uncached)} blocks...", flush=True)
        calls = [("eth_getBlockByNumber", [hex(b), False]) for b in uncached]
        results = rpc_batch(calls)
        for block_num, result in zip(uncached, results):
            if result and "timestamp" in result:
                cache[block_num] = int(result["timestamp"], 16)
        BLOCK_TS_CACHE.write_text(json.dumps({str(k): v for k, v in cache.items()}))
        print(f"    Cached {len(cache)} block timestamps", flush=True)

    return cache


# --- Token → conditionId mapping ---

def build_token_map(cannae_addr: str) -> dict[str, str]:
    token_map: dict[str, str] = {}

    if TOKEN_MAP_FILE.exists():
        try:
            token_map = json.loads(TOKEN_MAP_FILE.read_text())
        except Exception:
            pass

    # From existing data-api parquet (if present from previous runs)
    parquet = TRADES_DIR / f"{cannae_addr}.parquet"
    if parquet.exists():
        try:
            df = pd.read_parquet(parquet)
            if "asset" in df.columns and "conditionId" in df.columns:
                for _, row in df.iterrows():
                    asset = str(row.get("asset", ""))
                    cid = str(row.get("conditionId", ""))
                    if asset and cid:
                        token_map[asset] = cid
        except Exception:
            pass

    # From our trades.jsonl
    trades_file = PROJECT_ROOT / "data" / "trades.jsonl"
    if trades_file.exists():
        try:
            for line in open(trades_file):
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                tid = str(d.get("token_id", ""))
                cid = str(d.get("condition_id", ""))
                if tid and cid:
                    token_map[tid] = cid
        except Exception:
            pass

    TOKEN_MAP_FILE.write_text(json.dumps(token_map))
    print(f"  Token map: {len(token_map)} entries", flush=True)
    return token_map


def resolve_unmapped_tokens(events: list[dict], token_map: dict[str, str]) -> dict[str, str]:
    """Query gamma API for unmapped token_ids."""
    unmapped = set()
    for e in events:
        if e["token_id"] not in token_map:
            unmapped.add(e["token_id"])

    if not unmapped:
        return token_map

    print(f"  Resolving {len(unmapped)} unmapped tokens via gamma API...", flush=True)
    resolved = 0
    for tid in unmapped:
        url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={tid}&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "PM-DataLake/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                cid = data[0].get("conditionId", "")
                if cid:
                    token_map[tid] = cid
                    resolved += 1
        except Exception:
            pass
        time.sleep(0.2)

    TOKEN_MAP_FILE.write_text(json.dumps(token_map))
    print(f"    Resolved {resolved}/{len(unmapped)}", flush=True)
    return token_map


# --- Data-api enrichment ---

def fetch_data_api_trades(address: str, max_trades: int = 3100) -> list[dict]:
    all_trades: list[dict] = []
    offset = 0
    page_size = 50
    while len(all_trades) < max_trades:
        url = f"{DATA_API}/trades?user={address}&limit={page_size}&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "PM-DataLake/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception:
            break
        if not data or not isinstance(data, list):
            break
        all_trades.extend(data)
        if len(data) < page_size:
            break
        offset += page_size
        time.sleep(0.15)
    return all_trades


def enrich_with_data_api(events: list[dict], address: str, token_map: dict[str, str]):
    """Add price/title/eventSlug from data-api. Also update token_map."""
    print("  Fetching data-api trades for enrichment...", flush=True)
    api_trades = fetch_data_api_trades(address)
    print(f"    {len(api_trades)} trades from data-api", flush=True)

    # Update token_map from data-api
    for t in api_trades:
        asset = str(t.get("asset", ""))
        cid = t.get("conditionId", "")
        if asset and cid:
            token_map[asset] = cid

    # Index by tx_hash
    api_by_tx: dict[str, list[dict]] = {}
    for t in api_trades:
        tx = t.get("transactionHash", "")
        if tx:
            api_by_tx.setdefault(tx, []).append(t)

    enriched = 0
    for event in events:
        tx = event.get("tx_hash", "")
        if tx in api_by_tx:
            for api_t in api_by_tx[tx]:
                if str(api_t.get("asset", "")) == event.get("token_id"):
                    event["price"] = api_t.get("price")
                    event["title"] = api_t.get("title", "")
                    event["eventSlug"] = api_t.get("eventSlug", "")
                    event["outcome"] = api_t.get("outcome", "")
                    enriched += 1
                    break
    print(f"    Enriched {enriched}/{len(events)} events with price/title", flush=True)

    TOKEN_MAP_FILE.write_text(json.dumps(token_map))


# --- Wallet helpers ---

def get_cannae_address() -> str:
    config_path = PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            for w in cfg.get("wallets", []):
                if "cannae" in (w.get("name") or "").lower():
                    return w.get("address", CANNAE_DEFAULT).lower()
        except Exception:
            pass
    return CANNAE_DEFAULT


def get_scout_candidates(top_n: int = 5) -> list[str]:
    scout_file = PROJECT_ROOT / "data" / "scout_evaluated.json"
    if not scout_file.exists():
        return []
    try:
        data = json.loads(scout_file.read_text())
        if isinstance(data, list):
            return [
                (e.get("address") or e.get("wallet") or e.get("proxyWallet", "")).lower()
                for e in data[:top_n]
                if e.get("address") or e.get("wallet") or e.get("proxyWallet")
            ]
    except Exception:
        pass
    return []


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Download wallet trades via on-chain data (ANKR RPC)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK, help="Days of history (default 90)")
    parser.add_argument("--wallet", type=str, default=None, help="Single wallet only")
    parser.add_argument("--skip-scouts", action="store_true", help="Skip scout candidates")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip data-api enrichment")
    args = parser.parse_args()

    TRADES_DIR.mkdir(parents=True, exist_ok=True)

    # Build wallet list
    wallets: list[tuple[str, str]] = []
    if args.wallet:
        wallets.append(("custom", args.wallet.lower()))
    else:
        cannae = get_cannae_address()
        wallets.append(("cannae", cannae))
        if not args.skip_scouts:
            for addr in get_scout_candidates():
                if addr not in [w[1] for w in wallets]:
                    wallets.append(("scout", addr))

    current_block = get_current_block()
    print(f"Current block: {current_block}", flush=True)
    print(f"Downloading trades for {len(wallets)} wallet(s)...", flush=True)

    for label, address in wallets:
        print(f"\n{'='*60}", flush=True)
        print(f"[{label}] {address[:10]}...", flush=True)

        # Determine start block
        parquet_path = TRADES_DIR / f"{address}.parquet"
        df_existing = None
        if parquet_path.exists():
            try:
                df_existing = pd.read_parquet(parquet_path)
                if "block_number" in df_existing.columns and len(df_existing) > 0:
                    max_block = int(df_existing["block_number"].max())
                    start_block = max_block + 1
                    from datetime import datetime, timezone
                    max_ts = int(df_existing["timestamp"].max()) if "timestamp" in df_existing.columns else 0
                    if max_ts:
                        ts_str = datetime.fromtimestamp(max_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                        print(f"  Incremental from block {start_block} ({ts_str})", flush=True)
                    else:
                        print(f"  Incremental from block {start_block}", flush=True)
                else:
                    df_existing = None
                    start_block = current_block - (args.days * BLOCKS_PER_DAY)
            except Exception:
                df_existing = None
                start_block = current_block - (args.days * BLOCKS_PER_DAY)
        else:
            start_block = current_block - (args.days * BLOCKS_PER_DAY)

        if start_block > current_block:
            print("  Already up to date.", flush=True)
            continue

        days_span = (current_block - start_block) / BLOCKS_PER_DAY
        print(f"  Scanning {days_span:.0f} days ({current_block - start_block:,} blocks)...", flush=True)

        # Phase 1: On-chain events
        events = fetch_transfer_events(address, start_block, current_block)
        print(f"  Total: {len(events)} on-chain events", flush=True)

        if not events:
            print("  No new events.", flush=True)
            continue

        # Phase 2: Block timestamps
        ts_cache = resolve_block_timestamps(events)
        for e in events:
            e["timestamp"] = ts_cache.get(e["block_number"], 0)

        # Phase 3: Token → conditionId
        token_map = build_token_map(address)
        if not args.skip_enrich:
            enrich_with_data_api(events, address, token_map)

        # Resolve remaining unmapped tokens via gamma API
        token_map = resolve_unmapped_tokens(events, token_map)

        # Apply conditionId mapping
        mapped = 0
        for e in events:
            cid = token_map.get(e["token_id"], "")
            e["conditionId"] = cid
            if cid:
                mapped += 1
        print(f"  Mapped {mapped}/{len(events)} to conditionId", flush=True)

        # Phase 4: Save
        df_new = pd.DataFrame(events)

        # Ensure consistent columns
        for col in ["price", "title", "eventSlug", "outcome"]:
            if col not in df_new.columns:
                df_new[col] = None if col == "price" else ""

        if df_existing is not None and len(df_existing) > 0:
            # Align columns before concat
            for col in df_new.columns:
                if col not in df_existing.columns:
                    df_existing[col] = None if col == "price" else ""
            df_combined = pd.concat([df_existing, df_new], ignore_index=True)
            df_combined = df_combined.drop_duplicates(subset=["tx_hash", "token_id", "log_index"], keep="last")
        else:
            df_combined = df_new

        df_combined = df_combined.sort_values("timestamp", ascending=False).reset_index(drop=True)
        df_combined.to_parquet(parquet_path, index=False)

        from datetime import datetime, timezone
        min_ts = int(df_combined["timestamp"].min())
        max_ts = int(df_combined["timestamp"].max())
        min_dt = datetime.fromtimestamp(min_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        max_dt = datetime.fromtimestamp(max_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  Saved {len(df_combined)} trades ({min_dt} to {max_dt})", flush=True)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
