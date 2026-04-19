#!/usr/bin/env python3
"""Pull our bot's own trade activity from Polymarket data-api.

Bot funder: 0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a (fivemin-bot / bottie-test).

Output:
  data/bot_activity_raw.json — all events 30d (TRADE + REDEEM)
  data/bot_windows.json      — per-window aggregate with same schema as hv_windows.json

Reuses data/clob_markets_cache.json for winner lookup (populated by hv_pipeline).
"""
from __future__ import annotations
import json, os, sys, time, urllib.request, urllib.error, argparse
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

BOT = "0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a"
API = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DATA = Path("data")
RAW = DATA / "bot_activity_raw.json"
WINDOWS = DATA / "bot_windows.json"

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)

def http_get(url, timeout=15, retries=3):
    last=None
    for i in range(retries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404): raise
            last=e
        except Exception as e:
            last=e
        time.sleep(1+i*2)
    raise last

def atomic_write_json(path, obj):
    tmp=str(path)+".tmp"
    with open(tmp,"w") as f: json.dump(obj, f)
    os.replace(tmp, path)

def pull(days: int):
    cutoff = int(time.time()) - days*86400
    log(f"Pull bot activity since {datetime.fromtimestamp(cutoff,tz=timezone.utc).isoformat()}")
    all_items=[]; seen=set()
    end_ts=int(time.time())
    while True:
        url=f"{API}/activity?user={BOT}&limit=500&end={end_ts}"
        batch=http_get(url, timeout=20)
        if not batch: break
        new=[]
        for a in batch:
            k=(a.get("transactionHash",""), a.get("asset",""), a.get("side",""), a.get("timestamp",0), a.get("type",""))
            if k in seen: continue
            seen.add(k); new.append(a)
        all_items.extend(new)
        oldest=min(a.get("timestamp",end_ts) for a in batch)
        log(f"  end={end_ts} got={len(batch)} new={len(new)} oldest={datetime.fromtimestamp(oldest,tz=timezone.utc).strftime('%m-%d %H:%M')} total={len(all_items)}")
        if oldest<cutoff or not new: break
        nxt=oldest-1
        if nxt>=end_ts: break
        end_ts=nxt; time.sleep(0.2)
    crypto=[a for a in all_items if a.get("timestamp",0)>=cutoff and "-updown-5m-" in a.get("slug","")]
    coins=Counter(a.get("slug","").split("-")[0] for a in crypto)
    log(f"Crypto 5M subset: {len(crypto)}  coins={dict(coins)}")
    atomic_write_json(RAW, crypto)
    return crypto

def aggregate(raw):
    log(f"Aggregating {len(raw)} events into windows...")
    agg=defaultdict(lambda: {"slug":"","coin":"","conditionId":"","title":"",
                             "window_start":0, "n_trades":0, "n_redeems":0,
                             "buy_usdc":0.0, "sell_usdc":0.0, "redeem_usdc":0.0,
                             "buy_shares_up":0.0, "buy_shares_down":0.0,
                             "sell_shares_up":0.0, "sell_shares_down":0.0})
    nt=nr=0
    for a in raw:
        slug=a.get("slug",""); w=agg[slug]
        w["slug"]=slug
        w["coin"]=slug.split("-")[0] if slug else ""
        w["conditionId"]=a.get("conditionId","") or w["conditionId"]
        w["title"]=a.get("title","") or w["title"]
        try: w["window_start"]=int(slug.rsplit("-",1)[-1])
        except: pass
        t=a.get("type",""); size=float(a.get("size",0)); usdc=float(a.get("usdcSize",0))
        outc=a.get("outcome",""); side=a.get("side","")
        if t=="TRADE":
            w["n_trades"]+=1; nt+=1
            if side=="BUY":
                w["buy_usdc"]+=usdc
                if outc=="Up": w["buy_shares_up"]+=size
                elif outc=="Down": w["buy_shares_down"]+=size
            elif side=="SELL":
                w["sell_usdc"]+=usdc
                if outc=="Up": w["sell_shares_up"]+=size
                elif outc=="Down": w["sell_shares_down"]+=size
        elif t=="REDEEM":
            w["n_redeems"]+=1; w["redeem_usdc"]+=usdc; nr+=1
    log(f"  TRADE={nt} REDEEM={nr}")
    windows=[]
    for slug,w in agg.items():
        w["held_up"]=round(w.pop("buy_shares_up")-w.pop("sell_shares_up"),4)
        w["held_down"]=round(w.pop("buy_shares_down")-w.pop("sell_shares_down"),4)
        w["buy_usdc"]=round(w["buy_usdc"],2)
        w["sell_usdc"]=round(w["sell_usdc"],2)
        w["redeem_usdc"]=round(w["redeem_usdc"],2)
        windows.append(w)
    windows.sort(key=lambda x: x["window_start"])
    atomic_write_json(WINDOWS, windows)
    coins=Counter(w["coin"] for w in windows)
    log(f"Saved {len(windows)} windows. Coins: {dict(coins)}")
    return windows

if __name__ == "__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args=ap.parse_args()
    DATA.mkdir(exist_ok=True)
    t0=time.time()
    raw=pull(args.days)
    aggregate(raw)
    log(f"Done in {time.time()-t0:.1f}s")
