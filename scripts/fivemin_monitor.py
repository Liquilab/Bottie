#!/usr/bin/env python3
"""
5-Minute Crypto Candle Orderbook Monitor

Monitort alle actieve 5m/15m crypto up/down markets.
Scant orderbooks 60s voor window einde op 1-3c asks.
Logt beschikbare liquidity per kant (Up/Down).

Doel: bepalen of er genoeg 1-3c asks overblijven
na 0x7da07b (de "liquidity harvester").

Usage:
    python3 fivemin_monitor.py
"""

import json, os, re, time, logging, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HARVESTER = "0x7da07b2a8b009a406198677debda46ad651b6be2"

DATA_DIR = Path("/opt/bottie/data/fivemin_monitor")
LOG_FILE = DATA_DIR / "orderbook_scans.jsonl"
SUMMARY_FILE = DATA_DIR / "summary.json"

SCAN_INTERVAL = 10  # seconds between scans
MAX_PRICE = 0.03  # track asks up to 3c

COINS = ["bitcoin", "ethereum", "solana", "xrp", "dogecoin"]
COIN_SHORT = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "xrp": "XRP", "dogecoin": "DOGE",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fivemin_monitor")


def fetch(url: str, timeout: int = 10) -> dict | list:
    req = urllib.request.Request(
        url, headers={"User-Agent": "5mMon/1", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ═══════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════


def parse_window(title: str) -> dict | None:
    """Parse a 5m/15m up/down market title into window info."""
    t = title.lower()
    if "up or down" not in t:
        return None

    # Extract coin
    coin = None
    for c in COINS:
        if c in t:
            coin = COIN_SHORT[c]
            break
    if not coin:
        return None

    # Extract date + time window
    m = re.search(
        r"(january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\s+(\d+),\s+(\d+):(\d+)(am|pm)-(\d+):(\d+)(am|pm)",
        t,
    )
    if not m:
        return None

    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month = months[m.group(1)]
    day = int(m.group(2))
    sh, sm, sap = int(m.group(3)), int(m.group(4)), m.group(5)
    eh, em, eap = int(m.group(6)), int(m.group(7)), m.group(8)

    if sap == "pm" and sh != 12: sh += 12
    if sap == "am" and sh == 12: sh = 0
    if eap == "pm" and eh != 12: eh += 12
    if eap == "am" and eh == 12: eh = 0

    dur = ((eh * 60 + em) - (sh * 60 + sm)) % 1440

    # Only 5m and 15m
    if dur not in (5, 15):
        return None

    try:
        et_tz = timezone(timedelta(hours=-4))
        start_et = datetime(2026, month, day, sh, sm, tzinfo=et_tz)
        end_et = start_et + timedelta(minutes=dur)
        return {
            "coin": coin,
            "duration": dur,
            "start_utc": start_et.astimezone(timezone.utc),
            "end_utc": end_et.astimezone(timezone.utc),
            "title": title,
        }
    except (ValueError, OverflowError):
        return None


def discover_markets() -> list[dict]:
    """Find active 5m/15m crypto up/down markets via harvester activity."""
    markets = []
    seen_cids = set()

    # Method 1: Check harvester's recent activity for conditionIds
    try:
        activity = fetch(f"{DATA_API}/activity?user={HARVESTER}&limit=100")
        for a in activity:
            cid = a.get("conditionId", "")
            title = a.get("title", "")
            if not cid or cid in seen_cids:
                continue
            window = parse_window(title)
            if not window:
                continue
            seen_cids.add(cid)
            markets.append({**window, "condition_id": cid})
    except Exception as e:
        log.warning(f"Harvester activity fetch failed: {e}")

    # Method 2: Gamma API for active events
    try:
        events = fetch(
            f"{GAMMA}/events?active=true&closed=false&limit=200"
            f"&order=volume24hr&ascending=false"
        )
        for event in events:
            for market in event.get("markets", []):
                q = market.get("question", "")
                cid = market.get("conditionId", "")
                if not cid or cid in seen_cids:
                    continue
                window = parse_window(q)
                if not window:
                    continue
                seen_cids.add(cid)
                markets.append({**window, "condition_id": cid})
    except Exception as e:
        log.warning(f"Gamma fetch failed: {e}")

    return markets


# ═══════════════════════════════════════════════════════════════
# ORDERBOOK SCANNER
# ═══════════════════════════════════════════════════════════════


def scan_orderbook(condition_id: str) -> dict | None:
    """Scan orderbook for cheap asks on both sides."""
    try:
        mkt = fetch(f"{CLOB}/markets/{condition_id}")
    except Exception:
        return None

    if mkt.get("closed") or not mkt.get("active"):
        return None

    result = {}
    for tok in mkt.get("tokens", []):
        outcome = tok.get("outcome", "")
        token_id = tok.get("token_id", "")
        if not token_id:
            continue

        try:
            book = fetch(f"{CLOB}/book?token_id={token_id}")
        except Exception:
            result[outcome] = {"asks_le3c": 0, "shares_le3c": 0, "best_ask": None, "asks": []}
            continue

        asks = book.get("asks", [])
        cheap = [a for a in asks if float(a.get("price", 1)) <= MAX_PRICE]
        cheap_shares = sum(float(a.get("size", 0)) for a in cheap)
        best = float(asks[0]["price"]) if asks else None

        # Detail per price level
        by_price = defaultdict(float)
        for a in cheap:
            cents = round(float(a["price"]) * 100)
            by_price[cents] += float(a.get("size", 0))

        result[outcome] = {
            "asks_le3c": len(cheap),
            "shares_le3c": cheap_shares,
            "best_ask": best,
            "asks_by_cent": dict(by_price),
        }

    return result


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("5-Minute Orderbook Monitor starting")
    log.info(f"Tracking: {', '.join(COINS)}")
    log.info(f"Max price: {MAX_PRICE*100:.0f}c")
    log.info(f"Scan interval: {SCAN_INTERVAL}s")
    log.info("=" * 60)

    # Stats
    total_scans = 0
    total_opportunities = 0
    summary = {
        "started": datetime.now(timezone.utc).isoformat(),
        "scans": 0,
        "opportunities": 0,
        "by_coin": {},
    }

    known_markets: dict[str, dict] = {}  # cid -> market info
    last_discovery = 0

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Discover markets every 60s
            if time.time() - last_discovery > 60:
                new_markets = discover_markets()
                for m in new_markets:
                    cid = m["condition_id"]
                    if cid not in known_markets:
                        known_markets[cid] = m
                        log.info(
                            f"DISCOVERED {m['coin']} {m['duration']}m "
                            f"ends {m['end_utc'].strftime('%H:%M:%S')} UTC | {m['title'][:50]}"
                        )
                last_discovery = time.time()

                # Prune expired markets (ended >5min ago)
                expired = [
                    cid for cid, m in known_markets.items()
                    if m["end_utc"] < now - timedelta(minutes=5)
                ]
                for cid in expired:
                    del known_markets[cid]

            # Scan markets that are ending within 90 seconds
            for cid, market in list(known_markets.items()):
                secs_to_end = (market["end_utc"] - now).total_seconds()

                # Scan window: 90s before end to 30s after end
                if not (-30 <= secs_to_end <= 90):
                    continue

                book = scan_orderbook(cid)
                if not book:
                    continue

                total_scans += 1

                # Check for opportunities (cheap asks on EITHER side)
                up_data = book.get("Up", book.get("Yes", {}))
                down_data = book.get("Down", book.get("No", {}))

                up_shares = up_data.get("shares_le3c", 0)
                down_shares = down_data.get("shares_le3c", 0)
                up_best = up_data.get("best_ask")
                down_best = down_data.get("best_ask")

                has_up = up_shares > 0
                has_down = down_shares > 0
                has_both = has_up and has_down

                # Estimate cost and potential profit
                up_cost = sum(
                    float(cents) / 100 * shares
                    for cents, shares in up_data.get("asks_by_cent", {}).items()
                )
                down_cost = sum(
                    float(cents) / 100 * shares
                    for cents, shares in down_data.get("asks_by_cent", {}).items()
                )

                # Potential payout = min(up_shares, down_shares) × $1
                # (guaranteed one side wins)
                pair_shares = min(up_shares, down_shares)
                potential_profit = pair_shares - up_cost - down_cost if has_both else 0

                record = {
                    "timestamp": now.isoformat(),
                    "coin": market["coin"],
                    "duration": market["duration"],
                    "secs_to_end": round(secs_to_end),
                    "title": market["title"][:60],
                    "up_shares_le3c": round(up_shares),
                    "down_shares_le3c": round(down_shares),
                    "up_best_ask": up_best,
                    "down_best_ask": down_best,
                    "up_asks_by_cent": up_data.get("asks_by_cent", {}),
                    "down_asks_by_cent": down_data.get("asks_by_cent", {}),
                    "up_cost": round(up_cost, 2),
                    "down_cost": round(down_cost, 2),
                    "has_both_sides": has_both,
                    "pair_shares": round(pair_shares),
                    "potential_profit": round(potential_profit, 2),
                }

                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps(record) + "\n")

                if has_both and potential_profit > 0:
                    total_opportunities += 1
                    log.info(
                        f"💰 OPPORTUNITY {market['coin']} {market['duration']}m "
                        f"T-{secs_to_end:.0f}s | "
                        f"Up: {up_shares:.0f}sh @{up_best:.3f} (${up_cost:.1f}) | "
                        f"Down: {down_shares:.0f}sh @{down_best:.3f} (${down_cost:.1f}) | "
                        f"Pair: {pair_shares:.0f}sh → ${potential_profit:.0f} profit"
                    )
                elif has_up or has_down:
                    side = "Up" if has_up else "Down"
                    shares = up_shares if has_up else down_shares
                    best = up_best if has_up else down_best
                    log.info(
                        f"📊 SINGLE {market['coin']} {market['duration']}m "
                        f"T-{secs_to_end:.0f}s | {side}: {shares:.0f}sh @{best:.3f}"
                    )
                else:
                    log.debug(
                        f"   EMPTY {market['coin']} {market['duration']}m "
                        f"T-{secs_to_end:.0f}s | no ≤3c asks"
                    )

            # Update summary every 5 min
            if total_scans % 30 == 0 and total_scans > 0:
                summary["scans"] = total_scans
                summary["opportunities"] = total_opportunities
                summary["last_updated"] = now.isoformat()

                # Compute stats from log
                if LOG_FILE.exists():
                    by_coin = defaultdict(lambda: {
                        "scans": 0, "opportunities": 0,
                        "avg_up_shares": 0, "avg_down_shares": 0,
                        "total_potential": 0,
                    })
                    lines = LOG_FILE.read_text().strip().split("\n")
                    for line in lines[-500:]:  # last 500 scans
                        try:
                            r = json.loads(line)
                            c = r["coin"]
                            by_coin[c]["scans"] += 1
                            by_coin[c]["avg_up_shares"] += r.get("up_shares_le3c", 0)
                            by_coin[c]["avg_down_shares"] += r.get("down_shares_le3c", 0)
                            if r.get("has_both_sides") and r.get("potential_profit", 0) > 0:
                                by_coin[c]["opportunities"] += 1
                                by_coin[c]["total_potential"] += r["potential_profit"]
                        except (json.JSONDecodeError, KeyError):
                            pass

                    for c in by_coin:
                        n = by_coin[c]["scans"]
                        if n > 0:
                            by_coin[c]["avg_up_shares"] = round(by_coin[c]["avg_up_shares"] / n)
                            by_coin[c]["avg_down_shares"] = round(by_coin[c]["avg_down_shares"] / n)
                    summary["by_coin"] = dict(by_coin)

                SUMMARY_FILE.write_text(json.dumps(summary, indent=2))
                log.info(
                    f"STATS: {total_scans} scans | {total_opportunities} opportunities | "
                    f"tracking {len(known_markets)} markets"
                )

        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
