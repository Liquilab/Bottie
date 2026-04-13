#!/usr/bin/env python3
"""Cannae per-leg ROI analyse — Stap 2 van strategy shift onderzoek.

Bouwt twee tabellen:
  1. Long history baseline uit cannae_closed_full.csv (tot 2026-03-20)
  2. Recent venster (2026-03-21 -> nu) uit fresh PM Activity API

Per (sport, leg) berekent: n, WR, ROI, Wilson LB.
Doel: detecteren of Cannae's hauptbet-allocatie en/of leg-ROI is verschoven van
win/draw naar ou/spread in de afgelopen ~7 dagen.
"""
import csv
import json
import math
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
CSV_PATH = Path(__file__).parent.parent / "cannae_trades" / "cannae_closed_full.csv"
API = "https://data-api.polymarket.com"
RECENT_FROM = "2026-03-21"  # eerste dag NA cutoff van CSV
RECENT_END  = "2026-04-07"  # vandaag (exclusive end)

# ---------- classifiers ----------

def detect_leg(title: str) -> str:
    t = (title or "").lower()
    # player props (e.g. "LeBron James: Points O/U 25.5")
    if ("points o/u" in t or "rebounds o/u" in t or "assists o/u" in t) and ": " in t:
        return "player_prop"
    if "o/u" in t or "over/under" in t:
        return "ou"
    if "spread" in t:
        return "spread"
    if "both teams to score" in t or "btts" in t:
        return "btts"
    if "draw" in t:
        return "draw"
    return "win"

NBA_RE = re.compile(r"^nba-")
NFL_RE = re.compile(r"^nfl-")
NHL_RE = re.compile(r"^nhl-")
MLB_RE = re.compile(r"^mlb-")
CBB_RE = re.compile(r"^(ncaa|cbb)-")

def detect_sport(event_slug: str) -> str:
    s = (event_slug or "").lower()
    if NBA_RE.match(s): return "nba"
    if NFL_RE.match(s): return "nfl"
    if NHL_RE.match(s): return "nhl"
    if MLB_RE.match(s): return "mlb"
    if CBB_RE.match(s): return "cbb"
    if not s: return "unknown"
    # default = football
    return "football"

# ---------- stats ----------

def wilson_lb(wins, n, z=1.96):
    if n == 0: return 0.0
    p = wins / n
    denom = 1 + z*z/n
    centre = p + z*z/(2*n)
    spread = z * math.sqrt((p*(1-p) + z*z/(4*n)) / n)
    return (centre - spread) / denom

def roll(bets):
    """bets: list of (cost, pnl, won_int)"""
    n = len(bets)
    if n == 0: return None
    cost = sum(b[0] for b in bets)
    pnl  = sum(b[1] for b in bets)
    wins = sum(b[2] for b in bets)
    wr   = wins / n
    roi  = (pnl / cost * 100) if cost > 0 else 0.0
    wlb  = wilson_lb(wins, n) * 100
    return {"n": n, "wins": wins, "cost": cost, "pnl": pnl, "wr": wr*100, "roi": roi, "wlb": wlb}

def fmt_row(label, s):
    if s is None:
        return f"  {label:<30s} n=0"
    return (f"  {label:<30s} n={s['n']:4d}  W={s['wins']:4d}  "
            f"cost=${s['cost']:>10,.0f}  pnl=${s['pnl']:>+10,.0f}  "
            f"WR={s['wr']:5.1f}%  ROI={s['roi']:+6.1f}%  WilsonLB={s['wlb']:5.1f}%")

# ---------- 1) long history from CSV ----------

def load_csv_bets():
    bets = []
    with open(CSV_PATH) as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                avg = float(r["avg_price"])
                shares = float(r["total_bought"])
                pnl = float(r["realized_pnl"])
                won = int(r["won"])
            except (ValueError, KeyError):
                continue
            cost = shares * avg
            if cost <= 0:
                continue
            bets.append({
                "date": r["date"],
                "sport": detect_sport(r["event_slug"]),
                "leg": detect_leg(r["title"]),
                "event_slug": r["event_slug"],
                "title": r["title"],
                "outcome": r["outcome"],
                "cost": cost,
                "pnl": pnl,
                "won": won,
            })
    return bets

# ---------- 2) recent window from API ----------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "B/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def fetch_activity(atype, max_offset=10000):
    """Paginate /activity?type=trade|redeem for cannae wallet."""
    out = []
    limit = 500
    offset = 0
    while offset < max_offset:
        url = f"{API}/activity?user={CANNAE}&limit={limit}&offset={offset}&type={atype}"
        try:
            chunk = http_get(url)
        except Exception as e:
            print(f"  fetch err offset={offset}: {e}", file=sys.stderr)
            break
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
        time.sleep(0.15)
    return out

def build_recent_bets(trades, redeems):
    """Group trades by conditionId (REDEEM events are per-condition, not per-outcome).

    Per conditionId:
      cost   = sum(BUY usdcSize) - sum(SELL usdcSize) across all outcomes
      payout = sum(REDEEM usdcSize) for that conditionId (0 if all outcomes lost)
      pnl    = payout - cost
      won    = 1 if payout > cost else 0
    Resolution: a condition is considered resolved iff (a) it has a redeem event,
    OR (b) the eventSlug's parsed date is >= 2 days ago (likely settled-as-loss).
    """
    def slug_date(slug):
        # extract YYYY-MM-DD from slug like 'nba-bos-mia-2026-04-05'
        m = re.search(r"(\d{4}-\d{2}-\d{2})", slug or "")
        return m.group(1) if m else None

    def in_window(ev):
        ts = ev.get("timestamp")
        if not ts: return False
        d = time.strftime("%Y-%m-%d", time.gmtime(ts))
        return RECENT_FROM <= d < RECENT_END

    trades_w  = [t for t in trades  if in_window(t)]
    redeems_w = [r for r in redeems if in_window(r)]
    # Redeems can happen days after the trade — also include redeems whose conditionId
    # appears in trades_w even if redeem ts is outside window (rare edge).
    print(f"  recent window {RECENT_FROM}..{RECENT_END}: {len(trades_w)} trades, {len(redeems_w)} redeems (in-window)")

    pos = defaultdict(lambda: {"buy_usdc": 0.0, "sell_usdc": 0.0, "title": None,
                                "slug": None, "first_ts": None})
    for t in trades_w:
        cid = t.get("conditionId")
        if not cid: continue
        p = pos[cid]
        side = (t.get("side") or "").upper()
        usdc = float(t.get("usdcSize") or 0)
        if side == "BUY":  p["buy_usdc"]  += usdc
        elif side == "SELL": p["sell_usdc"] += usdc
        if p["title"] is None:
            p["title"] = t.get("title") or ""
            p["slug"]  = t.get("eventSlug") or t.get("slug") or ""
        ts = t.get("timestamp")
        if p["first_ts"] is None or ts < p["first_ts"]:
            p["first_ts"] = ts

    # Sum redeems per cid (across full redeem set, not just in-window)
    redeem_payout = defaultdict(float)
    for r in redeems:
        cid = r.get("conditionId")
        if not cid: continue
        redeem_payout[cid] += float(r.get("usdcSize") or 0)

    today = time.strftime("%Y-%m-%d", time.gmtime())
    cutoff = time.strftime("%Y-%m-%d",
                            time.gmtime(time.time() - 2*86400))  # 2 days ago

    bets = []
    skipped_open = 0
    for cid, p in pos.items():
        cost = p["buy_usdc"] - p["sell_usdc"]
        if cost <= 0.5:
            continue
        payout = redeem_payout.get(cid, 0.0)
        sd = slug_date(p["slug"])
        resolved = (payout > 0) or (sd is not None and sd <= cutoff)
        if not resolved:
            skipped_open += 1
            continue
        pnl = payout - cost
        won = 1 if payout > cost else 0
        bets.append({
            "date": sd or time.strftime("%Y-%m-%d", time.gmtime(p["first_ts"])),
            "sport": detect_sport(p["slug"]),
            "leg": detect_leg(p["title"]),
            "event_slug": p["slug"],
            "title": p["title"],
            "outcome": "",
            "cost": cost,
            "pnl": pnl,
            "won": won,
        })
    print(f"  built {len(bets)} resolved recent bets, skipped {skipped_open} open/unresolved")
    return bets

# ---------- aggregator ----------

def report(bets, label):
    print(f"\n{'='*78}")
    print(f"{label}  (n={len(bets)})")
    print('='*78)

    # Per (sport, leg)
    by = defaultdict(list)
    for b in bets:
        by[(b["sport"], b["leg"])].append((b["cost"], b["pnl"], b["won"]))

    sports = sorted({s for s, _ in by.keys()})
    legs = ["win", "draw", "spread", "ou", "btts", "player_prop"]

    for sport in sports:
        print(f"\n  -- sport: {sport} --")
        for leg in legs:
            sub = by.get((sport, leg))
            if not sub: continue
            print(fmt_row(leg, roll(sub)))
        # totals for sport
        all_sub = [x for (s, _), v in by.items() if s == sport for x in v]
        print(fmt_row("ALL legs", roll(all_sub)))

    # Hauptbet-leg-distributie per sport (per game = grootste bet hoort tot leg X)
    # group bets by event_slug, pick the one with highest cost
    print(f"\n  -- hauptbet leg distributie per sport (per game) --")
    by_game = defaultdict(list)
    for b in bets:
        by_game[(b["sport"], b["event_slug"])].append(b)
    haupt = defaultdict(lambda: defaultdict(int))
    for (sport, _slug), gb in by_game.items():
        top = max(gb, key=lambda x: x["cost"])
        haupt[sport][top["leg"]] += 1
    for sport in sorted(haupt.keys()):
        legs_dist = haupt[sport]
        total = sum(legs_dist.values())
        parts = ", ".join(f"{l}={legs_dist[l]} ({legs_dist[l]/total*100:.0f}%)"
                          for l in legs if legs_dist.get(l, 0) > 0)
        print(f"    {sport:<10s} games={total:4d}   {parts}")

# ---------- main ----------

def main():
    print("Loading historical CSV...")
    csv_bets = load_csv_bets()
    print(f"  {len(csv_bets)} bets loaded")
    if csv_bets:
        dates = [b["date"] for b in csv_bets if b["date"]]
        print(f"  date range: {min(dates)} -> {max(dates)}")

    report(csv_bets, "LONG HISTORY (CSV, tot 2026-03-20)")

    # Lange-vs-recente window: ook splits binnen CSV op 7 dagen voor cutoff
    cut = "2026-03-13"
    long_old = [b for b in csv_bets if b["date"] < cut]
    long_recent_csv = [b for b in csv_bets if b["date"] >= cut]
    report(long_old, f"CSV vroeg (< {cut})")
    report(long_recent_csv, f"CSV laatste week (>= {cut} t/m 2026-03-20)")

    print("\nFetching fresh PM activity (trades + redeems)...")
    trades = fetch_activity("trade")
    print(f"  {len(trades)} trades")
    redeems = fetch_activity("redeem")
    print(f"  {len(redeems)} redeems")

    recent_bets = build_recent_bets(trades, redeems)
    report(recent_bets, f"RECENT VENSTER ({RECENT_FROM} t/m {RECENT_END} excl)")

if __name__ == "__main__":
    main()
