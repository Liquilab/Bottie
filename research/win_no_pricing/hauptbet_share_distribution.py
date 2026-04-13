#!/usr/bin/env python3
"""
Compute Cannae's hauptbet share (largest leg / game total) distribution.
Recent window only: April 1-7 2026 via PM Activity API.
Splits by sport (football vs NBA) so we can pick the right threshold.

Excludes -more-markets dupes (per learning_more_markets_duplication memory).
"""
import json
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
START = int(datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 4, 7, 23, 59, tzinfo=timezone.utc).timestamp())


def fetch_day(day_start, day_end):
    """Fetch one day, paged. PM has a 3500-offset hard cap."""
    trades = []
    offset = 0
    while offset < 3500:
        url = (f"https://data-api.polymarket.com/activity?user={CANNAE}"
               f"&type=trade&limit=500&offset={offset}&start={day_start}&end={day_end}")
        req = urllib.request.Request(url, headers={'User-Agent': 'Bottie/1'})
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            print(f"  err offset={offset}: {e}")
            break
        if not data:
            break
        trades.extend(data)
        if len(data) < 500:
            break
        offset += 500
    return trades


def fetch_all():
    trades = []
    day = 86400
    cur = START
    while cur < END:
        day_trades = fetch_day(cur, min(cur + day, END))
        print(f"  {datetime.fromtimestamp(cur, tz=timezone.utc).date()}: {len(day_trades)} trades")
        trades.extend(day_trades)
        cur += day
    return trades


def is_nba(slug: str) -> bool:
    return slug.startswith("nba-")


def is_football(slug: str) -> bool:
    prefixes = ("bun-", "elc-", "arg-", "bra-", "es2-", "fl1-", "lal-", "sea-",
                "fr2-", "aus-", "por-", "ere-", "tur-", "spl-", "ucl-", "uel-",
                "efa-", "efl-", "mls-", "chi-", "fif-", "mex-", "rou1-",
                "acn-", "cde-", "itc-")
    return any(slug.startswith(p) for p in prefixes)


def main():
    trades = fetch_all()
    print(f"Fetched {len(trades)} Cannae trades for April 1-7 2026 UTC\n")

    # Net positions per (slug, condition_id)
    games = defaultdict(lambda: defaultdict(float))
    for t in trades:
        slug = t.get("eventSlug", "")
        # Skip more-markets dupes — they're a separate event for the same game
        if slug.endswith("-more-markets"):
            continue
        cid = t.get("conditionId", "")
        size = float(t.get("usdcSize", 0) or 0)
        side = t.get("side", "")
        if side == "BUY":
            games[slug][cid] += size
        elif side == "SELL":
            games[slug][cid] -= size

    # For each game compute hauptbet share
    nba_shares = []
    football_shares = []
    for slug, legs in games.items():
        # Only legs with net buy > 0
        positive = {c: v for c, v in legs.items() if v > 0}
        if not positive:
            continue
        total = sum(positive.values())
        if total < 100:  # tiny dust games
            continue
        biggest = max(positive.values())
        share = biggest / total
        if is_nba(slug):
            nba_shares.append((slug, total, share, len(positive)))
        elif is_football(slug):
            football_shares.append((slug, total, share, len(positive)))

    def bucket_dist(shares, label):
        print(f"{label}: n={len(shares)} games (≥$100 game total)")
        if not shares:
            return
        buckets = [(0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6),
                   (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
        for lo, hi in buckets:
            in_b = [s for s in shares if lo <= s[2] < hi]
            pct = len(in_b) / len(shares) * 100
            avg_total = sum(s[1] for s in in_b) / len(in_b) if in_b else 0
            bar = "█" * int(pct / 2)
            print(f"  {lo*100:>3.0f}-{hi*100:>3.0f}%  n={len(in_b):>3} "
                  f"({pct:>5.1f}%)  avg game ${avg_total:>7,.0f}  {bar}")
        # Cumulative: % of games passing each threshold
        print(f"\n  Cumulative pass rate at threshold:")
        for thr in (0.40, 0.50, 0.60, 0.70):
            passing = [s for s in shares if s[2] >= thr]
            pct = len(passing) / len(shares) * 100
            avg_total = sum(s[1] for s in passing) / len(passing) if passing else 0
            print(f"    ≥{thr*100:>2.0f}%:  {len(passing):>3}/{len(shares)} = {pct:>5.1f}%  "
                  f"avg ${avg_total:>7,.0f}")
        print()

    bucket_dist(football_shares, "FOOTBALL")
    bucket_dist(nba_shares, "NBA")

    # Cross-check: what % of games pass current $4K floor?
    print("Cross-check: current $4K floor pass rate")
    fb_4k = [s for s in football_shares if s[1] >= 4000]
    nba_4k = [s for s in nba_shares if s[1] >= 4000]
    print(f"  Football: {len(fb_4k)}/{len(football_shares)} = "
          f"{len(fb_4k)/len(football_shares)*100 if football_shares else 0:.1f}%")
    print(f"  NBA:      {len(nba_4k)}/{len(nba_shares)} = "
          f"{len(nba_4k)/len(nba_shares)*100 if nba_shares else 0:.1f}%")


if __name__ == "__main__":
    main()
