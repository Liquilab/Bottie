#!/usr/bin/env python3
"""
Cannae Quant Analysis — Full edge decomposition + ongoing monitoring.

Datasource: /activity endpoint (trades + redeems) = no survivorship bias.
Run: cd /opt/bottie && python3 research/cannae_quant_analysis/analyze.py

Output:
  research/cannae_quant_analysis/report.json
  research/cannae_quant_analysis/history/YYYY-MM-DD.json
  research/cannae_quant_analysis/data/activity_raw.jsonl (append-only)
"""

import json
import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cannae_quant")

API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "CannaeQuant/1.0", "Accept": "application/json"}

BASE = Path(__file__).parent
DATA = BASE / "data"
HISTORY = BASE / "history"
DATA.mkdir(parents=True, exist_ok=True)
HISTORY.mkdir(parents=True, exist_ok=True)

RAW_FILE = DATA / "activity_raw.jsonl"
EVENT_CACHE = DATA / "events_cache.json"
REPORT_FILE = BASE / "report.json"

# ---------- Config ----------

def load_cannae_address():
    """Get Cannae address from config.yaml — never hardcode."""
    import yaml
    for path in [Path("config.yaml"), Path("/opt/bottie/config.yaml")]:
        if path.exists():
            cfg = yaml.safe_load(path.read_text())
            for w in cfg.get("copy_trading", {}).get("watchlist", []):
                if w.get("name", "").lower() == "cannae":
                    addr = w["address"]
                    log.info(f"Cannae address from config: {addr}")
                    return addr
    raise RuntimeError("Cannae address not found in config.yaml")


# ---------- Data Fetching ----------

def fetch_activity(client: httpx.Client, address: str, atype: str, max_offset=3500) -> list:
    """Fetch all activity records of a given type."""
    results = []
    offset = 0
    while offset < max_offset:
        url = f"{API}/activity?user={address}&type={atype}&limit=500&offset={offset}"
        try:
            resp = client.get(url, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            log.warning(f"Stop {atype} at offset {offset}: {e}")
            break
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        log.info(f"  {atype} offset={offset} → {len(batch)} records")
        if len(batch) < 500:
            break
        offset += 500
    return results


def fetch_positions(client: httpx.Client, address: str, max_offset=10000) -> list:
    """Fetch all open positions (includes resolved losers at curPrice~0)."""
    results = []
    offset = 0
    while offset < max_offset:
        url = f"{API}/positions?user={address}&limit=500&offset={offset}"
        try:
            resp = client.get(url, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            log.warning(f"Stop positions at offset {offset}: {e}")
            break
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return results


def fetch_event_metadata(client: httpx.Client, slugs: set, cache: dict) -> dict:
    """Fetch event metadata from Gamma API, using cache."""
    new_slugs = [s for s in slugs if s and s not in cache]
    if not new_slugs:
        return cache

    log.info(f"Fetching metadata for {len(new_slugs)} new events...")
    for i, slug in enumerate(new_slugs):
        if i > 0 and i % 50 == 0:
            log.info(f"  ... {i}/{len(new_slugs)}")
        try:
            resp = client.get(f"{GAMMA}/events?slug={slug}", timeout=15)
            resp.raise_for_status()
            events = resp.json()
            if events:
                e = events[0]
                cache[slug] = {
                    "start_date": e.get("startDate", ""),
                    "end_date": e.get("endDate", ""),
                    "volume": float(e.get("volume", 0) or 0),
                    "liquidity": float(e.get("liquidity", 0) or 0),
                    "resolved": e.get("closed", False),
                }
        except Exception:
            pass  # skip, will retry next run

    return cache


def save_raw(records: list):
    """Append-only save to JSONL, dedup on transactionHash."""
    existing = set()
    if RAW_FILE.exists():
        for line in RAW_FILE.read_text().splitlines():
            try:
                r = json.loads(line)
                existing.add(r.get("transactionHash", ""))
            except Exception:
                pass

    new = 0
    with RAW_FILE.open("a") as f:
        for r in records:
            txh = r.get("transactionHash", "")
            if txh and txh not in existing:
                f.write(json.dumps(r) + "\n")
                existing.add(txh)
                new += 1
    log.info(f"Raw data: {new} new records appended ({len(existing)} total)")


# ---------- Classification ----------

def classify_market_type(title: str) -> str:
    t = (title or "").lower()
    if "spread" in t:
        return "spread"
    if "o/u " in t or "over/under" in t:
        return "ou"
    if "draw" in t or "end in a draw" in t:
        return "draw"
    if "both teams" in t or "btts" in t:
        return "btts"
    return "win"


def detect_league(slug: str) -> str:
    if not slug:
        return "unknown"
    return slug.split("-")[0]


# ---------- Statistics ----------

def wilson_ci(wins: int, total: int, z=1.96) -> tuple:
    """Wilson score 95% confidence interval."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (round(max(0, center - spread), 4), round(min(1, center + spread), 4))


# ---------- Analysis Modules ----------

def build_resolved_bets(trades: list, redeems: list, positions: list) -> list:
    """
    Build resolved bet records by matching trades → redeems (winners) and
    trades → positions with curPrice<=0.05 (losers).
    """
    # Redeemed conditionIds = confirmed winners
    redeemed_cids = set()
    redeem_by_cid = {}
    for r in redeems:
        cid = r.get("conditionId", "")
        if cid:
            redeemed_cids.add(cid)
            redeem_by_cid[cid] = r

    # Positions with curPrice near 0 = confirmed losers
    loser_cids = set()
    open_cids = set()
    for p in positions:
        cid = p.get("conditionId", "") or ""
        if not cid:
            continue
        size = float(p.get("size", 0) or 0)
        if size < 0.01:
            continue
        cur = float(p.get("curPrice", 0) or 0)
        if cur <= 0.05:
            loser_cids.add(cid)
        elif cur < 0.95:
            open_cids.add(cid)

    # Group BUY trades by conditionId
    buys_by_cid = defaultdict(list)
    for t in trades:
        if t.get("side") != "BUY":
            continue
        cid = t.get("conditionId", "")
        if cid:
            buys_by_cid[cid].append(t)

    results = []
    for cid, cid_trades in buys_by_cid.items():
        t0 = cid_trades[0]
        title = t0.get("title", "") or ""
        slug = t0.get("eventSlug", "") or t0.get("slug", "") or ""
        event_slug = slug.split("-more-markets")[0] if slug else ""
        outcome = t0.get("outcome", "") or ""

        total_cost = sum(float(x.get("usdcSize", 0) or 0) for x in cid_trades)
        total_shares = sum(float(x.get("size", 0) or 0) for x in cid_trades)
        prices = [float(x.get("price", 0) or 0) for x in cid_trades]
        avg_price = total_cost / total_shares if total_shares > 0 else 0
        timestamps = [int(x.get("timestamp", 0) or 0) for x in cid_trades]
        first_ts = min(timestamps) if timestamps else 0
        last_ts = max(timestamps) if timestamps else 0

        if cid in redeemed_cids:
            result = "WIN"
            pnl = total_shares - total_cost
        elif cid in loser_cids:
            result = "LOSS"
            pnl = -total_cost
        elif cid in open_cids:
            result = "OPEN"
            pnl = 0
        else:
            result = "UNKNOWN"
            pnl = 0

        results.append({
            "cid": cid,
            "title": title,
            "event_slug": event_slug,
            "outcome": outcome,
            "mt": classify_market_type(title),
            "league": detect_league(event_slug),
            "cost": total_cost,
            "shares": total_shares,
            "avg_price": round(avg_price, 4),
            "result": result,
            "pnl": round(pnl, 2),
            "n_trades": len(cid_trades),
            "first_ts": first_ts,
            "last_ts": last_ts,
        })

    return results


def analyze_by_group(resolved: list, key_fn) -> dict:
    """Generic group-by analysis with WR, ROI, PnL, Wilson CI."""
    groups = defaultdict(list)
    for r in resolved:
        k = key_fn(r)
        groups[k].append(r)

    output = {}
    for k, bets in sorted(groups.items(), key=lambda x: -len(x[1])):
        wins = sum(1 for b in bets if b["result"] == "WIN")
        losses = sum(1 for b in bets if b["result"] == "LOSS")
        total = wins + losses
        cost = sum(b["cost"] for b in bets)
        pnl = sum(b["pnl"] for b in bets)
        wr = wins / total if total > 0 else 0
        roi = pnl / cost if cost > 0 else 0
        avg_cost = cost / total if total > 0 else 0
        ci = wilson_ci(wins, total)

        output[k] = {
            "bets": total,
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 4),
            "wr_ci_95": list(ci),
            "roi": round(roi, 4),
            "pnl": round(pnl, 2),
            "avg_cost": round(avg_cost, 2),
        }
    return output


def analyze_sizing_signal(resolved: list) -> dict:
    """Split bets into quartiles by cost, check if bigger bets win more."""
    if len(resolved) < 20:
        return {"insufficient_data": True}

    costs = sorted(r["cost"] for r in resolved)
    q25 = costs[len(costs) // 4]
    q50 = costs[len(costs) // 2]
    q75 = costs[3 * len(costs) // 4]

    quartiles = {"Q1_small": [], "Q2": [], "Q3": [], "Q4_large": []}
    for r in resolved:
        c = r["cost"]
        if c <= q25:
            quartiles["Q1_small"].append(r)
        elif c <= q50:
            quartiles["Q2"].append(r)
        elif c <= q75:
            quartiles["Q3"].append(r)
        else:
            quartiles["Q4_large"].append(r)

    result = {"thresholds": {"q25": round(q25, 2), "q50": round(q50, 2), "q75": round(q75, 2)}}
    for label, bets in quartiles.items():
        wins = sum(1 for b in bets if b["result"] == "WIN")
        total = len(bets)
        wr = wins / total if total > 0 else 0
        pnl = sum(b["pnl"] for b in bets)
        cost = sum(b["cost"] for b in bets)
        roi = pnl / cost if cost > 0 else 0
        result[label] = {"bets": total, "wr": round(wr, 4), "roi": round(roi, 4), "pnl": round(pnl, 2)}

    # Spearman rank correlation: size vs outcome (1=win, 0=loss)
    pairs = [(r["cost"], 1 if r["result"] == "WIN" else 0) for r in resolved]
    if len(pairs) >= 10:
        from functools import cmp_to_key
        costs_ranked = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
        n = len(pairs)
        rank_cost = [0] * n
        rank_outcome = [0] * n
        for rank, idx in enumerate(costs_ranked):
            rank_cost[idx] = rank
        outcomes_ranked = sorted(range(n), key=lambda i: pairs[i][1])
        for rank, idx in enumerate(outcomes_ranked):
            rank_outcome[idx] = rank
        d_sq = sum((rank_cost[i] - rank_outcome[i]) ** 2 for i in range(n))
        spearman = 1 - 6 * d_sq / (n * (n * n - 1))
        result["spearman_r"] = round(spearman, 4)

    return result


def analyze_timing(resolved: list, event_cache: dict) -> dict:
    """Analyze WR by hours before game start."""
    buckets = {
        ">24h": [], "12-24h": [], "6-12h": [], "2-6h": [],
        "1-2h": [], "30m-1h": [], "<30m": []
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

        r["hours_before_start"] = round(hours, 2)

        if hours > 24:
            buckets[">24h"].append(r)
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
            "wr": round(wins / total, 4) if total > 0 else 0,
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "avg_entry_price": round(statistics.mean(b["avg_price"] for b in bets), 4),
        }
    return result


def analyze_leg_correlation(resolved: list) -> dict:
    """Check if legs within the same game correlate."""
    by_event = defaultdict(list)
    for r in resolved:
        if r["event_slug"]:
            by_event[r["event_slug"]].append(r)

    multi_leg_games = {k: v for k, v in by_event.items() if len(v) >= 2}
    if len(multi_leg_games) < 10:
        return {"insufficient_data": True, "multi_leg_games": len(multi_leg_games)}

    all_win = 0
    mixed = 0
    all_loss = 0
    pairs = []  # (leg1_win, leg2_win) for correlation

    for slug, legs in multi_leg_games.items():
        outcomes = [1 if l["result"] == "WIN" else 0 for l in legs]
        if all(o == 1 for o in outcomes):
            all_win += 1
        elif all(o == 0 for o in outcomes):
            all_loss += 1
        else:
            mixed += 1

        # Pairwise for correlation
        for i in range(len(outcomes)):
            for j in range(i + 1, len(outcomes)):
                pairs.append((outcomes[i], outcomes[j]))

    total = all_win + mixed + all_loss
    result = {
        "multi_leg_games": total,
        "all_win_pct": round(all_win / total, 4) if total > 0 else 0,
        "mixed_pct": round(mixed / total, 4) if total > 0 else 0,
        "all_loss_pct": round(all_loss / total, 4) if total > 0 else 0,
    }

    # Pearson correlation on pairs
    if len(pairs) >= 10:
        x = [p[0] for p in pairs]
        y = [p[1] for p in pairs]
        mx, my = statistics.mean(x), statistics.mean(y)
        cov = sum((a - mx) * (b - my) for a, b in pairs) / len(pairs)
        sx = statistics.pstdev(x)
        sy = statistics.pstdev(y)
        if sx > 0 and sy > 0:
            result["pearson_r"] = round(cov / (sx * sy), 4)

    return result


def analyze_edge_decay(resolved: list) -> dict:
    """Rolling 30-bet WR and ROI."""
    sorted_bets = sorted(resolved, key=lambda r: r["first_ts"])
    window = 30
    if len(sorted_bets) < window:
        return {"insufficient_data": True}

    rolling = []
    for i in range(window, len(sorted_bets) + 1):
        chunk = sorted_bets[i - window:i]
        wins = sum(1 for b in chunk if b["result"] == "WIN")
        cost = sum(b["cost"] for b in chunk)
        pnl = sum(b["pnl"] for b in chunk)
        ts = chunk[-1]["first_ts"]
        rolling.append({
            "ts": ts,
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "wr": round(wins / window, 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
        })

    # Simple trend: compare first half vs second half
    mid = len(rolling) // 2
    first_half_wr = statistics.mean(r["wr"] for r in rolling[:mid])
    second_half_wr = statistics.mean(r["wr"] for r in rolling[mid:])

    trend = "stable"
    if second_half_wr < first_half_wr - 0.05:
        trend = "declining"
    elif second_half_wr > first_half_wr + 0.05:
        trend = "improving"

    return {
        "trend": trend,
        "first_half_wr": round(first_half_wr, 4),
        "second_half_wr": round(second_half_wr, 4),
        "rolling_points": rolling,
    }


def analyze_hauptbet(resolved: list) -> dict:
    """Per game: identify hauptbet (biggest $), measure WR by market type."""
    by_event = defaultdict(list)
    for r in resolved:
        if r["event_slug"]:
            by_event[r["event_slug"]].append(r)

    stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "cost": 0.0, "legs": []})
    for slug, legs in by_event.items():
        legs.sort(key=lambda x: x["cost"], reverse=True)
        hb = legs[0]
        mt = hb["mt"]
        s = stats[mt]
        if hb["result"] == "WIN":
            s["w"] += 1
        else:
            s["l"] += 1
        s["pnl"] += hb["pnl"]
        s["cost"] += hb["cost"]
        s["legs"].append(len(legs))

    result = {}
    for mt in ["win", "ou", "spread", "draw", "btts"]:
        s = stats[mt]
        total = s["w"] + s["l"]
        if total == 0:
            continue
        result[mt] = {
            "games": total,
            "wins": s["w"],
            "losses": s["l"],
            "wr": round(s["w"] / total, 4),
            "roi": round(s["pnl"] / s["cost"], 4) if s["cost"] > 0 else 0,
            "pnl": round(s["pnl"], 2),
            "avg_legs": round(statistics.mean(s["legs"]), 1),
        }

    # --- Hauptbet-only vs full-game comparison ---
    comparison = {"games": [], "summary": {}}
    multi_leg_events = {slug: legs for slug, legs in by_event.items() if len(legs) > 1}

    totals = {"hb_cost": 0, "hb_pnl": 0, "full_cost": 0, "full_pnl": 0,
              "hedge_helped": 0, "hedge_hurt": 0, "hedge_neutral": 0}

    for slug, legs in sorted(multi_leg_events.items(), key=lambda x: -sum(l["cost"] for l in x[1])):
        legs.sort(key=lambda x: x["cost"], reverse=True)
        hb = legs[0]
        non_hb = legs[1:]

        full_cost = sum(l["cost"] for l in legs)
        full_pnl = sum(l["pnl"] for l in legs)
        hb_cost = hb["cost"]
        hb_pnl = hb["pnl"]
        non_hb_pnl = sum(l["pnl"] for l in non_hb)
        non_hb_cost = sum(l["cost"] for l in non_hb)

        full_roi = full_pnl / full_cost if full_cost > 0 else 0
        hb_roi = hb_pnl / hb_cost if hb_cost > 0 else 0

        totals["hb_cost"] += hb_cost
        totals["hb_pnl"] += hb_pnl
        totals["full_cost"] += full_cost
        totals["full_pnl"] += full_pnl

        if non_hb_pnl > 0:
            totals["hedge_helped"] += 1
        elif non_hb_pnl < -0.01:
            totals["hedge_hurt"] += 1
        else:
            totals["hedge_neutral"] += 1

        comparison["games"].append({
            "slug": slug,
            "hauptbet": {"mt": hb["mt"], "outcome": hb["outcome"], "result": hb["result"],
                         "cost": round(hb_cost, 2), "pnl": round(hb_pnl, 2), "roi": round(hb_roi, 4)},
            "full_game": {"legs": len(legs), "cost": round(full_cost, 2), "pnl": round(full_pnl, 2),
                          "roi": round(full_roi, 4)},
            "non_hb": {"legs": len(non_hb), "cost": round(non_hb_cost, 2), "pnl": round(non_hb_pnl, 2)},
        })

    n = len(multi_leg_events)
    comparison["summary"] = {
        "multi_leg_games": n,
        "hauptbet_only": {
            "total_cost": round(totals["hb_cost"], 2),
            "total_pnl": round(totals["hb_pnl"], 2),
            "roi": round(totals["hb_pnl"] / totals["hb_cost"], 4) if totals["hb_cost"] > 0 else 0,
        },
        "full_game": {
            "total_cost": round(totals["full_cost"], 2),
            "total_pnl": round(totals["full_pnl"], 2),
            "roi": round(totals["full_pnl"] / totals["full_cost"], 4) if totals["full_cost"] > 0 else 0,
        },
        "hedge_impact": {
            "helped": totals["hedge_helped"],
            "hurt": totals["hedge_hurt"],
            "neutral": totals["hedge_neutral"],
            "net_pnl": round(totals["full_pnl"] - totals["hb_pnl"], 2),
            "net_cost": round(totals["full_cost"] - totals["hb_cost"], 2),
        },
    }

    result["_comparison"] = comparison
    return result


def generate_alerts(report: dict) -> list:
    """Check for drift vs expected performance."""
    alerts = []
    overall = report.get("overall", {})
    wr = overall.get("wr", 0)
    roi = overall.get("roi", 0)

    if wr < 0.70:
        alerts.append({"level": "WARNING", "msg": f"WR dropped to {wr:.1%} (expected >70%)"})
    if roi < 0.20:
        alerts.append({"level": "WARNING", "msg": f"ROI dropped to {roi:.1%} (expected >20%)"})

    # Check if majority of bets are in leagues not in our config
    by_league = report.get("by_league", {})
    total_bets = sum(v.get("bets", 0) for v in by_league.values())
    our_leagues = {"epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por", "bra", "itc", "ere", "es2", "bl2", "sea"}
    outside_bets = sum(v.get("bets", 0) for k, v in by_league.items() if k not in our_leagues)
    if total_bets > 0 and outside_bets / total_bets > 0.30:
        pct = outside_bets / total_bets
        alerts.append({
            "level": "WARNING",
            "msg": f"{pct:.0%} of Cannae's bets are in leagues outside our config (e.g. NBA, NHL)",
        })

    return alerts


def generate_recommendations(report: dict) -> list:
    """Auto-generate strategy recommendations from data."""
    recs = []
    by_league = report.get("by_league", {})
    our_leagues = {"epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por", "bra", "itc", "ere", "es2", "bl2", "sea"}

    # League recommendations
    for league, stats in by_league.items():
        if league not in our_leagues and stats.get("bets", 0) >= 10 and stats.get("roi", 0) > 0.10:
            recs.append(f"Add {league.upper()} to Cannae config? {stats['bets']} bets, {stats['wr']:.0%} WR, {stats['roi']:.0%} ROI, ${stats['pnl']:.0f} PnL")

    # Timing recommendation
    timing = report.get("timing", {})
    early_bets = sum(timing.get(b, {}).get("bets", 0) for b in [">24h", "12-24h", "6-12h"])
    late_bets = sum(timing.get(b, {}).get("bets", 0) for b in ["1-2h", "30m-1h", "<30m"])
    total_timed = early_bets + late_bets + timing.get("2-6h", {}).get("bets", 0)
    if total_timed > 0 and early_bets / total_timed > 0.50:
        recs.append(f"T-30 may be too late: {early_bets/total_timed:.0%} of bets placed >6h before start")

    # Leg correlation
    corr = report.get("leg_correlation", {})
    r = corr.get("pearson_r", 0)
    if r < 0.3 and corr.get("multi_leg_games", 0) >= 10:
        recs.append(f"Legs are independent (r={r:.2f}). Consider max_legs=3 for more alpha per game.")
    elif r > 0.7:
        recs.append(f"Legs highly correlated (r={r:.2f}). Extra legs add exposure, not diversification.")

    return recs


# ---------- Data Collection (importable) ----------

def collect_cannae_data(address: str = None, client: httpx.Client = None) -> dict:
    """Collect all Cannae data — importable by intelligence modules.

    Returns dict with: trades, redeems, positions, all_bets, resolved, open_bets, event_cache
    """
    if address is None:
        address = load_cannae_address()
    own_client = client is None
    if own_client:
        client = httpx.Client(headers=UA)

    try:
        log.info("Fetching activity (trades + redeems)...")
        trades = fetch_activity(client, address, "trade")
        redeems = fetch_activity(client, address, "redeem")
        log.info(f"Fetched: {len(trades)} trades, {len(redeems)} redeems")

        log.info("Fetching positions...")
        positions = fetch_positions(client, address)
        log.info(f"Fetched: {len(positions)} positions")

        save_raw(trades + redeems)

        all_bets = build_resolved_bets(trades, redeems, positions)
        resolved = [b for b in all_bets if b["result"] in ("WIN", "LOSS")]
        open_bets = [b for b in all_bets if b["result"] == "OPEN"]
        log.info(f"Total bets: {len(all_bets)} (resolved: {len(resolved)}, open: {len(open_bets)})")

        # Event metadata
        event_cache = {}
        if EVENT_CACHE.exists():
            event_cache = json.loads(EVENT_CACHE.read_text())
        slugs = {b["event_slug"] for b in all_bets if b["event_slug"]}
        event_cache = fetch_event_metadata(client, slugs, event_cache)
        EVENT_CACHE.write_text(json.dumps(event_cache, indent=2))

        return {
            "address": address,
            "trades": trades,
            "redeems": redeems,
            "positions": positions,
            "all_bets": all_bets,
            "resolved": resolved,
            "open_bets": open_bets,
            "event_cache": event_cache,
        }
    finally:
        if own_client:
            client.close()


# ---------- Main ----------

def main():
    log.info("=" * 70)
    log.info("CANNAE QUANT ANALYSIS")
    log.info("=" * 70)

    dataset = collect_cannae_data()
    resolved = dataset["resolved"]
    open_bets = dataset["open_bets"]
    all_bets = dataset["all_bets"]
    event_cache = dataset["event_cache"]

    result_counts = Counter(b["result"] for b in all_bets)
    log.info(f"  WIN={result_counts['WIN']} LOSS={result_counts['LOSS']} OPEN={result_counts['OPEN']} UNKNOWN={result_counts.get('UNKNOWN',0)}")

    # --- Run analyses ---
    log.info("\n--- Running analysis modules ---")

    # Overall
    total_wins = sum(1 for b in resolved if b["result"] == "WIN")
    total_cost = sum(b["cost"] for b in resolved)
    total_pnl = sum(b["pnl"] for b in resolved)
    overall = {
        "bets": len(resolved),
        "wins": total_wins,
        "losses": len(resolved) - total_wins,
        "wr": round(total_wins / len(resolved), 4) if resolved else 0,
        "wr_ci_95": list(wilson_ci(total_wins, len(resolved))),
        "roi": round(total_pnl / total_cost, 4) if total_cost > 0 else 0,
        "pnl": round(total_pnl, 2),
    }

    by_mt = analyze_by_group(resolved, lambda r: r["mt"])
    by_league = analyze_by_group(resolved, lambda r: r["league"])
    sizing = analyze_sizing_signal(resolved)
    timing = analyze_timing(resolved, event_cache)
    correlation = analyze_leg_correlation(resolved)
    decay = analyze_edge_decay(resolved)
    hauptbet = analyze_hauptbet(resolved)

    # --- Build report ---
    ts_range = sorted(b["first_ts"] for b in resolved if b["first_ts"])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "from": datetime.fromtimestamp(ts_range[0], tz=timezone.utc).strftime("%Y-%m-%d") if ts_range else "",
            "to": datetime.fromtimestamp(ts_range[-1], tz=timezone.utc).strftime("%Y-%m-%d") if ts_range else "",
        },
        "total_bets_analyzed": len(all_bets),
        "resolved_bets": len(resolved),
        "open_bets": len(open_bets),
        "overall": overall,
        "by_market_type": by_mt,
        "by_league": by_league,
        "sizing_signal": sizing,
        "timing": timing,
        "leg_correlation": correlation,
        "edge_decay": decay,
        "hauptbet_analysis": hauptbet,
        "alerts": generate_alerts({"overall": overall, "by_league": by_league, "timing": timing, "leg_correlation": correlation}),
        "recommendations": generate_recommendations({"overall": overall, "by_league": by_league, "timing": timing, "leg_correlation": correlation}),
    }

    # --- Save ---
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    log.info(f"\nReport saved to {REPORT_FILE}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history_file = HISTORY / f"{today}.json"
    history_file.write_text(json.dumps(report, indent=2))
    log.info(f"History saved to {history_file}")

    # --- Print summary ---
    log.info("\n" + "=" * 80)
    log.info("SUMMARY")
    log.info("=" * 80)
    log.info(f"Overall: {overall['bets']} bets, {overall['wr']:.1%} WR [{overall['wr_ci_95'][0]:.0%}-{overall['wr_ci_95'][1]:.0%}], {overall['roi']:.1%} ROI, ${overall['pnl']:.0f} PnL")

    log.info("\nBy Market Type:")
    for mt in ["win", "ou", "spread", "draw", "btts"]:
        if mt in by_mt:
            s = by_mt[mt]
            log.info(f"  {mt:10s} {s['bets']:4d} bets  WR={s['wr']:.0%} [{s['wr_ci_95'][0]:.0%}-{s['wr_ci_95'][1]:.0%}]  ROI={s['roi']:.0%}  PnL=${s['pnl']:.0f}")

    log.info("\nBy League (top 10):")
    for league, s in sorted(by_league.items(), key=lambda x: -x[1]["pnl"])[:10]:
        log.info(f"  {league:10s} {s['bets']:4d} bets  WR={s['wr']:.0%}  ROI={s['roi']:.0%}  PnL=${s['pnl']:.0f}")

    log.info(f"\nSizing signal: {'Q4 WR=' + str(sizing.get('Q4_large',{}).get('wr','?')) + ' vs Q1 WR=' + str(sizing.get('Q1_small',{}).get('wr','?')) if not sizing.get('insufficient_data') else 'insufficient data'}")
    log.info(f"Leg correlation: {correlation}")
    log.info(f"Edge decay trend: {decay.get('trend', 'unknown')}")

    if report["alerts"]:
        log.warning("\nALERTS:")
        for a in report["alerts"]:
            log.warning(f"  [{a['level']}] {a['msg']}")

    if report["recommendations"]:
        log.info("\nRECOMMENDATIONS:")
        for r in report["recommendations"]:
            log.info(f"  → {r}")

    log.info("\nDone.")


if __name__ == "__main__":
    main()
