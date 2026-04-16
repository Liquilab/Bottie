#!/usr/bin/env python3
"""Pinnacle CLV Scanner — finds mispriced Polymarket sports markets.

Compares Pinnacle sharp line to PM ask prices.
Only matches on 3 axes: league + date (±3h) + both teams.
Only individual matches, no futures/outrights.

Usage:
    ODDS_API_KEY=xxx python3 scripts/pinnacle_scan.py
    ODDS_API_KEY=xxx python3 scripts/pinnacle_scan.py --min-edge 5
    ODDS_API_KEY=xxx python3 scripts/pinnacle_scan.py --sport soccer_uefa_champs_league
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from team_match import match_event, extract_team_from_question, teams_match

CLOB = "https://clob.polymarket.com"
ODDS = "https://api.the-odds-api.com/v4"
RATE_LIMIT = 0.12

ALL_SPORTS = [
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_france_ligue_one", "soccer_italy_serie_a",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_turkey_super_league", "soccer_efl_champ",
    "soccer_mexico_ligamx", "soccer_usa_mls",
    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
    "soccer_denmark_superliga", "soccer_norway_eliteserien",
    "icehockey_nhl", "baseball_mlb",
    "tennis_atp_barcelona_open", "tennis_atp_munich", "tennis_wta_stuttgart_open",
]


def api_get(url):
    time.sleep(RATE_LIMIT)
    req = urllib.request.Request(url, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def get_pinnacle_probs(key: str, sport: str) -> list[dict]:
    """Fetch odds, extract Pinnacle vig-free probs. Falls back to average of all books."""
    url = f"{ODDS}/sports/{sport}/odds/?apiKey={key}&regions=eu&markets=h2h&oddsFormat=decimal"
    try:
        events = api_get(url)
    except Exception as e:
        print(f"  [{sport}] Odds API error: {e}", file=sys.stderr)
        return []

    results = []
    for e in events:
        home = e.get("home_team", "")
        away = e.get("away_team", "")
        commence = e.get("commence_time", "")

        # Extract per-team implied probabilities
        raw_probs: dict[str, list[float]] = {}
        pinnacle_probs: dict[str, float] = {}

        for bm in e.get("bookmakers", []):
            bm_key = bm.get("key", "")
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for o in mkt.get("outcomes", []):
                    name = o.get("name", "")
                    price = o.get("price", 0)
                    if price > 1:
                        prob = 1 / price
                        raw_probs.setdefault(name, []).append(prob)
                        if bm_key == "pinnacle":
                            pinnacle_probs[name] = prob

        # Use Pinnacle if available, else average
        probs = pinnacle_probs if pinnacle_probs else {k: sum(v)/len(v) for k, v in raw_probs.items()}
        source = "pinnacle" if pinnacle_probs else "average"

        # Remove vig (normalize to 100%)
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        results.append({
            "home": home, "away": away, "commence": commence,
            "probs": probs, "source": source,
        })
    return results


def load_pm_games(sched_path: str) -> list[dict]:
    """Load PM schedule and extract win-market team names."""
    if os.path.exists(sched_path):
        sched = json.load(open(sched_path))
    else:
        import subprocess
        result = subprocess.run(
            ["ssh", "-T", "-o", "ConnectTimeout=10", "root@78.141.222.227",
             "cat /opt/bottie/data/schedule_cache.json"],
            capture_output=True, text=True, timeout=20)
        sched = json.loads(result.stdout)

    games = []
    for g in sched:
        slug = g.get("event_slug", "")
        # Skip duplicates and special markets
        if any(kw in slug for kw in ["-more-", "-exact-", "-halftime-", "-corner"]):
            continue

        tokens = g.get("market_tokens", [])
        start_time = g.get("start_time", "")
        win_markets = []

        for cid, question, toks in tokens:
            q = question.lower()
            # Only win markets
            if any(kw in q for kw in ["draw", "spread", "o/u", "over", "under", "corner",
                                       "halftime", "exact", "both teams", "map ", "game 1",
                                       "game 2", "game 3", "total"]):
                continue
            if "win" not in q:
                continue

            team = extract_team_from_question(question)
            yes_tok = next((tid for o, tid in toks if o == "Yes"), None)
            if yes_tok and team:
                win_markets.append({"team": team, "cid": cid, "yes_token": yes_tok, "question": question})

        if win_markets:
            teams = [wm["team"] for wm in win_markets]
            games.append({"slug": slug, "start_time": start_time, "teams": teams, "win_markets": win_markets})

    return games


def get_best_ask(token_id: str) -> tuple[float, float]:
    """Get best ask price and depth from CLOB orderbook. Returns (price, depth_shares)."""
    try:
        book = api_get(f"{CLOB}/book?token_id={token_id}")
        asks = book.get("asks", [])
        if not asks:
            return 1.0, 0.0
        best = min(float(a["price"]) for a in asks)
        depth = sum(float(a["size"]) for a in asks if float(a["price"]) <= best + 0.02)
        return best, depth
    except Exception:
        return 1.0, 0.0


def main():
    parser = argparse.ArgumentParser(description="Pinnacle CLV Scanner")
    parser.add_argument("--min-edge", type=float, default=3.0, help="Min edge in cents (default 3)")
    parser.add_argument("--sport", type=str, default=None, help="Single sport to scan (default: all)")
    parser.add_argument("--sched", type=str, default="/opt/bottie/data/schedule_cache.json")
    args = parser.parse_args()

    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        print("ODDS_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    sports = [args.sport] if args.sport else ALL_SPORTS

    # Load PM games
    print("Loading PM schedule...", flush=True)
    pm_games = load_pm_games(args.sched)
    print(f"  {len(pm_games)} PM games with win markets", flush=True)

    # Scan each sport
    signals = []
    matched_events = 0
    total_odds = 0

    for sport in sports:
        print(f"\n[{sport}]", flush=True)
        odds_events = get_pinnacle_probs(key, sport)
        total_odds += len(odds_events)
        if not odds_events:
            continue
        print(f"  {len(odds_events)} odds events ({odds_events[0]['source']})", flush=True)

        for oe in odds_events:
            # Find matching PM game
            for pm in pm_games:
                if not match_event(oe["home"], oe["away"], oe["commence"],
                                   pm["teams"], pm["start_time"], pm["slug"]):
                    continue

                matched_events += 1

                # Compare each win market
                for wm in pm["win_markets"]:
                    # Find Pinnacle prob for this team
                    pin_prob = None
                    for pname, pprob in oe["probs"].items():
                        if teams_match(wm["team"], pname):
                            pin_prob = pprob
                            break
                    if pin_prob is None:
                        continue

                    # Get PM ask price
                    pm_ask, depth = get_best_ask(wm["yes_token"])
                    edge = (pin_prob - pm_ask) * 100  # in cents

                    if edge >= args.min_edge:
                        sig = {
                            "slug": pm["slug"],
                            "team": wm["team"],
                            "pm_ask": round(pm_ask, 3),
                            "pinnacle": round(pin_prob, 3),
                            "edge_cents": round(edge, 1),
                            "depth_shares": round(depth, 0),
                            "source": oe["source"],
                            "odds_home": oe["home"],
                            "odds_away": oe["away"],
                            "cid": wm["cid"],
                            "token_id": wm["yes_token"],
                        }
                        signals.append(sig)
                        print(f"  >>> EDGE {edge:+.1f}¢ | {wm['team']} | PM={pm_ask*100:.1f}¢ Pinnacle={pin_prob*100:.1f}% | {pm['slug']}", flush=True)
                    elif edge > 0:
                        print(f"      small {edge:+.1f}¢ | {wm['team']} | PM={pm_ask*100:.1f}¢ Pinnacle={pin_prob*100:.1f}% | {pm['slug']}", flush=True)
                break  # only match first PM game per odds event

    # Summary
    print(f"\n{'='*60}", flush=True)
    print(f"Odds events scanned: {total_odds}", flush=True)
    print(f"PM matches found: {matched_events}", flush=True)
    print(f"Signals (>={args.min_edge}¢ edge): {len(signals)}", flush=True)

    if signals:
        signals.sort(key=lambda s: -s["edge_cents"])
        print(f"\n{'Team':<35s} {'PM':>6s} {'Sharp':>6s} {'Edge':>6s} {'Slug'}", flush=True)
        print("-" * 85, flush=True)
        for s in signals:
            print(f"{s['team'][:34]:<35s} {s['pm_ask']*100:>5.1f}¢ {s['pinnacle']*100:>5.1f}% {s['edge_cents']:>+5.1f}¢ {s['slug'][:30]}", flush=True)

    # Save
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "min_edge": args.min_edge,
        "odds_events": total_odds,
        "pm_matches": matched_events,
        "signals": signals,
    }
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "pinnacle_scan.json")
    json.dump(out, open(out_path, "w"), indent=2)
    print(f"\nSaved to {out_path}", flush=True)


if __name__ == "__main__":
    main()
