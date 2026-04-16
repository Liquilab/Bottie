#!/usr/bin/env python3
"""Smart Wallet Scout v3 — classifies trading style, scores directional bets only.

Distinguishes arb/spread traders from directional bettors:
- Per event: 2+ outcomes = arb/spread, 1 outcome = directional
- Only scores directional events for WR/ROI
- Uses CLOB /markets/{cid} for resolution truth (not curPrice)

Usage:
    python scripts/smart_scout.py evaluate 0xABC
    python scripts/smart_scout.py evaluate 0xABC 0xDEF --min-games 10
    python scripts/smart_scout.py discover --league atp --days 7
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
RATE_LIMIT = 0.12

_cache_file = os.path.join(os.path.dirname(__file__), "..", "data", "resolution_cache.json")
_resolution_cache = {}


def api_get(url):
    time.sleep(RATE_LIMIT)
    req = urllib.request.Request(url, headers={"User-Agent": "Scout/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def load_cache():
    global _resolution_cache
    try:
        _resolution_cache = json.load(open(_cache_file))
    except (FileNotFoundError, json.JSONDecodeError):
        _resolution_cache = {}


def save_cache():
    os.makedirs(os.path.dirname(_cache_file), exist_ok=True)
    json.dump(_resolution_cache, open(_cache_file, "w"))


def check_resolution(cid: str) -> tuple[bool, str | None]:
    """Check market resolution via CLOB API. Returns (is_resolved, winner_outcome)."""
    if cid in _resolution_cache:
        c = _resolution_cache[cid]
        if c is not None:
            return c.get("resolved", False), c.get("winner")
        return False, None

    try:
        mkt = api_get(f"{CLOB_API}/markets/{cid}")
        closed = mkt.get("closed", False)
        winner = None
        for tok in mkt.get("tokens", []):
            if tok.get("winner") is True:
                winner = tok.get("outcome")
        resolved = closed and winner is not None
        _resolution_cache[cid] = {"resolved": resolved, "winner": winner}
        return resolved, winner
    except Exception:
        _resolution_cache[cid] = None
        return False, None


def is_game_bet(slug: str) -> bool:
    """Only individual matches (slug has date), no futures."""
    if not slug:
        return False
    if any(kw in slug for kw in ["winner", "season", "trophy", "champion", "golden-boot", "mvp"]):
        return False
    parts = slug.split("-")
    for i, p in enumerate(parts):
        if len(p) == 4 and p.isdigit() and i + 2 < len(parts):
            if len(parts[i + 1]) == 2 and len(parts[i + 2]) == 2:
                return True
    return False


def is_win_market(title: str) -> bool:
    """Filter: only win markets, no spread/ou/btts/draw/maps."""
    t = title.lower()
    skip = ["spread", "o/u", "over", "under", "both teams", "btts", "corner",
            "halftime", "exact", "total goals", "draw", "map ", "game 1",
            "game 2", "game 3", "total corners"]
    return not any(kw in t for kw in skip)


def evaluate_wallet(address: str, min_resolved: int = 10) -> dict:
    """Evaluate wallet: classify style, score directional bets only."""
    address = address.lower()

    # Fetch all positions (paginated)
    all_pos = []
    offset = 0
    while True:
        try:
            data = api_get(f"{DATA_API}/positions?user={address}&limit=500&sizeThreshold=0.01&offset={offset}")
        except Exception as e:
            print(f"  Error: {e}", file=sys.stderr)
            break
        if not data:
            break
        all_pos.extend(data)
        if len(data) < 500:
            break
        offset += len(data)

    # Group by eventSlug
    by_event = defaultdict(list)
    for p in all_pos:
        slug = p.get("eventSlug", "") or ""
        if not slug or "-more-markets" in slug:
            continue
        if not is_game_bet(slug):
            continue
        title = p.get("title", "") or ""
        if not is_win_market(title):
            continue
        size = float(p.get("size", 0))
        if size < 0.01:
            continue
        by_event[slug].append(p)

    print(f"  {len(all_pos)} total positions, {len(by_event)} game-bet win-only events", flush=True)

    # Classify each event: single-sided vs both-sides
    single_events = {}  # slug -> [positions]
    arb_events = {}
    for slug, positions in by_event.items():
        outcomes = set(p.get("outcome", "") for p in positions)
        if len(outcomes) >= 2:
            arb_events[slug] = positions
        else:
            single_events[slug] = positions

    total = len(by_event)
    n_single = len(single_events)
    n_arb = len(arb_events)
    arb_pct = n_arb / total * 100 if total > 0 else 0

    print(f"  Style: {n_single} single-sided, {n_arb} both-sides ({arb_pct:.0f}% arb)", flush=True)

    if arb_pct > 70:
        style = "arb_trader"
    elif arb_pct > 30:
        style = "mixed"
    else:
        style = "directional"

    # Score ONLY single-sided events via CLOB resolution
    stats = defaultdict(lambda: {"w": 0, "l": 0, "open": 0, "w_pnl": 0.0, "l_pnl": 0.0, "trades": []})
    checked = 0

    for slug, positions in single_events.items():
        checked += 1
        if checked % 50 == 0:
            print(f"  Checking {checked}/{n_single}...", end="\r", flush=True)
            save_cache()

        # All positions have same outcome (single-sided)
        pos = positions[0]
        cid = pos.get("conditionId", "")
        outcome = pos.get("outcome", "")
        total_iv = sum(float(p.get("initialValue", 0) or 0) for p in positions)
        total_shares = sum(float(p.get("size", 0)) for p in positions)
        avg_price = total_iv / total_shares if total_shares > 0 else 0

        resolved, winner = check_resolution(cid)

        prefix = slug.split("-")[0]
        if prefix in ("lol", "cs2", "dota2", "val"):
            cat = "esports"
        elif prefix in ("atp", "wta"):
            cat = "tennis"
        else:
            cat = prefix

        if not resolved:
            stats[cat]["open"] += 1
            continue

        won = outcome == winner
        pnl = (total_shares - total_iv) if won else -total_iv

        if won:
            stats[cat]["w"] += 1
            stats[cat]["w_pnl"] += pnl
        else:
            stats[cat]["l"] += 1
            stats[cat]["l_pnl"] += pnl

        stats[cat]["trades"].append({
            "result": "W" if won else "L",
            "pnl": round(pnl, 2),
            "iv": round(total_iv, 2),
            "avg_price": round(avg_price, 3),
            "outcome": outcome,
            "slug": slug,
        })

    print(flush=True)
    save_cache()

    # Aggregate
    total_w = sum(s["w"] for s in stats.values())
    total_l = sum(s["l"] for s in stats.values())
    total_resolved = total_w + total_l
    total_wpnl = sum(s["w_pnl"] for s in stats.values())
    total_lpnl = sum(s["l_pnl"] for s in stats.values())

    return {
        "address": address,
        "profile_url": f"https://polymarket.com/profile/{address}",
        "total_positions": len(all_pos),
        "game_events": total,
        "style": style,
        "arb_pct": round(arb_pct, 1),
        "single_sided_events": n_single,
        "arb_events": n_arb,
        "directional_resolved": total_resolved,
        "directional_wins": total_w,
        "directional_losses": total_l,
        "directional_wr": round(total_w / total_resolved * 100, 1) if total_resolved > 0 else 0,
        "directional_pnl": round(total_wpnl + total_lpnl, 2),
        "by_league": {
            cat: {
                "w": s["w"], "l": s["l"], "open": s["open"],
                "wr": round(s["w"] / (s["w"] + s["l"]) * 100, 1) if (s["w"] + s["l"]) > 0 else 0,
                "pnl": round(s["w_pnl"] + s["l_pnl"], 2),
                "sample": sorted(s["trades"], key=lambda t: -abs(t["pnl"]))[:5],
            }
            for cat, s in sorted(stats.items())
        },
    }


def discover_wallets(league: str, days: int = 7, min_bets: int = 5):
    """Find active wallets from recent resolved games."""
    import subprocess
    try:
        result = subprocess.run(
            ["ssh", "-T", "-o", "ConnectTimeout=10", "root@78.141.222.227",
             "cat /opt/bottie/data/schedule_cache.json"],
            capture_output=True, text=True, timeout=20)
        sched = json.loads(result.stdout)
    except Exception as ex:
        print(f"Error: {ex}")
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    cids = []
    for g in sched:
        slug = g.get("event_slug", "")
        prefix = slug.split("-")[0]
        if prefix != league and league != "all":
            continue
        if not is_game_bet(slug) or "-more-markets" in slug:
            continue
        st = g.get("start_time", "")
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if dt < cutoff or dt > now:
                continue
        except ValueError:
            continue
        for cid in g.get("condition_ids", [])[:3]:
            cids.append((cid, slug))

    print(f"Found {len(cids)} markets in '{league}' last {days} days", flush=True)

    wallet_bets = defaultdict(lambda: {"slugs": set(), "total_size": 0})
    for i, (cid, slug) in enumerate(cids):
        if i % 10 == 0:
            print(f"  Scanning {i + 1}/{len(cids)}...", end="\r", flush=True)
        try:
            trades = api_get(f"{DATA_API}/trades?market={cid}&limit=200")
        except Exception:
            continue
        for t in trades:
            if (t.get("side") or "").upper() != "BUY":
                continue
            wallet = (t.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            wallet_bets[wallet]["slugs"].add(slug)
            wallet_bets[wallet]["total_size"] += float(t.get("size", 0))

    candidates = [
        {"address": w, "games": len(info["slugs"]), "total_size": round(info["total_size"], 2),
         "profile_url": f"https://polymarket.com/profile/{w}"}
        for w, info in wallet_bets.items() if len(info["slugs"]) >= min_bets
    ]
    candidates.sort(key=lambda c: -c["games"])
    return candidates[:30]


def main():
    parser = argparse.ArgumentParser(description="Smart Wallet Scout v3")
    sub = parser.add_subparsers(dest="cmd")

    ev = sub.add_parser("evaluate", help="Evaluate wallet(s)")
    ev.add_argument("wallets", nargs="+")
    ev.add_argument("--min-games", type=int, default=10)

    disc = sub.add_parser("discover", help="Find wallets from recent games")
    disc.add_argument("--league", default="all")
    disc.add_argument("--days", type=int, default=7)
    disc.add_argument("--min-bets", type=int, default=5)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    load_cache()

    if args.cmd == "evaluate":
        results = []
        for wallet in args.wallets:
            print(f"\n{'=' * 60}")
            print(f"Evaluating {wallet[:12]}...")
            r = evaluate_wallet(wallet, args.min_games)
            results.append(r)

            # Print summary
            marker = {"arb_trader": "ARB", "mixed": "MIX", "directional": "DIR"}[r["style"]]
            print(f"\n  [{marker}] {r['address'][:12]} | {r['arb_pct']:.0f}% arb | "
                  f"directional: {r['directional_wins']}W/{r['directional_losses']}L "
                  f"({r['directional_wr']}% WR) | PnL ${r['directional_pnl']:+,.0f}")
            print(f"  Profile: {r['profile_url']}")

            if r["style"] == "arb_trader":
                print(f"  ⚠️  ARB TRADER — {r['arb_pct']:.0f}% of events are both-sides. NOT copyable via hauptbet.")

            print()
            for cat, s in r["by_league"].items():
                res = s["w"] + s["l"]
                if res == 0 and s["open"] == 0:
                    continue
                print(f"  {cat:<12s}  {s['w']}W/{s['l']}L  WR {s['wr']:>5.1f}%  PnL ${s['pnl']:>+8,.0f}  ({s['open']} open)")
                for t in s["sample"][:3]:
                    print(f"    [{t['result']}] ${t['pnl']:>+7,.0f} @ {t['avg_price']*100:.0f}¢  {t['slug'][:40]}")

        out_file = os.path.join(os.path.dirname(__file__), "..", "data", "smart_scout.json")
        json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "results": results}, open(out_file, "w"), indent=2)
        print(f"\nSaved to {out_file}")

    elif args.cmd == "discover":
        candidates = discover_wallets(args.league, args.days, args.min_bets)
        print(f"\n{'Wallet':<14s} {'Games':>5s} {'Volume':>10s}  URL")
        print("-" * 70)
        for c in candidates:
            print(f"{c['address'][:12]:<14s} {c['games']:>5d} ${c['total_size']:>9,.0f}  {c['profile_url']}")

        out_file = os.path.join(os.path.dirname(__file__), "..", "data", "scout_candidates.json")
        json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "candidates": candidates}, open(out_file, "w"), indent=2)
        print(f"\nNext: evaluate top candidates:")
        print(f"  python scripts/smart_scout.py evaluate {' '.join(c['address'] for c in candidates[:5])}")

    save_cache()


if __name__ == "__main__":
    main()
