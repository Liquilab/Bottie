#!/usr/bin/env python3
"""
Crypto Milestone Dry-Run Logger — Fase 1 Validatie

Track 1: Copy justdance milestone + 4h sweet spot trades
Track 2: Monitor hourly "above" markets independently

Draait op VPS naast Bottie. Geen orders, alleen logging.
Meet slippage en track resoluties.

Usage:
    python3 crypto_dryrun.py              # dry-run (default)
    python3 crypto_dryrun.py --backfill   # backfill missed signals from API
"""

import json, os, sys, time, re, logging, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

JUSTDANCE = "0xcc500cbcc8b7cf5bd21975ebbea34f21b5644c82"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

POLL_INTERVAL = 30  # seconds
DATA_DIR = Path("/opt/bottie/data/crypto_dryrun")
LOG_FILE = DATA_DIR / "signals.jsonl"
STATS_FILE = DATA_DIR / "stats.json"

# Sweet spot filters
MILESTONE_MAX_ENTRY = 0.35  # reach/hit YES
DIP_MAX_ENTRY = 0.20  # dip YES
UPDOWN_4H_ENABLED = True

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("crypto_dryrun")


def fetch(url: str, timeout: int = 15) -> list | dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "CryptoDryRun/1", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ═══════════════════════════════════════════════════════════════
# SIGNAL CLASSIFICATION
# ═══════════════════════════════════════════════════════════════


def classify_market(title: str) -> dict | None:
    """Classify a market title into actionable categories.

    Returns dict with keys: type, timeframe, coin, direction
    or None if not interesting.
    """
    t = title.lower()

    # Coin detection
    coin = None
    for c, keywords in [
        ("BTC", ["bitcoin", "btc"]),
        ("ETH", ["ethereum", "eth "]),
        ("SOL", ["solana", "sol "]),
        ("XRP", ["xrp"]),
        ("DOGE", ["dogecoin", "doge"]),
        ("BNB", ["bnb"]),
        ("HYPE", ["hype"]),
    ]:
        if any(k in t for k in keywords):
            coin = c
            break
    if not coin:
        return None

    # Milestone: "Will BTC reach/hit $X" or "Will BTC dip to $X"
    if "reach" in t or "hit" in t:
        # Determine timeframe
        tf = _extract_timeframe(t)
        return {"type": "milestone_reach", "timeframe": tf, "coin": coin, "direction": "bullish"}

    if "dip to" in t or "dip below" in t:
        tf = _extract_timeframe(t)
        return {"type": "milestone_dip", "timeframe": tf, "coin": coin, "direction": "bearish"}

    # "Above" daily markets: "Will the price of Bitcoin be above $72,400 on April 16?"
    if "above" in t:
        tf = _extract_timeframe(t)
        if tf == "other":
            tf = "daily"  # "above" markets are typically daily
        return {"type": "daily_above", "timeframe": tf, "coin": coin, "direction": "bullish"}

    # Up/Down markets
    if "up or down" in t:
        # Detect duration
        m = re.search(r"(\d+:\d+[AP]M)-(\d+:\d+[AP]M)", title, re.IGNORECASE)
        if m:
            dur = _calc_duration(m.group(1), m.group(2))
            if dur == 240:
                return {"type": "updown_4h", "timeframe": "4h", "coin": coin, "direction": None}
            elif dur == 60:
                return {"type": "updown_1h", "timeframe": "1h", "coin": coin, "direction": None}
            elif dur <= 15:
                return None  # Skip 5m/15m — no edge
        # Check for "1 hr" label
        if "1 hr" in t or "1 hour" in t:
            return {"type": "updown_1h", "timeframe": "1h", "coin": coin, "direction": None}

    return None


def _extract_timeframe(title: str) -> str:
    """Extract resolution timeframe from milestone title."""
    t = title.lower()
    months = "january|february|march|april|may|june|july|august|september|october|november|december"

    # Daily: "on April 16"
    if re.search(rf"on ({months}) \d+", t) or "today" in t:
        return "daily"
    # Weekly: "April 13-19"
    if re.search(r"\d+-\d+", t):
        return "weekly"
    # Monthly: "in April"
    if re.search(rf"in ({months})", t):
        return "monthly"
    # Yearly: "by December 31, 2026"
    if re.search(r"by ({months})", t.replace("by ", "by ")):
        return "yearly"
    return "other"


def _calc_duration(start: str, end: str) -> int:
    """Calculate duration in minutes between two time strings like '4:00AM' and '8:00AM'."""
    def to_mins(s):
        m = re.match(r"(\d+):(\d+)([AP]M)", s, re.IGNORECASE)
        if not m:
            return 0
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ap == "PM" and h != 12:
            h += 12
        if ap == "AM" and h == 12:
            h = 0
        return h * 60 + mi

    return (to_mins(end) - to_mins(start)) % 1440


def is_sweet_spot(title: str, outcome: str, price: float, classification: dict) -> bool:
    """Check if a trade matches our sweet spot filter."""
    if not classification:
        return False

    typ = classification["type"]
    outcome_l = outcome.lower()

    # Milestone reach/hit: YES @ <35c
    if typ == "milestone_reach":
        if outcome_l in ("yes", "up", "") and price <= MILESTONE_MAX_ENTRY:
            # Skip daily — 52% WR noise
            if classification["timeframe"] == "daily":
                return False
            return True

    # Milestone dip: YES @ <20c
    if typ == "milestone_dip":
        if outcome_l in ("yes", "up", "") and price <= DIP_MAX_ENTRY:
            if classification["timeframe"] == "daily":
                return False
            return True

    # Daily "above" markets: YES @ <35c
    if typ == "daily_above":
        if outcome_l in ("yes", "") and price <= MILESTONE_MAX_ENTRY:
            return True

    # 4H up/down: all entries (we track to measure edge)
    if typ == "updown_4h" and UPDOWN_4H_ENABLED:
        return True

    # 1H up/down: all entries (track 2 — experimental)
    if typ == "updown_1h":
        return True

    return False


# ═══════════════════════════════════════════════════════════════
# TRACK 1: JUSTDANCE COPY SIGNALS
# ═══════════════════════════════════════════════════════════════


class JustdanceTracker:
    """Poll justdance's trades and log sweet spot signals."""

    def __init__(self):
        self.seen_txs: set[str] = set()
        self._load_seen()

    def _load_seen(self):
        """Load previously seen transaction hashes."""
        if LOG_FILE.exists():
            with open(LOG_FILE) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if tx := rec.get("tx_hash"):
                            self.seen_txs.add(tx)
                    except json.JSONDecodeError:
                        pass
        log.info(f"Loaded {len(self.seen_txs)} seen transactions")

    def poll(self) -> list[dict]:
        """Poll justdance's recent trades and return new sweet spot signals."""
        signals = []
        try:
            trades = fetch(f"{DATA_API}/trades?user={JUSTDANCE}&side=BUY&limit=50")
        except Exception as e:
            log.warning(f"Failed to poll justdance trades: {e}")
            return signals

        for t in trades:
            tx_hash = t.get("transactionHash", "")
            if tx_hash in self.seen_txs:
                continue
            self.seen_txs.add(tx_hash)

            title = t.get("title", "")
            outcome = t.get("outcome", "")
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
            ts = int(t.get("timestamp", 0))

            classification = classify_market(title)
            if not classification:
                continue

            sweet = is_sweet_spot(title, outcome, price, classification)
            if not sweet:
                continue

            signal = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "justdance",
                "track": 1,
                "tx_hash": tx_hash,
                "title": title,
                "outcome": outcome,
                "jd_price": price,
                "jd_size": size,
                "jd_usdc": round(size * price, 2),
                "jd_timestamp": ts,
                "event_slug": t.get("eventSlug", ""),
                "condition_id": t.get("conditionId", ""),
                "asset": t.get("asset", ""),
                "classification": classification,
                "our_price": None,  # filled by slippage check
                "slippage": None,
                "resolution": None,  # filled when market resolves
                "resolved_at": None,
                "pnl_simulated": None,
            }

            # Measure slippage: what would WE pay right now?
            signal["our_price"] = self._get_current_price(
                t.get("conditionId", ""), outcome
            )
            if signal["our_price"] is not None:
                signal["slippage"] = round(signal["our_price"] - price, 4)

            signals.append(signal)
            log.info(
                f"SIGNAL [T1 justdance] {classification['type']} {classification['timeframe']} "
                f"{title[:50]} | jd@{price:.3f} us@{signal['our_price'] or '?'} "
                f"slip={signal['slippage'] or '?'}"
            )

        return signals

    def _get_current_price(self, condition_id: str, outcome: str) -> float | None:
        """Get current best ask for this outcome via CLOB."""
        if not condition_id:
            return None
        try:
            market = fetch(f"{CLOB_API}/markets/{condition_id}")
            tokens = market.get("tokens", [])
            target_token = None
            for tok in tokens:
                if tok.get("outcome", "").lower() == outcome.lower():
                    target_token = tok.get("token_id")
                    break
            if not target_token and tokens:
                target_token = tokens[0].get("token_id")
            if not target_token:
                return None

            book = fetch(f"{CLOB_API}/book?token_id={target_token}")
            asks = book.get("asks", [])
            if asks:
                return float(asks[0].get("price", 0))
        except Exception as e:
            log.debug(f"Price check failed for {condition_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# TRACK 2: HOURLY MARKET SCANNER
# ═══════════════════════════════════════════════════════════════


class HourlyScanner:
    """Scan for daily 'above' + milestone crypto markets via gamma API."""

    # Slug patterns for crypto milestone/above events
    SLUG_KEYWORDS = [
        "bitcoin-above", "ethereum-above", "solana-above",
        "xrp-above", "dogecoin-above", "bnb-above",
        "what-price-will-bitcoin", "what-price-will-ethereum",
        "what-price-will-solana",
        "bitcoin-up-or-down", "ethereum-up-or-down",
        "solana-up-or-down", "xrp-up-or-down",
    ]

    def __init__(self):
        self.seen_conditions: set[str] = set()

    def scan(self) -> list[dict]:
        """Scan gamma API for active crypto above/milestone markets."""
        signals = []
        try:
            events = fetch(
                f"{GAMMA_API}/events?active=true&closed=false&limit=100"
                f"&order=volume24hr&ascending=false"
            )
        except Exception as e:
            log.warning(f"Failed to scan markets: {e}")
            return signals

        for event in events:
            slug = event.get("slug", "")
            title = event.get("title", "")

            # Filter: only crypto milestone/above/updown events
            is_crypto = any(kw in slug for kw in self.SLUG_KEYWORDS)
            if not is_crypto:
                # Also match by title keywords
                t = title.lower()
                is_crypto = any(k in t for k in [
                    "bitcoin above", "ethereum above", "solana above",
                    "price will bitcoin", "price will ethereum",
                    "up or down", "bitcoin reach", "bitcoin dip",
                    "ethereum reach", "solana reach",
                ])
            if not is_crypto:
                continue

            for market in event.get("markets", []):
                question = market.get("question", "")
                cid = market.get("conditionId", "")

                if cid in self.seen_conditions:
                    continue

                m_class = classify_market(question)
                if not m_class:
                    continue

                # Check price via outcomePrices from gamma (faster than CLOB)
                prices_str = market.get("outcomePrices", "")
                best_ask = None
                try:
                    if prices_str:
                        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                        if prices and len(prices) > 0:
                            best_ask = float(prices[0])  # Yes/Up price
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass

                # Only signal cheap markets that match sweet spot
                if best_ask is None or best_ask > MILESTONE_MAX_ENTRY:
                    continue

                self.seen_conditions.add(cid)

                signal = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "scanner",
                    "track": 2,
                    "tx_hash": None,
                    "title": question,
                    "outcome": "Yes",
                    "jd_price": None,
                    "jd_size": None,
                    "jd_usdc": None,
                    "jd_timestamp": None,
                    "event_slug": slug,
                    "condition_id": cid,
                    "asset": None,
                    "classification": m_class,
                    "our_price": best_ask,
                    "slippage": None,
                    "resolution": None,
                    "resolved_at": None,
                    "pnl_simulated": None,
                }
                signals.append(signal)
                log.info(
                    f"SIGNAL [T2 scanner] {m_class['type']} {m_class['timeframe']} "
                    f"{m_class['coin']} @{best_ask:.3f} {question[:50]}"
                )

        return signals


# ═══════════════════════════════════════════════════════════════
# RESOLUTION TRACKER
# ═══════════════════════════════════════════════════════════════


def check_resolutions():
    """Check if any tracked signals have resolved."""
    if not LOG_FILE.exists():
        return

    lines = LOG_FILE.read_text().strip().split("\n")
    updated = False

    new_lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue

        if rec.get("resolution") is not None:
            new_lines.append(line)
            continue

        cid = rec.get("condition_id", "")
        if not cid:
            new_lines.append(line)
            continue

        try:
            market = fetch(f"{CLOB_API}/markets/{cid}")
            if market.get("closed") or market.get("resolved"):
                winner = ""
                for tok in market.get("tokens", []):
                    if tok.get("winner"):
                        winner = tok.get("outcome", "")
                        break
                outcome = rec.get("outcome", "").lower()
                won = winner.lower() == outcome if winner else None

                rec["resolution"] = "win" if won else "loss" if won is False else "unknown"
                rec["resolved_at"] = datetime.now(timezone.utc).isoformat()

                # Simulate PnL
                entry = rec.get("our_price") or rec.get("jd_price") or 0.20
                sim_bet = 75  # $75 simulated bet
                shares = sim_bet / entry if entry > 0 else 0
                if won:
                    rec["pnl_simulated"] = round(shares * (1.0 - entry), 2)
                elif won is False:
                    rec["pnl_simulated"] = round(-sim_bet, 2)

                updated = True
                log.info(
                    f"RESOLVED [{rec['source']}] {rec['resolution']} "
                    f"sim_pnl=${rec['pnl_simulated']:.0f} {rec['title'][:50]}"
                )
        except Exception:
            pass

        new_lines.append(json.dumps(rec))

    if updated:
        LOG_FILE.write_text("\n".join(new_lines) + "\n")
        _update_stats()


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════


def _update_stats():
    """Recompute stats from signals log."""
    if not LOG_FILE.exists():
        return

    stats = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "track1": {"total": 0, "resolved": 0, "wins": 0, "losses": 0, "pnl": 0, "avg_slippage": 0},
        "track2": {"total": 0, "resolved": 0, "wins": 0, "losses": 0, "pnl": 0},
        "by_type": {},
        "by_timeframe": {},
    }

    slippages = []
    with open(LOG_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            track = f"track{rec.get('track', 1)}"
            stats[track]["total"] += 1

            if rec.get("slippage") is not None:
                slippages.append(rec["slippage"])

            if rec.get("resolution"):
                stats[track]["resolved"] += 1
                if rec["resolution"] == "win":
                    stats[track]["wins"] += 1
                elif rec["resolution"] == "loss":
                    stats[track]["losses"] += 1
                if rec.get("pnl_simulated"):
                    stats[track]["pnl"] += rec["pnl_simulated"]

            # By type
            typ = rec.get("classification", {}).get("type", "unknown")
            if typ not in stats["by_type"]:
                stats["by_type"][typ] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0}
            stats["by_type"][typ]["total"] += 1
            if rec.get("resolution") == "win":
                stats["by_type"][typ]["wins"] += 1
                stats["by_type"][typ]["pnl"] += rec.get("pnl_simulated", 0)
            elif rec.get("resolution") == "loss":
                stats["by_type"][typ]["losses"] += 1
                stats["by_type"][typ]["pnl"] += rec.get("pnl_simulated", 0)

            # By timeframe
            tf = rec.get("classification", {}).get("timeframe", "unknown")
            if tf not in stats["by_timeframe"]:
                stats["by_timeframe"][tf] = {"total": 0, "wins": 0, "losses": 0, "pnl": 0}
            stats["by_timeframe"][tf]["total"] += 1
            if rec.get("resolution") == "win":
                stats["by_timeframe"][tf]["wins"] += 1
                stats["by_timeframe"][tf]["pnl"] += rec.get("pnl_simulated", 0)
            elif rec.get("resolution") == "loss":
                stats["by_timeframe"][tf]["losses"] += 1
                stats["by_timeframe"][tf]["pnl"] += rec.get("pnl_simulated", 0)

    if slippages:
        stats["track1"]["avg_slippage"] = round(sum(slippages) / len(slippages), 4)

    STATS_FILE.write_text(json.dumps(stats, indent=2))
    log.info(
        f"STATS T1: {stats['track1']['total']} signals, "
        f"{stats['track1']['wins']}W/{stats['track1']['losses']}L, "
        f"pnl=${stats['track1']['pnl']:.0f}, "
        f"avg_slip={stats['track1']['avg_slippage']:.3f} | "
        f"T2: {stats['track2']['total']} signals"
    )


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════


def append_signals(signals: list[dict]):
    """Append signals to JSONL log."""
    if not signals:
        return
    with open(LOG_FILE, "a") as f:
        for s in signals:
            f.write(json.dumps(s) + "\n")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("Crypto Dry-Run Logger starting")
    log.info(f"Track 1: justdance copy (milestone + 4h)")
    log.info(f"Track 2: hourly market scanner (1h + above)")
    log.info(f"Poll interval: {POLL_INTERVAL}s")
    log.info(f"Data dir: {DATA_DIR}")
    log.info("=" * 60)

    jd_tracker = JustdanceTracker()
    hourly_scanner = HourlyScanner()

    cycle = 0
    while True:
        try:
            # Track 1: justdance
            signals = jd_tracker.poll()
            append_signals(signals)

            # Track 2: hourly scanner (every 5 minutes, not every 30s)
            if cycle % 10 == 0:
                hourly_signals = hourly_scanner.scan()
                append_signals(hourly_signals)

            # Check resolutions (every 2 minutes)
            if cycle % 4 == 0:
                check_resolutions()

            # Stats (every 10 minutes)
            if cycle % 20 == 0:
                _update_stats()

        except KeyboardInterrupt:
            log.info("Shutting down")
            _update_stats()
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")

        cycle += 1
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        log.info("Backfilling from justdance trade history...")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tracker = JustdanceTracker()
        signals = tracker.poll()
        append_signals(signals)
        _update_stats()
        log.info(f"Backfilled {len(signals)} signals")
    else:
        main()
