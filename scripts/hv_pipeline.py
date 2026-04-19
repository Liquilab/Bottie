#!/usr/bin/env python3
"""HV wallet analysis pipeline — clean rewrite.

HV = 0x7da07b2a8b009a406198677debda46ad651b6be2 (BTC/ETH/XRP/SOL 5M scalper).

Phases (each resumable, skip if output already exists unless --force):
  1. pull     — paginate data-api activity, filter crypto 5M, save raw JSONL
  2. windows  — aggregate per window, persist schema-contracted JSON
  3. resolve  — fetch CLOB /markets/<cid> concurrently → winner, token IDs
  4. prices   — fetch CLOB prices-history per token concurrently → min price
  5. simulate — counterfactual: fixed-tier limit BUYs on both sides

Usage:
  python3 scripts/hv_pipeline.py --days 7 --tiers 0.01 --stakes btc=15,default=2
  python3 scripts/hv_pipeline.py --phase simulate --tiers 0.01,0.02,0.03

Safety:
  - Single-instance PID lock (data/.hv_pipeline.lock)
  - Atomic writes (tmp + rename)
  - Checkpoint every N ops in long phases
  - Schema validation between phases (fail-fast)
  - Thread pool for CLOB calls (bounded concurrency)
"""
from __future__ import annotations
import argparse, json, os, sys, time, signal, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

HV = "0x7da07b2a8b009a406198677debda46ad651b6be2"
API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DATA = Path("data")
LOCK = DATA / ".hv_pipeline.lock"
PATHS = {
    "raw":        DATA / "hv_activity_raw.json",
    "windows":    DATA / "hv_windows.json",
    "markets":    DATA / "clob_markets_cache.json",
    "prices":     DATA / "clob_prices_cache.json",
    "simulation": DATA / "hv_simulation.json",
}

WINDOW_SCHEMA_REQ = ["slug", "coin", "conditionId", "window_start", "title",
                     "buy_usdc", "sell_usdc", "redeem_usdc", "held_up", "held_down",
                     "n_trades", "n_redeems"]

# ------------------- utilities -------------------

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def http_get(url, timeout=15, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404): raise
            last = e
        except Exception as e:
            last = e
        time.sleep(1 + i * 2)
    raise last

def atomic_write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)

def acquire_lock():
    DATA.mkdir(exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)  # signal 0 = check alive
            log(f"ERROR: lock held by PID {pid}. Abort.")
            sys.exit(2)
        except (ProcessLookupError, ValueError):
            log("Stale lock, removing.")
            LOCK.unlink()
    LOCK.write_text(str(os.getpid()))

def release_lock():
    try: LOCK.unlink()
    except FileNotFoundError: pass

# ------------------- phase 1: pull -------------------

def phase_pull(days: int, force: bool):
    out = PATHS["raw"]
    if out.exists() and not force:
        raw = json.load(open(out))
        log(f"SKIP pull: {out} exists ({len(raw)} trades). Use --force to re-pull.")
        return raw
    cutoff = int(time.time()) - days * 86400
    log(f"Pulling HV activity since {datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()}")
    all_items, seen = [], set()
    end_ts = int(time.time())
    errs = 0
    while True:
        url = f"{API}/activity?user={HV}&limit=500&end={end_ts}"
        try:
            batch = http_get(url, timeout=20)
            errs = 0
        except Exception as e:
            errs += 1
            log(f"  pagination error (try {errs}): {e}")
            if errs >= 5:
                log("  too many errors → abort pull")
                sys.exit(3)
            time.sleep(2)
            continue
        if not batch: break
        new = []
        for a in batch:
            k = (a.get("transactionHash",""), a.get("asset",""), a.get("side",""), a.get("timestamp",0))
            if k in seen: continue
            seen.add(k); new.append(a)
        all_items.extend(new)
        oldest = min(a.get("timestamp", end_ts) for a in batch)
        log(f"  end={end_ts} got={len(batch)} new={len(new)} oldest={datetime.fromtimestamp(oldest,tz=timezone.utc).strftime('%m-%d %H:%M')} total={len(all_items)}")
        if oldest < cutoff: break
        if not new: break
        nxt = oldest - 1
        if nxt >= end_ts: break
        end_ts = nxt
        time.sleep(0.2)
    crypto = [a for a in all_items if a.get("timestamp",0) >= cutoff and "-updown-5m-" in a.get("slug","")]
    counts = Counter(a.get("slug","").split("-")[0] for a in crypto)
    log(f"Crypto 5M subset: {len(crypto)}  coins={dict(counts)}")
    atomic_write_json(out, crypto)
    log(f"Saved: {out}")
    return crypto

# ------------------- phase 2: windows -------------------

def phase_windows(raw: list, force: bool):
    out = PATHS["windows"]
    if out.exists() and not force:
        windows = json.load(open(out))
        log(f"SKIP windows: {out} exists ({len(windows)}). Use --force to rebuild.")
        return windows
    log(f"Aggregating {len(raw)} activity events into windows...")
    agg = defaultdict(lambda: {"slug":"", "coin":"", "conditionId":"", "title":"",
                               "window_start":0, "n_trades":0, "n_redeems":0,
                               "buy_usdc":0.0, "sell_usdc":0.0, "redeem_usdc":0.0,
                               "buy_shares_up":0.0, "buy_shares_down":0.0,
                               "sell_shares_up":0.0, "sell_shares_down":0.0})
    n_trade = n_redeem = n_other = 0
    for a in raw:
        slug = a.get("slug",""); w = agg[slug]
        w["slug"] = slug
        w["coin"] = slug.split("-")[0] if slug else ""
        w["conditionId"] = a.get("conditionId","") or w["conditionId"]
        w["title"] = a.get("title","") or w["title"]
        try: w["window_start"] = int(slug.rsplit("-",1)[-1])
        except: pass
        atype = a.get("type", "")
        size = float(a.get("size",0)); usdc = float(a.get("usdcSize",0))
        outc = a.get("outcome",""); side = a.get("side","")
        if atype == "TRADE":
            w["n_trades"] += 1
            n_trade += 1
            if side == "BUY":
                w["buy_usdc"] += usdc
                if outc == "Up": w["buy_shares_up"] += size
                elif outc == "Down": w["buy_shares_down"] += size
            elif side == "SELL":
                w["sell_usdc"] += usdc
                if outc == "Up": w["sell_shares_up"] += size
                elif outc == "Down": w["sell_shares_down"] += size
        elif atype == "REDEEM":
            w["n_redeems"] += 1
            w["redeem_usdc"] += usdc
            n_redeem += 1
        else:
            n_other += 1
    log(f"  Activity mix: TRADE={n_trade}  REDEEM={n_redeem}  other={n_other}")
    windows = []
    for slug, w in agg.items():
        w["held_up"]     = round(w.pop("buy_shares_up")   - w.pop("sell_shares_up"),   4)
        w["held_down"]   = round(w.pop("buy_shares_down") - w.pop("sell_shares_down"), 4)
        w["buy_usdc"]    = round(w["buy_usdc"],    2)
        w["sell_usdc"]   = round(w["sell_usdc"],   2)
        w["redeem_usdc"] = round(w["redeem_usdc"], 2)
        missing = [k for k in WINDOW_SCHEMA_REQ if k not in w]
        if missing:
            log(f"  SCHEMA FAIL on {slug}: missing {missing}"); sys.exit(4)
        windows.append(w)
    windows.sort(key=lambda x: x["window_start"])
    atomic_write_json(out, windows)
    coins = Counter(w["coin"] for w in windows)
    log(f"Saved {len(windows)} windows. Coins: {dict(coins)}")
    return windows

# ------------------- phase 3: resolve -------------------

def phase_resolve(windows: list, workers: int, force: bool):
    cache_path = PATHS["markets"]
    cache = json.load(open(cache_path)) if cache_path.exists() else {}
    # DO NOT wipe cache on --force. Cache is keyed by stable cid/token_id,
    # never conflicts with new days. Preserve across runs.
    cids = list({w["conditionId"] for w in windows if w.get("conditionId")})
    missing = [c for c in cids if c not in cache]
    log(f"Resolve /markets: total={len(cids)} cached={len(cache)} missing={len(missing)} workers={workers}")
    if not missing:
        return cache
    done = 0; errors = 0; lock_flush = [0]
    def fetch(cid):
        try:
            return cid, http_get(f"{CLOB}/markets/{cid}", timeout=10)
        except Exception as e:
            return cid, {"_error": str(e)}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, c) for c in missing]
        for fut in as_completed(futs):
            cid, m = fut.result()
            cache[cid] = m
            done += 1
            if "_error" in (m or {}): errors += 1
            if done % 200 == 0:
                atomic_write_json(cache_path, cache)
                log(f"  /markets {done}/{len(missing)}  errors={errors}")
    atomic_write_json(cache_path, cache)
    log(f"Saved: {cache_path}  errors={errors}")
    return cache

def extract_tokens_and_winner(mkt):
    up_tid = down_tid = winner = None
    for t in (mkt or {}).get("tokens", []):
        o = t.get("outcome"); tid = t.get("token_id")
        if o == "Up":   up_tid = tid
        if o == "Down": down_tid = tid
        if t.get("winner") is True: winner = o
    return up_tid, down_tid, winner

# ------------------- phase 4: prices -------------------

def phase_prices(windows: list, markets: dict, workers: int, force: bool):
    """Fetch prices-history scoped to the exact 5m window (startTs..startTs+300).

    CLOB's interval=1h returns empty for resolved 5m markets. Per-timestamp
    bounds give 5-6 ticks per window with real min/max — what we need for
    fill simulation.
    """
    cache_path = PATHS["prices"]
    cache = json.load(open(cache_path)) if cache_path.exists() else {}
    # DO NOT wipe cache on --force. Cache is keyed by stable cid/token_id,
    # never conflicts with new days. Preserve across runs.
    # Build job list: (token_id, window_start) — we scope each call to its window
    jobs = []
    token_to_window = {}
    for w in windows:
        m = markets.get(w.get("conditionId"))
        if not m or "_error" in m: continue
        up, down, _ = extract_tokens_and_winner(m)
        ws = w.get("window_start", 0)
        for tid in (up, down):
            if not tid: continue
            token_to_window[tid] = ws
            if tid not in cache:
                jobs.append((tid, ws))
    log(f"prices-history: cached={len(cache)} missing={len(jobs)} workers={workers}")
    if not jobs:
        return cache
    def fetch(tid, ws):
        try:
            url = f"{CLOB}/prices-history?market={tid}&startTs={ws}&endTs={ws+300}"
            r = http_get(url, timeout=10)
            hist = r.get("history", []) if isinstance(r, dict) else r
            prices = [p.get("p") for p in (hist or []) if isinstance(p, dict) and p.get("p") is not None]
            return tid, {"min": min(prices) if prices else None,
                         "max": max(prices) if prices else None,
                         "n":   len(prices),
                         "window_start": ws}
        except Exception as e:
            return tid, {"_error": str(e), "window_start": ws}
    done = 0; errors = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, t, ws) for t, ws in jobs]
        for fut in as_completed(futs):
            tid, res = fut.result()
            cache[tid] = res
            done += 1
            if "_error" in res: errors += 1
            if done % 500 == 0:
                atomic_write_json(cache_path, cache)
                log(f"  prices {done}/{len(jobs)}  errors={errors}")
    atomic_write_json(cache_path, cache)
    log(f"Saved: {cache_path}  errors={errors}")
    return cache

# ------------------- phase 5: simulate -------------------

def phase_simulate(windows, markets, prices, tiers, stakes_by_coin, default_stake):
    per_coin_tier = defaultdict(lambda: {
        "windows":0, "both_filled":0, "winner_filled":0, "loser_filled":0, "no_fill":0,
        "cost":0.0, "payout":0.0, "net_pnl":0.0, "stake":0.0, "price":0.0})
    total_rows = 0
    for w in windows:
        cid = w.get("conditionId"); coin = w.get("coin","?")
        m = markets.get(cid)
        if not m or "_error" in m: continue
        up, down, winner = extract_tokens_and_winner(m)
        if not up or not down or not winner: continue
        pu = prices.get(up, {}); pd = prices.get(down, {})
        mu = pu.get("min"); md = pd.get("min")
        if mu is None or md is None: continue
        stake = stakes_by_coin.get(coin, default_stake)
        for p in tiers:
            up_fill = mu <= p; down_fill = md <= p
            cost = (stake if up_fill else 0) + (stake if down_fill else 0)
            payout = (stake/p if (up_fill and winner=="Up") else 0) + \
                     (stake/p if (down_fill and winner=="Down") else 0)
            key = (coin, p)
            s = per_coin_tier[key]
            s["windows"] += 1; s["stake"] = stake; s["price"] = p
            s["cost"] += cost; s["payout"] += payout; s["net_pnl"] += (payout - cost)
            if up_fill and down_fill: s["both_filled"] += 1
            if (up_fill and winner=="Up") or (down_fill and winner=="Down"): s["winner_filled"] += 1
            if (up_fill and winner=="Down") or (down_fill and winner=="Up"): s["loser_filled"] += 1
            if not up_fill and not down_fill: s["no_fill"] += 1
        total_rows += 1
    out = {
        "stakes_by_coin": stakes_by_coin,
        "default_stake": default_stake,
        "tiers": tiers,
        "n_windows_simulated": total_rows,
        "per_coin_tier": {f"{c}@{int(p*100)}c": v for (c,p), v in per_coin_tier.items()},
    }
    atomic_write_json(PATHS["simulation"], out)
    # print summary
    print()
    print(f"=== Simulation ({total_rows} windows) ===")
    hdr = f"{'coin':<6} {'tier':>5} {'stake':>7} {'windows':>8} {'both':>6} {'winner':>7} {'noFill':>7} {'cost':>10} {'payout':>10} {'net_pnl':>11}"
    print(hdr)
    for (coin, p), s in sorted(per_coin_tier.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        print(f"{coin:<6} {int(p*100):>4}c ${s['stake']:>5.0f} {s['windows']:>8} {s['both_filled']:>6} {s['winner_filled']:>7} {s['no_fill']:>7} {s['cost']:>10.2f} {s['payout']:>10.2f} {s['net_pnl']:>+11.2f}")
    return out

# ------------------- CLI -------------------

def parse_stakes(s):
    out = {}
    default = 2.0
    for part in s.split(","):
        k, v = part.split("=")
        k = k.strip().lower(); v = float(v)
        if k == "default": default = v
        else: out[k] = v
    return out, default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tiers", default="0.01", help="comma list, e.g. 0.01,0.02,0.03")
    ap.add_argument("--stakes", default="btc=15,default=2", help="per-coin stake, e.g. btc=15,eth=2,default=2")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--phase", choices=["all","pull","windows","resolve","prices","simulate"], default="all")
    ap.add_argument("--force", action="store_true", help="rebuild phase outputs")
    args = ap.parse_args()
    tiers = [float(x) for x in args.tiers.split(",")]
    stakes, default_stake = parse_stakes(args.stakes)

    acquire_lock()
    signal.signal(signal.SIGTERM, lambda *_: (release_lock(), sys.exit(130)))
    signal.signal(signal.SIGINT,  lambda *_: (release_lock(), sys.exit(130)))
    t0 = time.time()
    try:
        run = args.phase
        raw = windows = markets = prices = None
        if run in ("all","pull"):
            raw = phase_pull(args.days, args.force)
        if run in ("all","windows"):
            if raw is None: raw = json.load(open(PATHS["raw"]))
            windows = phase_windows(raw, args.force)
        if run in ("all","resolve"):
            if windows is None: windows = json.load(open(PATHS["windows"]))
            markets = phase_resolve(windows, args.workers, args.force)
        if run in ("all","prices"):
            if windows is None: windows = json.load(open(PATHS["windows"]))
            if markets is None: markets = json.load(open(PATHS["markets"]))
            prices = phase_prices(windows, markets, args.workers, args.force)
        if run in ("all","simulate"):
            if windows is None: windows = json.load(open(PATHS["windows"]))
            if markets is None: markets = json.load(open(PATHS["markets"]))
            if prices  is None: prices  = json.load(open(PATHS["prices"]))
            phase_simulate(windows, markets, prices, tiers, stakes, default_stake)
    finally:
        release_lock()
    log(f"Done in {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
