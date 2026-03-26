"""Module 5: Temporal Analysis — WHEN does Cannae bet and in what patterns?"""

import logging
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("intelligence.temporal")


def analyze_temporal(dataset: dict) -> dict:
    """Full temporal analysis: hour-of-day, day-of-week, batches, hours-before-start."""
    resolved = dataset["resolved"]
    event_cache = dataset["event_cache"]

    return {
        "hour_of_day": _hour_distribution(resolved),
        "day_of_week": _day_distribution(resolved),
        "batches": _detect_batches(resolved),
        "hours_before_start": _hours_before_start(resolved, event_cache),
    }


def _hour_distribution(resolved: list) -> dict:
    """Bets per UTC hour with WR/ROI."""
    buckets = defaultdict(lambda: {"w": 0, "l": 0, "cost": 0.0, "pnl": 0.0})
    for r in resolved:
        if not r["first_ts"]:
            continue
        dt = datetime.fromtimestamp(r["first_ts"], tz=timezone.utc)
        h = dt.hour
        b = buckets[h]
        b["cost"] += r["cost"]
        b["pnl"] += r["pnl"]
        if r["result"] == "WIN":
            b["w"] += 1
        else:
            b["l"] += 1

    result = {}
    for h in range(24):
        b = buckets[h]
        total = b["w"] + b["l"]
        if total == 0:
            continue
        result[f"{h:02d}"] = {
            "bets": total,
            "wr": round(b["w"] / total, 4),
            "roi": round(b["pnl"] / b["cost"], 4) if b["cost"] > 0 else 0,
            "pnl": round(b["pnl"], 2),
        }
    return result


def _day_distribution(resolved: list) -> dict:
    """Bets per day-of-week with WR/ROI."""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    buckets = defaultdict(lambda: {"w": 0, "l": 0, "cost": 0.0, "pnl": 0.0})
    for r in resolved:
        if not r["first_ts"]:
            continue
        dt = datetime.fromtimestamp(r["first_ts"], tz=timezone.utc)
        d = dt.weekday()
        b = buckets[d]
        b["cost"] += r["cost"]
        b["pnl"] += r["pnl"]
        if r["result"] == "WIN":
            b["w"] += 1
        else:
            b["l"] += 1

    result = {}
    for d in range(7):
        b = buckets[d]
        total = b["w"] + b["l"]
        if total == 0:
            continue
        result[days[d]] = {
            "bets": total,
            "wr": round(b["w"] / total, 4),
            "roi": round(b["pnl"] / b["cost"], 4) if b["cost"] > 0 else 0,
            "pnl": round(b["pnl"], 2),
        }
    return result


def _detect_batches(resolved: list, gap_minutes: int = 30) -> dict:
    """Detect betting batches: groups of bets within gap_minutes of each other."""
    sorted_bets = sorted(resolved, key=lambda r: r["first_ts"])
    if not sorted_bets:
        return {"batches": 0}

    batches = []
    current_batch = [sorted_bets[0]]

    for bet in sorted_bets[1:]:
        prev_ts = current_batch[-1]["first_ts"]
        if bet["first_ts"] - prev_ts <= gap_minutes * 60:
            current_batch.append(bet)
        else:
            batches.append(current_batch)
            current_batch = [bet]
    batches.append(current_batch)

    sizes = [len(b) for b in batches]
    gaps_hours = []
    for i in range(1, len(batches)):
        gap = (batches[i][0]["first_ts"] - batches[i - 1][-1]["first_ts"]) / 3600
        gaps_hours.append(round(gap, 1))

    return {
        "total_batches": len(batches),
        "avg_batch_size": round(sum(sizes) / len(sizes), 1) if sizes else 0,
        "max_batch_size": max(sizes) if sizes else 0,
        "median_gap_hours": round(sorted(gaps_hours)[len(gaps_hours) // 2], 1) if gaps_hours else 0,
        "batch_size_distribution": {
            "1_bet": sum(1 for s in sizes if s == 1),
            "2_5_bets": sum(1 for s in sizes if 2 <= s <= 5),
            "6_10_bets": sum(1 for s in sizes if 6 <= s <= 10),
            "11_plus": sum(1 for s in sizes if s > 10),
        },
    }


def _hours_before_start(resolved: list, event_cache: dict) -> dict:
    """WR by hours before game start — extends existing timing analysis."""
    buckets = {
        ">48h": [], "24-48h": [], "12-24h": [], "6-12h": [],
        "2-6h": [], "1-2h": [], "30m-1h": [], "<30m": [],
    }

    for r in resolved:
        slug = r["event_slug"]
        meta = event_cache.get(slug, {})
        start_str = meta.get("start_date", "")
        if not start_str or not r["first_ts"]:
            continue
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            trade_dt = datetime.fromtimestamp(r["first_ts"], tz=timezone.utc)
            hours = (start - trade_dt).total_seconds() / 3600
        except Exception:
            continue

        if hours > 48:
            buckets[">48h"].append(r)
        elif hours > 24:
            buckets["24-48h"].append(r)
        elif hours > 12:
            buckets["12-24h"].append(r)
        elif hours > 6:
            buckets["6-12h"].append(r)
        elif hours > 2:
            buckets["2-6h"].append(r)
        elif hours > 1:
            buckets["1-2h"].append(r)
        elif hours > 0.5:
            buckets["30m-1h"].append(r)
        else:
            buckets["<30m"].append(r)

    result = {}
    for label, bets in buckets.items():
        if not bets:
            result[label] = {"bets": 0}
            continue
        wins = sum(1 for b in bets if b["result"] == "WIN")
        total = len(bets)
        cost = sum(b["cost"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)
        result[label] = {
            "bets": total,
            "wr": round(wins / total, 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "avg_price": round(sum(b["avg_price"] for b in bets) / total, 4),
        }
    return result
