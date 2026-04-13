#!/usr/bin/env python3
"""Stap 3 — hauptbet-leg verschuiving over tijd.

Per game (event_slug) bepaalt de hauptbet-leg (hoogste cost). Aggregeert per
ISO-week en per dag voor NBA en voetbal. Output: tabellen + ASCII bar charts.
"""
import csv
import re
import sys
from collections import defaultdict
from datetime import date as Date
from pathlib import Path

CSV_PATH = Path(__file__).parent.parent / "cannae_trades" / "cannae_closed_full.csv"

# ---------- classifiers (zelfde als stap 2) ----------

def detect_leg(title):
    t = (title or "").lower()
    if ("points o/u" in t or "rebounds o/u" in t or "assists o/u" in t) and ": " in t:
        return "player_prop"
    if "o/u" in t or "over/under" in t: return "ou"
    if "spread" in t: return "spread"
    if "both teams to score" in t or "btts" in t: return "btts"
    if "draw" in t: return "draw"
    return "win"

def detect_sport(slug):
    s = (slug or "").lower()
    if s.startswith("nba-"): return "nba"
    if s.startswith("nfl-"): return "nfl"
    if s.startswith("nhl-"): return "nhl"
    if s.startswith("mlb-"): return "mlb"
    if s.startswith(("ncaa-", "cbb-")): return "cbb"
    if not s: return "unknown"
    return "football"

def slug_date(slug):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", slug or "")
    return m.group(1) if m else None

def iso_week(d_str):
    """YYYY-MM-DD -> ISO week label 'YYYY-Www'"""
    y, m, d = map(int, d_str.split("-"))
    iy, iw, _ = Date(y, m, d).isocalendar()
    return f"{iy}-W{iw:02d}"

# ---------- load ----------

def load_bets():
    bets = []
    with open(CSV_PATH) as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                cost = float(r["total_bought"]) * float(r["avg_price"])
            except (ValueError, KeyError):
                continue
            if cost <= 0: continue
            slug = r["event_slug"]
            sd = slug_date(slug) or r["date"]
            if not sd: continue
            bets.append({
                "date": sd,
                "sport": detect_sport(slug),
                "leg": detect_leg(r["title"]),
                "event_slug": slug,
                "cost": cost,
            })
    return bets

# ---------- aggregate ----------

def hauptbet_per_game(bets):
    """Returns dict event_slug -> {date, sport, leg, cost}"""
    by_game = defaultdict(list)
    for b in bets:
        by_game[b["event_slug"]].append(b)
    out = {}
    for slug, gb in by_game.items():
        top = max(gb, key=lambda x: x["cost"])
        out[slug] = {"date": top["date"], "sport": top["sport"],
                     "leg": top["leg"], "cost": top["cost"]}
    return out

def aggregate(haupt, sport, bucket_fn):
    """bucket_fn: date_str -> bucket label. Returns ordered list of (bucket, leg_counts dict)."""
    buckets = defaultdict(lambda: defaultdict(int))
    for slug, h in haupt.items():
        if h["sport"] != sport: continue
        b = bucket_fn(h["date"])
        buckets[b][h["leg"]] += 1
    return sorted(buckets.items())

# ---------- output ----------

LEGS = ["win", "draw", "spread", "ou", "btts", "player_prop"]

def print_table(rows, label):
    print(f"\n## {label}\n")
    print(f"| bucket | n | " + " | ".join(LEGS) + " |")
    print(f"|---|---|" + "|".join(["---"] * len(LEGS)) + "|")
    for bucket, counts in rows:
        n = sum(counts.values())
        if n == 0: continue
        cells = []
        for leg in LEGS:
            c = counts.get(leg, 0)
            if c == 0:
                cells.append("—")
            else:
                cells.append(f"{c} ({c/n*100:.0f}%)")
        print(f"| {bucket} | {n} | " + " | ".join(cells) + " |")

def print_bar_chart(rows, sport, label):
    """ASCII stacked bar: % win+draw vs % spread+ou+btts."""
    print(f"\n## {label} — % hauptbet = WIN+DRAW vs SPREAD+OU(+BTTS)\n")
    print("```")
    print(f"{'bucket':<12s} {'n':>4s}  {'WD':>5s} {'SOB':>5s}  {'visualisation (W=win/draw, S=spread/ou/btts)':<55s}")
    for bucket, counts in rows:
        n = sum(counts.values())
        if n == 0: continue
        wd = counts.get("win", 0) + counts.get("draw", 0)
        sob = counts.get("spread", 0) + counts.get("ou", 0) + counts.get("btts", 0)
        wd_pct = wd / n * 100
        sob_pct = sob / n * 100
        # 50 chars wide bar
        wd_chars = int(round(wd_pct / 2))
        sob_chars = int(round(sob_pct / 2))
        bar = "W" * wd_chars + "S" * sob_chars
        print(f"{bucket:<12s} {n:>4d}  {wd_pct:>4.0f}% {sob_pct:>4.0f}%  {bar}")
    print("```")

def main():
    print("Loading bets from CSV...")
    bets = load_bets()
    print(f"  {len(bets)} bets")

    haupt = hauptbet_per_game(bets)
    print(f"  {len(haupt)} unique games")

    for sport in ("nba", "football"):
        n_sport = sum(1 for h in haupt.values() if h["sport"] == sport)
        print(f"\n{'='*70}\n# {sport.upper()}  (games={n_sport})\n{'='*70}")

        # Per ISO week
        rows_w = aggregate(haupt, sport, iso_week)
        print_table(rows_w, f"{sport} — hauptbet per ISO week")
        print_bar_chart(rows_w, sport, f"{sport} weekly")

        # Per day (laatste 30 dagen tonen)
        rows_d = aggregate(haupt, sport, lambda d: d)
        rows_d_recent = rows_d[-30:]
        print_bar_chart(rows_d_recent, sport, f"{sport} dagelijks (laatste 30 dagen met data)")

if __name__ == "__main__":
    main()
