#!/usr/bin/env python3
"""Build analytics.json from HV + bot windows data.

Consumes:
  data/hv_windows.json
  data/bot_windows.json
  data/clob_markets_cache.json  (for winner lookup)

Produces:
  data/analytics.json with:
    - per_day_per_coin: [{date, coin, hv_pnl, bot_pnl, hv_cost, bot_cost, hv_wins, hv_losses, bot_wins, bot_losses}]
    - per_window_join:  [{slug, coin, date, hv_pnl, bot_pnl, delta, winner}]  (only windows in both sets)
    - summary: {hv_total_pnl_7d, bot_total_pnl_7d, delta_total, ...}

Dashboard reads this. M1 scope; predictive (σ regime, forecast) = M4.
"""
from __future__ import annotations
import json, os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")

def log(m): print(f"[analytics] {m}", flush=True)

def load_json(p, default):
    if not p.exists(): return default
    try: return json.load(open(p))
    except: return default

def pnl_for_window(row, markets):
    """Compute P&L. Prefer redeem_usdc (ground-truth USDC settlement),
    fallback held_winning × $1 if no REDEEM recorded but market resolved."""
    if row.get("redeem_usdc", 0) > 0:
        return row["sell_usdc"] - row["buy_usdc"] + row["redeem_usdc"]
    # Fallback: look up winner
    m = markets.get(row.get("conditionId"), {})
    winner = None
    for t in (m or {}).get("tokens", []):
        if t.get("winner") is True: winner = t.get("outcome")
    if not winner: return None  # unresolved
    held = row["held_up"] if winner == "Up" else row["held_down"]
    return row["sell_usdc"] - row["buy_usdc"] + held

def winner_for(row, markets):
    m = markets.get(row.get("conditionId"), {})
    for t in (m or {}).get("tokens", []):
        if t.get("winner") is True: return t.get("outcome")
    return None

def main():
    hv_windows = load_json(DATA / "hv_windows.json", [])
    bot_windows = load_json(DATA / "bot_windows.json", [])
    markets = load_json(DATA / "clob_markets_cache.json", {})
    log(f"HV windows: {len(hv_windows)}  Bot windows: {len(bot_windows)}  Markets: {len(markets)}")

    # Enrich with pnl + date
    def enrich(rows):
        out = []
        for r in rows:
            pnl = pnl_for_window(r, markets)
            if pnl is None: continue
            dt = datetime.fromtimestamp(r["window_start"], tz=timezone.utc)
            out.append({
                **r,
                "date": dt.strftime("%Y-%m-%d"),
                "weekday": dt.strftime("%A"),
                "hour_utc": dt.hour,
                "winner": winner_for(r, markets),
                "pnl": round(pnl, 2),
            })
        return out
    hv = enrich(hv_windows)
    bot = enrich(bot_windows)
    log(f"HV resolved: {len(hv)}  Bot resolved: {len(bot)}")

    # Per-day per-coin aggregates
    def agg_daily(rows):
        d = defaultdict(lambda: {"pnl":0.0,"cost":0.0,"wins":0,"losses":0,"flat":0,"n":0})
        for r in rows:
            k = (r["date"], r["coin"])
            s = d[k]
            s["pnl"] += r["pnl"]
            s["cost"] += r["buy_usdc"]
            s["n"] += 1
            if r["pnl"] > 0.01: s["wins"] += 1
            elif r["pnl"] < -0.01: s["losses"] += 1
            else: s["flat"] += 1
        return {f"{k[0]}|{k[1]}": v for k,v in d.items()}
    hv_daily = agg_daily(hv)
    bot_daily = agg_daily(bot)

    # Merge keys for per_day_per_coin table
    rows = []
    for k in sorted(set(hv_daily) | set(bot_daily)):
        date, coin = k.split("|")
        h = hv_daily.get(k, {"pnl":0,"cost":0,"wins":0,"losses":0,"flat":0,"n":0})
        b = bot_daily.get(k, {"pnl":0,"cost":0,"wins":0,"losses":0,"flat":0,"n":0})
        rows.append({
            "date": date, "coin": coin,
            "hv_pnl": round(h["pnl"],2), "hv_cost": round(h["cost"],2),
            "hv_w": h["wins"], "hv_l": h["losses"], "hv_n": h["n"],
            "hv_roi_pct": round(100*h["pnl"]/h["cost"],2) if h["cost"]>0 else None,
            "bot_pnl": round(b["pnl"],2), "bot_cost": round(b["cost"],2),
            "bot_w": b["wins"], "bot_l": b["losses"], "bot_n": b["n"],
            "bot_roi_pct": round(100*b["pnl"]/b["cost"],2) if b["cost"]>0 else None,
        })

    # Per-window join (windows where BOTH HV and bot participated)
    hv_by_slug = {r["slug"]: r for r in hv}
    bot_by_slug = {r["slug"]: r for r in bot}
    both = set(hv_by_slug) & set(bot_by_slug)
    joined = []
    for slug in both:
        h = hv_by_slug[slug]; b = bot_by_slug[slug]
        joined.append({
            "slug": slug, "coin": h["coin"], "date": h["date"], "window_start": h["window_start"],
            "winner": h.get("winner"),
            "hv_pnl": h["pnl"], "bot_pnl": b["pnl"],
            "delta": round(h["pnl"] - b["pnl"], 2),  # positive = HV outperformed
            "hv_cost": h["buy_usdc"], "bot_cost": b["buy_usdc"],
        })
    joined.sort(key=lambda r: r["window_start"], reverse=True)

    # Summary
    hv_total = sum(r["pnl"] for r in hv)
    bot_total = sum(r["pnl"] for r in bot)
    hv_cost_total = sum(r["buy_usdc"] for r in hv)
    bot_cost_total = sum(r["buy_usdc"] for r in bot)
    hv_wins = sum(1 for r in hv if r["pnl"] > 0.01)
    bot_wins = sum(1 for r in bot if r["pnl"] > 0.01)

    # Per-coin aggregate
    def by_coin(rows):
        d = defaultdict(lambda: {"pnl":0.0,"cost":0.0,"wins":0,"n":0})
        for r in rows:
            c = r["coin"]; d[c]["pnl"] += r["pnl"]; d[c]["cost"] += r["buy_usdc"]
            d[c]["n"] += 1
            if r["pnl"] > 0.01: d[c]["wins"] += 1
        return {c: {"pnl":round(v["pnl"],2), "cost":round(v["cost"],2), "wins":v["wins"], "n":v["n"],
                    "wr_pct":round(100*v["wins"]/v["n"],2) if v["n"]>0 else None,
                    "roi_pct":round(100*v["pnl"]/v["cost"],2) if v["cost"]>0 else None}
                for c,v in d.items()}

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_span": {
            "hv_windows": len(hv), "bot_windows": len(bot),
            "windows_in_both": len(both),
            "hv_date_range": [hv[0]["date"] if hv else None, hv[-1]["date"] if hv else None],
            "bot_date_range": [bot[0]["date"] if bot else None, bot[-1]["date"] if bot else None],
        },
        "summary": {
            "hv_total_pnl": round(hv_total, 2),
            "hv_total_cost": round(hv_cost_total, 2),
            "hv_roi_pct": round(100*hv_total/hv_cost_total, 2) if hv_cost_total>0 else None,
            "hv_wr_pct": round(100*hv_wins/len(hv), 2) if hv else None,
            "bot_total_pnl": round(bot_total, 2),
            "bot_total_cost": round(bot_cost_total, 2),
            "bot_roi_pct": round(100*bot_total/bot_cost_total, 2) if bot_cost_total>0 else None,
            "bot_wr_pct": round(100*bot_wins/len(bot), 2) if bot else None,
        },
        "per_coin_hv": by_coin(hv),
        "per_coin_bot": by_coin(bot),
        "per_day_per_coin": rows,
        "per_window_join": joined[:500],  # limit for dashboard size
    }

    tmp = DATA / "analytics.json.tmp"
    with open(tmp, "w") as f: json.dump(out, f, indent=2)
    os.replace(tmp, DATA / "analytics.json")
    log(f"Saved: data/analytics.json")
    log(f"Summary: HV pnl=${hv_total:+,.2f}  Bot pnl=${bot_total:+,.2f}  shared windows={len(both)}")

if __name__ == "__main__":
    main()
