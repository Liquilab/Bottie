#!/usr/bin/env python3
"""Whale Consensus Bot — buys opponent No on football games where top-100
holder consensus >= 55%. Flat 2.5% bankroll per game.

Runs as standalone service alongside the Rust bot (which handles resolution).
"""
import json, os, sys, time, urllib.request, logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

import dotenv
dotenv.load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────
SUBGRAPH_URL = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions-subgraph/0.0.7/gn"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

MIN_CONSENSUS_PCT = 55
TOP_N_HOLDERS = 100
MIN_TRADERS = 10
SIZING_PCT = 2.5
T1_MINUTES = 3          # buy window: 0..3 min before kickoff
POLL_INTERVAL = 20       # seconds between polls
SCHEDULE_REFRESH = 1800  # refresh schedule every 30 min
QUERY_DELAY = 0.12       # seconds between subgraph queries

PK = os.environ["PRIVATE_KEY"]
FUNDER = os.environ["FUNDER_ADDRESS"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
TRADES_FILE = DATA_DIR / "consensus_trades.jsonl"
BOARD_FILE  = DATA_DIR / "consensus_board.json"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

US_SPORTS = {"nba", "nhl", "mlb", "nfl", "cbb", "ncaa", "mls"}
US_SPORTS_TEST = set()  # disabled — subgraph has 0 holders for NBA/NHL
US_SPORTS_BET = 5.0
NBA_TRACK = {"nba"}  # log odds + outcomes without buying
NBA_LOG_FILE = DATA_DIR / "nba_underdog_tracker.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("consensus")

# ── CLOB Client ─────────────────────────────────────────────────────────
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY

clob = ClobClient(
    host=CLOB_HOST,
    chain_id=137,
    key=PK,
    signature_type=2,
    funder=FUNDER,
)
clob.set_api_creds(clob.create_or_derive_api_creds())

# ── State ───────────────────────────────────────────────────────────────
bought_slugs = set()   # event_slugs already bought this session
schedule = []          # list of UpcomingGame dicts
last_schedule_refresh = 0

# ── Helpers ─────────────────────────────────────────────────────────────
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ConsensusBot/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def http_post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "ConsensusBot/1"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def get_bankroll():
    """Get USDC balance from PM wallet."""
    try:
        val = http_get("https://data-api.polymarket.com/value?user=%s" % FUNDER)
        if val:
            return float(val[0].get("value", 0))
    except:
        pass
    return 0

# ── Schedule ────────────────────────────────────────────────────────────
def refresh_game_schedule():
    """Fetch upcoming sports events from Gamma API."""
    global schedule, last_schedule_refresh
    now = datetime.now(timezone.utc)

    games = []
    sport_tags = ["soccer_epl", "soccer_bundesliga", "soccer_la_liga", "soccer_serie_a",
                  "soccer_ligue_1", "soccer_eredivisie", "soccer_primeira_liga",
                  "soccer_championship", "soccer_super_lig", "soccer_segunda_division",
                  "soccer_2_bundesliga", "soccer_scottish_premiership", "soccer_liga_mx",
                  "soccer_a_league", "soccer_ligue_2", "soccer_primera_division_argentina",
                  "soccer_serie_a_brazil", "soccer_mls", "soccer_europa_league",
                  "soccer_champions_league", "soccer_fa_cup", "soccer"]

    seen = set()
    for tag in sport_tags:
        try:
            events = http_get("%s/events?tag=%s&closed=false&limit=100" % (GAMMA_URL, tag))
            for e in events:
                slug = e.get("slug", "")
                if slug in seen or not slug:
                    continue
                seen.add(slug)

                # Parse markets: only win markets
                markets = []
                for m in e.get("markets", []):
                    q = m.get("question", "")
                    ql = q.lower()
                    # Football: "Will X win?" / NBA: "Team A vs Team B"
                    is_moneyline = "win" in ql or " vs. " in ql or " vs " in ql
                    is_excluded = "draw" in ql or "o/u" in ql or "both" in ql or "spread" in ql or "rebound" in ql or "assist" in ql or "point" in ql
                    if not is_moneyline or is_excluded:
                        continue
                    cid = m.get("conditionId", "")
                    tokens_raw = m.get("clobTokenIds", "")
                    try:
                        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else (tokens_raw or [])
                    except:
                        tokens = []
                    if cid and len(tokens) >= 2:
                        markets.append({
                            "cid": cid,
                            "question": q,
                            "yes_token": tokens[0],
                            "no_token": tokens[1],
                        })

                if len(markets) >= 1:  # NBA=1 moneyline, football=2+ win markets
                    # Parse start date
                    start_str = e.get("startDate", "")
                    try:
                        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    except:
                        continue

                    sport = slug.split("-")[0]
                    if sport in US_SPORTS and sport not in US_SPORTS_TEST:
                        continue

                    games.append({
                        "slug": slug,
                        "sport": sport,
                        "start": start,
                        "markets": markets,
                        "title": e.get("title", ""),
                    })
        except Exception as ex:
            log.warning("schedule fetch error for %s: %s", tag, str(ex)[:60])

    # Supplement from Rust bot schedule cache (has more games)
    seen = set(g["slug"] for g in games)
    cache_file = DATA_DIR / "schedule_cache.json"
    if cache_file.exists():
        try:
            sched = json.loads(cache_file.read_text())
            now_dt = datetime.now(timezone.utc)
            for g in sched:
                slug = g.get("event_slug","")
                sport = slug.split("-")[0]
                start = g.get("start_time","")
                if sport in US_SPORTS: continue
                # Skip variants
                skip = False
                for suffix in ["-more-markets","-halftime","-exact-score","-player-props","-total-corners"]:
                    if suffix in slug:
                        skip = True
                        break
                if skip: continue
                if slug in seen: continue
                try:
                    kt = datetime.fromisoformat(start.replace("Z","+00:00"))
                    if kt < now_dt - timedelta(hours=1): continue
                    if kt > now_dt + timedelta(hours=24): continue
                except: continue
                # We don't have market details from cache, but we can get them from Gamma
                # For now add with empty markets — they'll be filled when T-1 triggers
                # Actually we need condition_ids to query subgraph. Skip if no markets.
                # Try to get markets from Gamma for this specific event
                try:
                    ev_data = http_get("%s/events?slug=%s" % (GAMMA_URL, slug))
                    if ev_data:
                        markets = []
                        for m in ev_data[0].get("markets",[]):
                            q = m.get("question","")
                            ql = q.lower()
                            if "win" not in ql or "draw" in ql or "o/u" in ql or "both" in ql or "spread" in ql:
                                continue
                            cid = m.get("conditionId","")
                            tokens_raw = m.get("clobTokenIds","")
                            try:
                                tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else (tokens_raw or [])
                            except:
                                tokens = []
                            if cid and len(tokens) >= 2:
                                markets.append({"cid": cid, "question": q, "yes_token": tokens[0], "no_token": tokens[1]})
                        if len(markets) >= 1:  # NBA=1 moneyline, football=2+ win markets
                            games.append({"slug": slug, "sport": sport, "start": kt, "markets": markets, "title": ev_data[0].get("title","")})
                            seen.add(slug)
                except:
                    pass
                time.sleep(0.05)
        except Exception as ex:
            log.warning("schedule cache supplement error: %s", str(ex)[:60])

    schedule = games
    last_schedule_refresh = time.time()
    log.info("SCHED: %d football games loaded", len(schedule))

# ── Subgraph ────────────────────────────────────────────────────────────
def query_top_holders(cid, n=100):
    query = '{ userBalances(first: %d, orderBy: balance, orderDirection: desc, where: { asset_: { condition: "%s" } }) { user balance asset { outcomeIndex } } }' % (n, cid)
    try:
        resp = http_post_json(SUBGRAPH_URL, {"query": query})
        holders = resp.get("data", {}).get("userBalances", [])
        return [{
            "user": h["user"],
            "balance": int(h.get("balance", 0)) / 1e6,
            "oi": int(h.get("asset", {}).get("outcomeIndex", "0")),
        } for h in holders]
    except Exception as ex:
        log.warning("subgraph error for %s: %s", cid[:16], str(ex)[:60])
        return []

# ── Consensus ───────────────────────────────────────────────────────────
def calculate_consensus(game):
    """Calculate whale consensus for a football game.
    Returns (buy_market, consensus_pct, n_traders) or None."""

    # Fetch top holders per win market
    all_holders = {}
    for m in game["markets"]:
        holders = query_top_holders(m["cid"], TOP_N_HOLDERS)
        all_holders[m["cid"]] = holders
        time.sleep(QUERY_DELAY)

    # Per leg (cid + side): count unique traders + total shares
    legs = defaultdict(lambda: {"traders": set(), "shares": 0})
    all_traders = set()

    for cid, holders in all_holders.items():
        for h in holders:
            side = "Yes" if h["oi"] == 0 else "No"
            key = (cid, side)
            d = legs[key]
            if h["user"] not in d["traders"]:
                d["traders"].add(h["user"])
                d["shares"] += h["balance"]
            all_traders.add(h["user"])

    n_total = len(all_traders)
    if n_total < MIN_TRADERS:
        log.info("SKIP %s — %d traders < %d minimum", game["slug"], n_total, MIN_TRADERS)
        return None

    # Find leg with most shares (weighted consensus)
    best_key = max(legs, key=lambda k: legs[k]["shares"])
    best = legs[best_key]
    best_cid, best_side = best_key
    consensus_pct = 100 * len(best["traders"]) / n_total

    if consensus_pct < MIN_CONSENSUS_PCT:
        log.info("SKIP %s — %.0f%% < %d%% (%d traders)",
                 game["slug"], consensus_pct, MIN_CONSENSUS_PCT, n_total)
        return None

    # Find the market to buy
    best_market = next(m for m in game["markets"] if m["cid"] == best_cid)
    sport = game.get("sport","")
    is_us = sport in US_SPORTS_TEST

    if is_us:
        # NBA/NHL: 1 market, 2 outcomes. Buy the consensus side directly.
        # outcomeIndex 0 = yes_token (Team A), 1 = no_token (Team B)
        if best_side == "Yes":
            buy_token = best_market["yes_token"]  # Team A
        else:
            buy_token = best_market["no_token"]   # Team B
        buy_market = best_market
    else:
        # Football: 2+ markets. Buy opponent No (wins on win + draw).
        if best_side == "Yes":
            opp_markets = [m for m in game["markets"] if m["cid"] != best_cid]
            if not opp_markets:
                log.info("SKIP %s — no opponent market found", game["slug"])
                return None
            buy_market = opp_markets[0]
            buy_token = buy_market["no_token"]
        else:
            buy_market = best_market
            buy_token = best_market["no_token"]

    return {
        "buy_market": buy_market,
        "buy_token": buy_token,
        "consensus_pct": consensus_pct,
        "n_traders": n_total,
        "consensus_shares": best["shares"],
        "best_side": best_side,
        "best_question": best_market["question"],
    }

# ── Execution ───────────────────────────────────────────────────────────
def execute_buy(game, consensus):
    """Place a taker buy order for the consensus leg."""
    token_id = consensus["buy_token"]
    question = consensus["buy_market"]["question"]

    # Get best ask from orderbook — try No token first, fallback to Yes token
    best_ask = None
    buy_token_final = token_id
    try:
        book = clob.get_order_book(token_id)
        if book.asks:
            best_ask = float(book.asks[0].price)
    except Exception as ex:
        log.warning("orderbook error %s: %s", game["slug"], str(ex)[:60])

    # If No token is illiquid (ask >= 0.95), try Yes token of same market
    if best_ask is None or best_ask >= 0.95:
        yes_token = consensus["buy_market"]["yes_token"]
        try:
            book_yes = clob.get_order_book(yes_token)
            if book_yes.bids:
                yes_bid = float(book_yes.bids[0].price)
                if 0.05 < yes_bid < 0.95:
                    # Buy No at implied price = 1 - yes_bid
                    best_ask = round(1 - yes_bid, 2)
                    log.info("FALLBACK %s — No token illiquid, using Yes bid %.2f → No price %.2f",
                             game["slug"], yes_bid, best_ask)
        except: pass

    if best_ask is None or best_ask >= 0.95 or best_ask <= 0.05:
        log.info("SKIP %s — ask %.2f out of range", game["slug"], best_ask if best_ask else 0)
        return False

    # Size: $5 flat for NBA/NHL test, 2.5% bankroll for football
    sport = game.get("sport","")
    if sport in US_SPORTS_TEST:
        bet_usdc = US_SPORTS_BET
    else:
        bankroll = get_bankroll()
        if bankroll < 50:
            log.warning("SKIP %s — bankroll $%.0f too low", game["slug"], bankroll)
            return False
        bet_usdc = bankroll * SIZING_PCT / 100
        if bet_usdc < 2.50:
            log.info("SKIP %s — bet $%.2f below minimum", game["slug"], bet_usdc)
            return False

    shares = round(bet_usdc / best_ask, 2)

    log.info("BUY %s No @ %.0f¢ | $%.2f (%.1f%%) | consensus %.0f%% (%d traders) | %s",
             game["slug"], best_ask * 100, bet_usdc, SIZING_PCT,
             consensus["consensus_pct"], consensus["n_traders"],
             question[:50])

    if DRY_RUN:
        log.info("DRY RUN — order not placed")
        return True

    try:
        order = clob.create_and_post_order(OrderArgs(
            token_id=token_id,
            price=best_ask,
            size=shares,
            side=BUY,
        ))
        log.info("ORDER: %s", json.dumps(order)[:200] if isinstance(order, dict) else str(order)[:200])
    except Exception as ex:
        log.error("ORDER FAILED %s: %s", game["slug"], str(ex)[:100])
        return False

    # Log trade
    trade = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_slug": game["slug"],
        "sport": game["sport"],
        "market_title": question,
        "outcome": "No",
        "side": "BUY",
        "price": best_ask,
        "size_usdc": bet_usdc,
        "size_shares": shares,
        "consensus_pct": consensus["consensus_pct"],
        "consensus_traders": consensus["n_traders"],
        "consensus_shares": consensus["consensus_shares"],
        "signal_source": "whale_consensus",
        "dry_run": DRY_RUN,
    }
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(trade) + "\n")

    return {"price": best_ask, "size_usdc": bet_usdc}

# ── Board (for dashboard) ───────────────────────────────────────────────
board_data = {}  # slug -> board entry

def write_board():
    """Write consensus_board.json for the dashboard."""
    now = datetime.now(timezone.utc)
    entries = []
    for game in schedule:
        slug = game["slug"]
        until = (game["start"] - now).total_seconds() / 60
        if until < -180:  # skip games that ended >3h ago
            continue
        entry = board_data.get(slug, {
            "slug": slug,
            "sport": game["sport"],
            "title": game["title"],
            "kickoff": game["start"].isoformat(),
            "status": "PENDING",
            "consensus_pct": 0,
            "n_traders": 0,
            "buy_question": "",
            "buy_side": "",
            "bought": False,
            "price": 0,
            "size_usdc": 0,
        })
        entry["until_min"] = round(until, 1)
        entries.append(entry)

    entries.sort(key=lambda e: e.get("kickoff", ""))
    try:
        with open(BOARD_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception as ex:
        log.warning("board write error: %s", str(ex)[:60])

def update_board_consensus(game, consensus):
    """Update board with consensus result."""
    board_data[game["slug"]] = {
        "slug": game["slug"],
        "sport": game["sport"],
        "title": game["title"],
        "kickoff": game["start"].isoformat(),
        "status": "BUY" if consensus else "SKIP",
        "consensus_pct": consensus["consensus_pct"] if consensus else 0,
        "n_traders": consensus["n_traders"] if consensus else 0,
        "buy_question": consensus["buy_market"]["question"] if consensus else "",
        "buy_side": "No" if consensus else "",
        "bought": False,
        "price": 0,
        "size_usdc": 0,
    }

def update_board_bought(slug, price, size_usdc):
    """Update board entry after successful buy."""
    if slug in board_data:
        board_data[slug]["bought"] = True
        board_data[slug]["price"] = price
        board_data[slug]["size_usdc"] = size_usdc

# ── Main Loop ───────────────────────────────────────────────────────────
def main():
    log.info("Whale Consensus Bot starting | min_consensus=%d%% | sizing=%.1f%% | dry_run=%s",
             MIN_CONSENSUS_PCT, SIZING_PCT, DRY_RUN)
    log.info("Funder: %s", FUNDER)

    # Load previously bought slugs from trades file
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            for line in f:
                try:
                    t = json.loads(line.strip())
                    bought_slugs.add(t.get("event_slug", ""))
                except:
                    pass
        log.info("Loaded %d previous trades", len(bought_slugs))

    while True:
        try:
            # Refresh schedule if stale
            if time.time() - last_schedule_refresh > SCHEDULE_REFRESH:
                refresh_game_schedule()
                write_board()

            now = datetime.now(timezone.utc)

            # ── Pre-game consensus logger (T-30): log consensus for all games ──
            SCOUT_FILE = DATA_DIR / "consensus_scout.jsonl"
            scouted = set()
            if SCOUT_FILE.exists():
                for line in SCOUT_FILE.read_text().splitlines():
                    try: scouted.add(json.loads(line).get("slug",""))
                    except: pass

            for game in schedule:
                until = (game["start"] - now).total_seconds() / 60
                if until < 0 or until > 30: continue  # 0-30 min before kickoff
                if until < T1_MINUTES: continue  # leave T-1 window for buying
                if game["slug"] in scouted: continue
                if game["sport"] in US_SPORTS and game["sport"] not in US_SPORTS_TEST: continue
                if len(game["markets"]) < 1: continue

                # Query consensus (same as buying logic)
                consensus = calculate_consensus(game)
                scout_entry = {
                    "timestamp": now.isoformat(),
                    "slug": game["slug"],
                    "sport": game["sport"],
                    "title": game.get("title",""),
                    "kickoff": game["start"].isoformat(),
                    "consensus_pct": consensus["consensus_pct"] if consensus else 0,
                    "n_traders": consensus["n_traders"] if consensus else 0,
                    "consensus_shares": consensus["consensus_shares"] if consensus else 0,
                    "best_side": consensus["best_side"] if consensus else "",
                    "best_question": consensus["best_question"] if consensus else "",
                    "result": None,  # filled after resolution
                }
                with open(SCOUT_FILE, "a") as f:
                    f.write(json.dumps(scout_entry) + "\n")
                scouted.add(game["slug"])
                if consensus:
                    log.info("SCOUT: %s — %.0f%% (%d traders) %s %s",
                             game["slug"], consensus["consensus_pct"], consensus["n_traders"],
                             consensus["best_side"], consensus["best_question"][:30])
                    # Alert for strong signals
                    if consensus["consensus_pct"] >= 70:
                        log.info("🚨 STRONG SIGNAL: %s — %.0f%% consensus (%d traders)",
                                 game["slug"], consensus["consensus_pct"], consensus["n_traders"])
                else:
                    log.info("SCOUT: %s — no consensus", game["slug"])

            # Find games in T-1 window
            for game in schedule:
                until = (game["start"] - now).total_seconds() / 60  # minutes until kickoff
                if until < 0 or until > T1_MINUTES:
                    continue

                slug = game["slug"]
                if slug in bought_slugs:
                    continue

                log.info("T1 WINDOW: %s — %.1f min to kickoff (%d markets)",
                         slug, until, len(game["markets"]))

                # Calculate consensus
                consensus = calculate_consensus(game)
                update_board_consensus(game, consensus)
                write_board()
                if consensus is None:
                    bought_slugs.add(slug)  # don't retry
                    continue

                log.info("CONSENSUS: %s — %.0f%% (%d/%d traders, %.0f shares) → %s No",
                         slug, consensus["consensus_pct"],
                         consensus["n_traders"], consensus["n_traders"],
                         consensus["consensus_shares"],
                         consensus["buy_market"]["question"][:40])

                # Execute
                result = execute_buy(game, consensus)
                if result:
                    update_board_bought(slug, result["price"], result["size_usdc"])
                    write_board()
                    bought_slugs.add(slug)
                else:
                    bought_slugs.add(slug)  # don't retry on failure either

                time.sleep(2)  # pause between orders

            # ── NBA underdog tracker (log only, no buying) ──
            nba_tracked = set()
            if NBA_LOG_FILE.exists():
                for line in NBA_LOG_FILE.read_text().splitlines():
                    try: nba_tracked.add(json.loads(line).get("slug",""))
                    except: pass

            for game in schedule:
                if game["sport"] not in NBA_TRACK: continue
                until = (game["start"] - now).total_seconds() / 60
                if until < 0 or until > T1_MINUTES: continue
                if game["slug"] in nba_tracked: continue

                # Get orderbook prices for each outcome
                for m in game["markets"]:
                    try:
                        book = clob.get_order_book(m["yes_token"])
                        yes_bid = float(book.bids[0].price) if book.bids else 0
                        book2 = clob.get_order_book(m["no_token"])
                        no_bid = float(book2.bids[0].price) if book2.bids else 0
                    except:
                        continue

                    if yes_bid <= 0 or no_bid <= 0: continue

                    dog_side = "yes" if yes_bid < no_bid else "no"
                    dog_price = min(yes_bid, no_bid)
                    fav_price = max(yes_bid, no_bid)

                    entry = {
                        "timestamp": now.isoformat(),
                        "slug": game["slug"],
                        "sport": game["sport"],
                        "question": m["question"],
                        "dog_side": dog_side,
                        "dog_price": dog_price,
                        "fav_price": fav_price,
                        "result": None,  # filled later by resolver
                    }
                    with open(NBA_LOG_FILE, "a") as f:
                        f.write(json.dumps(entry) + "\n")
                    nba_tracked.add(game["slug"])
                    log.info("NBA TRACK: %s — dog=%s @ %.0f¢, fav @ %.0f¢",
                             game["slug"], dog_side, dog_price*100, fav_price*100)
                    break  # 1 moneyline per game

        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as ex:
            log.error("Loop error: %s", str(ex)[:200])

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
