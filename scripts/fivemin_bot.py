#!/usr/bin/env python3
"""
5-Minute Crypto Candle Bot — Limit Order Strategy

Plaatst limit BUY orders @1c op BEIDE kanten (Up + Down) van 5m crypto candles.
Als de candle beweegt vult één kant. Bij reversal = 100x winst.
Als beide kanten vullen = gegarandeerde winst.

Draait op Crypto 5M wallet (bottie-test .env).

Usage:
    python3 fivemin_bot.py                # live mode (5 shares @1c)
    python3 fivemin_bot.py --dry-run      # log only, no orders
    python3 fivemin_bot.py --shares 10    # custom share size
"""

import json, os, sys, re, time, signal, logging, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Sizing: fixed USD per side, split across 3 tiers
BET_USD_PER_SIDE = 20.00  # 2026-04-17 12:19 user bumped 40->20 (Binance filter live, kleinere bet meer windows)
MIN_SHARES = 5  # PM minimum

# 3-tier bid ladder — catcht shallow crashes (2-3c) en deep crashes (1c)
# Empirisch (HARVESTER, 246 markets): ≤1c WR 3.2%, ≤2c 6.2%, ≤3c 12% — alle profitable
TIERS = [
    (0.01, 0.35),  # 35% op 1c — ROI-king (+239% ROI in HARVESTER 5h data); 5% naar 4c experiment
    (0.02, 0.20),  # 20% op 2c — zwakste WR (1.2%) maar nog steeds +146% ROI
    (0.03, 0.40),  # 40% op 3c — hoogste WR (7.4%), frequency-tier (HARVESTER weegt 3c net zo zwaar als 1c)
    (0.04, 0.05),  # 5% op 4c — 2026-04-17 experiment, break-even WR=4%, HARVESTER plaatst hier 0 trades (N=3495) dus geen prior
]
BANKROLL_PCT = None  # deprecated — use BET_USD_PER_SIDE

# Funder address for on-chain balance check
FUNDER = None  # set from env

# BTC only for now
COINS = {
    "bitcoin": "BTC",
}

DATA_DIR = Path("/opt/bottie-test/data/fivemin_bot")
LOG_FILE = DATA_DIR / "trades.jsonl"
STATS_FILE = DATA_DIR / "stats.json"

# Timing
DISCOVERY_INTERVAL = 30  # discover new markets every 30s
WINDOW_LEAD_TIME = 10  # place orders 10s after window opens
CANCEL_AFTER_END = 15  # cancel unfilled orders 15s after window ends
LOOP_INTERVAL = 5  # main loop interval

# Binance momentum skip-filter (2026-04-17):
# Skip windows where |10-min BTC move| > threshold. Raw 72h data: 5 wins all had
# m600 <= 0.125%, 66/177 losses had m600 > 0.15% -> skip saves ~$2,640/72h.
# Rationale: sterke 10-min momentum = losende kant van ladder fills gegarandeerd,
# orderbook reverseert niet in 5 min.
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=11"
SKIP_MOVE_10M_THRESHOLD = 0.0015  # 0.15%
BINANCE_TIMEOUT = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fivemin_bot")

DRY_RUN = "--dry-run" in sys.argv


# ═══════════════════════════════════════════════════════════════
# CLOB CLIENT
# ═══════════════════════════════════════════════════════════════


def init_clob():
    """Initialize CLOB client with Crypto 5M wallet credentials."""
    from py_clob_client.client import ClobClient

    global FUNDER
    FUNDER = os.environ["FUNDER_ADDRESS"]
    client = ClobClient(
        CLOB_URL,
        key=os.environ["PRIVATE_KEY"],
        chain_id=137,
        funder=FUNDER,
        signature_type=2,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    log.info(f"CLOB client initialized | funder={FUNDER[:10]}...")
    return client


def fetch(url: str, timeout: int = 10):
    req = urllib.request.Request(
        url, headers={"User-Agent": "5mBot/1", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ═══════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════


@dataclass
class Window:
    coin: str
    condition_id: str
    tokens: dict  # {"Up": token_id, "Down": token_id}
    start_utc: datetime
    end_utc: datetime
    title: str
    orders_placed: bool = False
    order_ids: dict = None  # {"Up": order_id, "Down": order_id}
    fills: dict = None  # {"Up": shares_filled, "Down": shares_filled}
    resolved: bool = False

    def __post_init__(self):
        if self.order_ids is None:
            self.order_ids = {}
        if self.fills is None:
            self.fills = {}


def parse_window_time(title: str) -> tuple[datetime, datetime, str] | None:
    """Parse title to get window start/end UTC and coin."""
    t = title.lower()
    if "up or down" not in t:
        return None

    coin = None
    for keyword, short in COINS.items():
        if keyword in t:
            coin = short
            break
    if not coin:
        return None

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
    sh, sm = int(m.group(3)), int(m.group(4))
    sap = m.group(5)
    eh, em = int(m.group(6)), int(m.group(7))
    eap = m.group(8)

    if sap == "pm" and sh != 12: sh += 12
    if sap == "am" and sh == 12: sh = 0
    if eap == "pm" and eh != 12: eh += 12
    if eap == "am" and eh == 12: eh = 0

    dur = ((eh * 60 + em) - (sh * 60 + sm)) % 1440
    if dur != 5:  # only 5-minute windows
        return None

    try:
        et_tz = timezone(timedelta(hours=-4))
        start = datetime(2026, month, day, sh, sm, tzinfo=et_tz).astimezone(timezone.utc)
        end = start + timedelta(minutes=5)
        return start, end, coin
    except (ValueError, OverflowError):
        return None


COIN_SLUGS = {
    "BTC": "btc-updown-5m",
}


def discover_windows(client) -> list[Window]:
    """Generate 5m window slugs and fetch from gamma/CLOB."""
    windows = []
    now = datetime.now(timezone.utc)

    # Current and next 5m window timestamps
    current_min = (now.minute // 5) * 5
    current_start = now.replace(minute=current_min, second=0, microsecond=0)
    timestamps = [
        int(current_start.timestamp()),
        int((current_start + timedelta(minutes=5)).timestamp()),
    ]

    for coin, slug_prefix in COIN_SLUGS.items():
        for ts in timestamps:
            slug = f"{slug_prefix}-{ts}"
            start = datetime.fromtimestamp(ts, tz=timezone.utc)
            end = start + timedelta(minutes=5)

            try:
                data = fetch(f"{GAMMA_URL}/markets?slug={slug}")
                if not data:
                    continue
                market = data[0] if isinstance(data, list) else data
                cid = market.get("conditionId", "")
                if not cid:
                    continue

                # Get token IDs from CLOB
                mkt = fetch(f"{CLOB_URL}/markets/{cid}")
                if mkt.get("closed"):
                    continue
                tokens = {}
                for tok in mkt.get("tokens", []):
                    outcome = tok.get("outcome", "")
                    tokens[outcome] = tok.get("token_id", "")
                if len(tokens) < 2:
                    continue

                title = market.get("question", f"{coin} Up or Down 5m")
                windows.append(Window(
                    coin=coin,
                    condition_id=cid,
                    tokens=tokens,
                    start_utc=start,
                    end_utc=end,
                    title=title,
                ))
            except Exception:
                continue

    return windows


# ═══════════════════════════════════════════════════════════════
# ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════


def get_bankroll() -> float:
    """Get on-chain USDC balance for sizing."""
    try:
        key = os.environ.get("POLYGONSCAN_API_KEY", "")
        funder = FUNDER or os.environ.get("FUNDER_ADDRESS", "")
        usdc_pos = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        url = (
            f"https://api.etherscan.io/v2/api?chainid=137"
            f"&module=account&action=tokenbalance"
            f"&contractaddress={usdc_pos}&address={funder}"
            f"&tag=latest&apikey={key}"
        )
        data = fetch(url)
        return int(data.get("result", 0)) / 1e6
    except Exception as e:
        log.warning(f"Bankroll check failed: {e}")
        return 0



def should_skip_binance_momentum():
    """Check Binance BTCUSDT last 10 1-min bars. Skip if |move| > threshold.
    Returns (skip: bool, move_pct: float). Fails open on error.
    """
    try:
        req = urllib.request.Request(
            BINANCE_KLINES_URL,
            headers={"User-Agent": "Bottie-fivemin/1"},
        )
        bars = json.loads(urllib.request.urlopen(req, timeout=BINANCE_TIMEOUT).read())
        if not bars or len(bars) < 11:
            return False, 0.0
        open_10m_ago = float(bars[0][1])
        close_now = float(bars[-2][4])
        move = (close_now - open_10m_ago) / open_10m_ago
        return abs(move) > SKIP_MOVE_10M_THRESHOLD, move * 100
    except Exception as e:
        log.debug(f"Binance skip check failed (fail-open): {e}")
        return False, 0.0


def place_orders(client, window: Window, shares: int) -> bool:
    """Place 3-tier limit BUY orders on both sides. Fixed-dollar sizing."""
    from py_clob_client.clob_types import OrderArgs

    bankroll = get_bankroll()
    budget_per_side = BET_USD_PER_SIDE

    # Safety cap: if bankroll is tiny, don't spend more than 5% per window
    if bankroll > 0 and budget_per_side > bankroll * 0.05:
        budget_per_side = bankroll * 0.05
        log.warning(f"  CAPPED — bankroll ${bankroll:.2f} low, budget/side reduced to ${budget_per_side:.2f}")

    if budget_per_side < MIN_SHARES * 0.01:
        log.warning(f"  SKIP — bankroll ${bankroll:.2f} too low (need ${MIN_SHARES * 0.01:.2f}/side min)")
        return False

    for outcome, token_id in window.tokens.items():
        if not token_id:
            continue

        for price, pct in TIERS:
            tier_budget = budget_per_side * pct
            tier_shares = max(MIN_SHARES, int(tier_budget / price))
            tier_cost = tier_shares * price

            try:
                order_args = OrderArgs(
                    price=price,
                    size=tier_shares,
                    side="BUY",
                    token_id=token_id,
                )

                if DRY_RUN:
                    log.info(
                        f"  DRY-RUN: BUY {outcome} @{price:.2f} "
                        f"{tier_shares}sh (${tier_cost:.2f})"
                    )
                    window.order_ids[f"{outcome}_{price}"] = f"dry-{outcome}-{price}"
                    continue

                signed = client.create_order(order_args)
                resp = client.post_order(signed, "GTC")
                order_id = resp.get("orderID", "")
                matched = float(resp.get("size_matched", 0))

                window.order_ids[f"{outcome}_{price}"] = order_id

                if matched > 0:
                    prev = window.fills.get(outcome, 0)
                    window.fills[outcome] = prev + matched
                    log.info(
                        f"  INSTANT FILL {outcome} {matched:.0f}sh "
                        f"@{price:.2f} | oid={order_id[:12]}"
                    )
                else:
                    log.info(
                        f"  ORDER {outcome} {tier_shares}sh @{price:.2f} "
                        f"(${tier_cost:.2f}) | oid={order_id[:12]}"
                    )
            except Exception as e:
                log.error(f"  ORDER FAILED {outcome} @{price}: {e}")

    window.orders_placed = True
    log.info(
        f"  TOTAL: 6 orders (3 tiers × 2 sides) | "
        f"bankroll=${bankroll:.2f} budget/side=${budget_per_side:.2f} (fixed)"
    )
    return True


def check_both_sides_filled(window: Window):
    """Detect when BOTH Up and Down filled — locks in guaranteed ~$1/pair at resolution.

    On-chain merge (to cash out before resolution) requires Safe proxy execution
    (signature_type=2), not yet implemented. At resolution the winning side
    redeems to $1 automatically, so we just hold and log the locked-in profit.
    """
    up = float(window.fills.get("Up", 0) or 0)
    dn = float(window.fills.get("Down", 0) or 0)
    if up < MIN_SHARES or dn < MIN_SHARES:
        return
    if window.fills.get("_arb_logged"):
        return

    pairs = min(up, dn)
    # Cost approximation: shares on each side × avg fill price (tiered so ~1-2c)
    # Exact cost is tracked in order_ids but approximated here as 2 × 0.015 × pairs
    # Profit = $1 per pair (winning side) - cost of both legs
    approx_cost = pairs * 0.015 * 2  # 2 legs, avg tier price ~1.5c
    approx_profit = pairs * 1.0 - approx_cost

    log.info(
        f"  🔒 BOTH SIDES FILLED {window.coin}: {up:.0f}sh Up + {dn:.0f}sh Down = "
        f"{pairs:.0f} arb-pairs | approx locked profit ${approx_profit:.2f} at resolution"
    )
    log.info(
        f"     (on-chain merge-before-resolution: not implemented for proxy wallet; "
        f"waiting for auto-redemption at window close)"
    )
    window.fills["_arb_logged"] = True


def _cancel_ok(resp, order_id: str) -> bool:
    """True if PM CLOB cancel response confirms the order is no longer active.

    Response shape: {"canceled": [oid, ...], "not_canceled": {oid: reason, ...}}.
    A reason like "order can't be found - already canceled or matched" means the
    order is already gone from PM's books — that's the outcome we wanted, so treat
    it as success. Without this, cancel_orders retries forever on the same oids.
    """
    if not isinstance(resp, dict):
        return False
    canceled = resp.get("canceled") or []
    if order_id in canceled:
        return True
    not_canceled = resp.get("not_canceled") or {}
    reason = not_canceled.get(order_id)
    if reason is None:
        return False
    r = str(reason).lower()
    if ("already canceled" in r or "already matched" in r or "matched" in r
            or "can't be found" in r or "not found" in r):
        return True
    return False


def cancel_unfilled_tiers(client, window: Window, prices: list, tag: str):
    """Cancel unfilled orders for specific price tiers (both sides).

    Empirisch (HARVESTER data 2026-04-17, N=1773 BTC 5M fills over 5u):
    edge zit volledig in T-30..T-15 bucket (1c T-20..T-15: 75% WR, +3051% ROI).
    Fills na T-15 zijn 0% WR op alle tiers. Cancel T-15 vs geen cancel = +$1,336 over 5u.
    """
    sentinel = f"_tier_cancelled_{tag}"
    if window.order_ids.get(sentinel):
        return
    cancelled = 0
    failed = 0
    for key, order_id in list(window.order_ids.items()):
        if not isinstance(order_id, str) or order_id.startswith("dry-"):
            continue
        if key.endswith("_filled") or key.startswith("_"):
            continue
        parts = key.split("_")
        if len(parts) != 2:
            continue
        try:
            price = float(parts[1])
        except ValueError:
            continue
        if not any(abs(price - p) < 0.0005 for p in prices):
            continue
        try:
            if DRY_RUN:
                cancelled += 1
            else:
                resp = client.cancel(order_id)
                if _cancel_ok(resp, order_id):
                    cancelled += 1
                else:
                    failed += 1
                    log.warning(f"  ⚠️  tier-cancel {key} oid={order_id[:12]} resp={resp}")
        except Exception as e:
            failed += 1
            log.warning(f"  ⚠️  tier-cancel {key} EXC: {e}")
    window.order_ids[sentinel] = True
    if cancelled or failed:
        tiers_str = "/".join(f"{int(p*100)}c" for p in prices)
        suffix = f" ({failed} FAILED)" if failed else ""
        log.info(f"  ⏱️  CANCEL {tiers_str} @ {tag}: {cancelled} unfilled orders ({window.coin}){suffix}")


def cancel_opposite_side(client, window: Window, filled_outcome: str):
    """Cancel all still-open GTC orders on the OPPOSITE side of the filled outcome.

    Rationale: once one side fills, we've taken a directional position. The
    opposite-side bids are unlikely to fill in the remaining window time, and
    leaving them open ties up balance that could be used for other windows.
    """
    # Determine opposite outcome name (Up ↔ Down)
    opposite = "Down" if filled_outcome.lower() == "up" else "Up"

    sentinel = f"_cancelled_{opposite}"
    if window.order_ids.get(sentinel):
        return  # already cancelled this side

    cancelled = 0
    failed = 0
    failed_ids = []
    for key, order_id in list(window.order_ids.items()):
        if not isinstance(order_id, str) or order_id.startswith("dry-"):
            continue
        if key.endswith("_filled") or key.startswith("_"):
            continue
        if not key.startswith(f"{opposite}_"):
            continue
        try:
            if DRY_RUN:
                cancelled += 1
            else:
                resp = client.cancel(order_id)
                if _cancel_ok(resp, order_id):
                    cancelled += 1
                else:
                    failed += 1
                    failed_ids.append((key, order_id, resp))
                    log.warning(f"  ⚠️  cancel-opposite {key} oid={order_id[:12]} resp={resp}")
        except Exception as e:
            failed += 1
            log.warning(f"  ⚠️  cancel-opposite {key} EXC: {e}")

    # Only set sentinel if all cancels confirmed — retry next loop if any failed
    if failed == 0:
        window.order_ids[sentinel] = True
    if cancelled or failed:
        suffix = f" — {failed} FAILED, will retry next loop" if failed else ""
        log.info(
            f"  ❎ CANCEL OPPOSITE {opposite}: {cancelled} orders "
            f"(filled={filled_outcome}, freeing balance){suffix}"
        )


def check_fills(client, window: Window):
    """Check if any orders have been filled. Cancel opposite-side orders on first fill."""
    new_fill_outcomes = set()

    # list() snapshot — we mutate window.order_ids inside the loop when a fill is detected.
    # Without this, dict mutation during iteration raises RuntimeError and the subsequent
    # cancel_opposite_side call never runs.
    for key, order_id in list(window.order_ids.items()):
        if not order_id or (isinstance(order_id, str) and order_id.startswith("dry-")):
            continue
        if key.endswith("_filled") or key.startswith("_"):
            continue

        try:
            order = client.get_order(order_id)
            filled = float(order.get("size_matched", 0))
            if filled > 0 and f"{key}_filled" not in window.order_ids:
                window.order_ids[f"{key}_filled"] = True
                # key format: "Up_0.01" → outcome = "Up"
                outcome = key.split("_")[0]
                price = key.split("_")[1] if "_" in key else "0.01"
                prev = window.fills.get(outcome, 0)
                window.fills[outcome] = prev + filled
                new_fill_outcomes.add(outcome)
                log.info(
                    f"  FILL {window.coin} {outcome} {filled:.0f}sh "
                    f"@{price}"
                )
        except Exception:
            pass

    # After detecting fills, cancel opposite-side orders for any newly-filled outcome
    for outcome in new_fill_outcomes:
        cancel_opposite_side(client, window, outcome)


def try_sell(client, window: Window):
    """Check prices of filled positions and sell if above threshold."""
    if getattr(window, '_sold', False):
        return

    for outcome, filled_shares in list(window.fills.items()):
        if f"{outcome}_sold" in window.fills:
            continue  # already sold this side

        token_id = window.tokens.get(outcome, "")
        if not token_id:
            continue

        try:
            book = fetch(f"{CLOB_URL}/book?token_id={token_id}")
            bids = book.get("bids", [])
            if not bids:
                continue

            best_bid = float(bids[0].get("price", 0))
            bid_size = float(bids[0].get("size", 0))

            if best_bid >= SELL_THRESHOLD:
                # Sell at best bid
                from py_clob_client.clob_types import OrderArgs

                sell_shares = min(filled_shares, bid_size)
                order_args = OrderArgs(
                    price=best_bid,
                    size=sell_shares,
                    side="SELL",
                    token_id=token_id,
                )

                if DRY_RUN:
                    log.info(
                        f"  DRY-RUN SELL {window.coin} {outcome} "
                        f"{sell_shares:.0f}sh @{best_bid:.2f} "
                        f"= ${sell_shares * best_bid:.2f} "
                        f"(profit ${sell_shares * (best_bid - LIMIT_PRICE):.2f})"
                    )
                    window.fills[f"{outcome}_sold"] = sell_shares
                    return

                signed = client.create_order(order_args)
                resp = client.post_order(signed, "FOK")
                matched = float(resp.get("size_matched", 0))

                if matched > 0:
                    profit = matched * (best_bid - LIMIT_PRICE)
                    cost = matched * LIMIT_PRICE
                    pct = (best_bid / LIMIT_PRICE - 1) * 100

                    log.info(
                        f"  💰 SOLD {window.coin} {outcome} "
                        f"{matched:.0f}sh @{best_bid:.2f} "
                        f"= ${matched * best_bid:.2f} "
                        f"(+${profit:.2f}, +{pct:.0f}%)"
                    )
                    window.fills[f"{outcome}_sold"] = matched
                    window._sold = True

                    # Log trade
                    record = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "sell",
                        "coin": window.coin,
                        "title": window.title,
                        "outcome": outcome,
                        "buy_price": LIMIT_PRICE,
                        "sell_price": best_bid,
                        "shares": matched,
                        "cost": round(cost, 4),
                        "revenue": round(matched * best_bid, 4),
                        "profit": round(profit, 4),
                        "pct": round(pct, 1),
                    }
                    with open(LOG_FILE, "a") as f:
                        f.write(json.dumps(record) + "\n")
                else:
                    log.info(
                        f"  SELL MISSED {window.coin} {outcome} "
                        f"@{best_bid:.2f} — no fill"
                    )
        except Exception as e:
            log.debug(f"  Sell check failed {outcome}: {e}")


def cancel_orders(client, window: Window):
    """Cancel any unfilled orders. Only tries once if all confirmed; retries on failure."""
    if getattr(window, '_cancelled', False):
        return

    cancelled = 0
    failed = 0
    for key, order_id in list(window.order_ids.items()):
        if not isinstance(order_id, str) or order_id.startswith("dry-"):
            continue
        if key.endswith("_filled") or key.startswith("_"):
            continue

        try:
            if DRY_RUN:
                cancelled += 1
            else:
                resp = client.cancel(order_id)
                if _cancel_ok(resp, order_id):
                    cancelled += 1
                else:
                    failed += 1
                    log.warning(f"  ⚠️  post-window cancel {key} oid={order_id[:12]} resp={resp}")
        except Exception as e:
            failed += 1
            log.warning(f"  ⚠️  post-window cancel {key} EXC: {e}")

    # Only mark window as cleaned up if everything confirmed cancelled
    if failed == 0:
        window._cancelled = True
    if cancelled or failed:
        suffix = f" ({failed} FAILED, retry)" if failed else ""
        log.info(f"  CANCEL {window.coin} {cancelled} unfilled orders{suffix}")


# ═══════════════════════════════════════════════════════════════
# RESOLUTION TRACKING
# ═══════════════════════════════════════════════════════════════


def check_resolution(window: Window):
    """Check if market resolved and calculate PnL."""
    if not window.fills:
        return

    try:
        mkt = fetch(f"{CLOB_URL}/markets/{window.condition_id}")
        if not mkt.get("closed"):
            return

        winner = None
        for tok in mkt.get("tokens", []):
            if tok.get("winner"):
                winner = tok.get("outcome", "")
                break

        if not winner:
            return

        window.resolved = True

        # Calculate PnL
        total_pnl = 0
        details = []
        for outcome, filled in window.fills.items():
            cost = filled * LIMIT_PRICE
            if outcome == winner:
                pnl = filled * (1.0 - LIMIT_PRICE)
                details.append(f"{outcome} WIN +${pnl:.2f}")
            else:
                pnl = -cost
                details.append(f"{outcome} LOSS -${cost:.2f}")
            total_pnl += pnl

        result = "WIN" if total_pnl > 0 else "LOSS"
        log.info(
            f"  RESOLVED {window.coin} {window.title[:40]} → {winner} | "
            f"{' | '.join(details)} | NET ${total_pnl:+.2f}"
        )

        # Log to file
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin": window.coin,
            "title": window.title,
            "condition_id": window.condition_id,
            "winner": winner,
            "fills": window.fills,
            "pnl": round(total_pnl, 4),
            "result": result,
            "cost": round(sum(f * LIMIT_PRICE for f in window.fills.values()), 4),
        }
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

    except Exception as e:
        log.debug(f"Resolution check failed: {e}")


# ═══════════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════════


def update_stats():
    """Compute running stats from trade log."""
    if not LOG_FILE.exists():
        return

    wins = losses = total_pnl = total_cost = events = 0
    by_coin = {}

    for line in LOG_FILE.read_text().strip().split("\n"):
        try:
            r = json.loads(line)
            events += 1
            total_pnl += r.get("pnl", 0)
            total_cost += r.get("cost", 0)
            if r["result"] == "WIN":
                wins += 1
            else:
                losses += 1

            coin = r["coin"]
            if coin not in by_coin:
                by_coin[coin] = {"w": 0, "l": 0, "pnl": 0}
            by_coin[coin]["pnl"] += r.get("pnl", 0)
            if r["result"] == "WIN":
                by_coin[coin]["w"] += 1
            else:
                by_coin[coin]["l"] += 1
        except (json.JSONDecodeError, KeyError):
            pass

    stats = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "events": events,
        "wins": wins,
        "losses": losses,
        "wr": f"{wins / events * 100:.1f}%" if events else "0%",
        "total_pnl": round(total_pnl, 2),
        "total_cost": round(total_cost, 2),
        "roi": f"{total_pnl / total_cost * 100:.0f}%" if total_cost > 0 else "0%",
        "by_coin": by_coin,
    }
    STATS_FILE.write_text(json.dumps(stats, indent=2))
    log.info(
        f"STATS: {events} events | {wins}W/{losses}L | "
        f"PnL=${total_pnl:+.2f} | Cost=${total_cost:.2f}"
    )


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # SIGTERM/SIGINT -> graceful cleanup (2026-04-17 bug fix:
    # restart mid-window left orders live on CLOB → orphan fills like the
    # 2:55-3:00 Down 233sh position that cost $5.99 this morning)
    _shutdown = {"flag": False}
    def _sig(signum, frame):
        _shutdown["flag"] = True
        log.info(f"Received signal {signum} — will cleanup on next loop iter")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    log.info("=" * 60)
    log.info(f"5-Minute Crypto Bot {'(DRY RUN)' if DRY_RUN else 'LIVE'}")
    tier_str = " + ".join(f"{int(p*100)}% @ {int(pr*100)}c" for pr, p in TIERS)
    log.info(f"Sizing: ${BET_USD_PER_SIDE:.2f}/side fixed — tiers: {tier_str}")
    log.info(f"Min shares: {MIN_SHARES}")
    log.info(f"Coins: {', '.join(COINS.values())}")
    log.info("=" * 60)

    if DRY_RUN:
        client = None
    else:
        client = init_clob()

    active_windows: dict[str, Window] = {}  # cid -> Window
    last_discovery = 0
    cycle = 0

    while True:
        try:
            now = datetime.now(timezone.utc)

            # ── Discover new windows ──
            if time.time() - last_discovery > DISCOVERY_INTERVAL:
                new_windows = discover_windows(client)
                for w in new_windows:
                    if w.condition_id not in active_windows:
                        # Only add windows that haven't started yet or just started
                        secs_since_start = (now - w.start_utc).total_seconds()
                        if -60 < secs_since_start < 120:  # within 1min before to 2min after start
                            active_windows[w.condition_id] = w
                            log.info(
                                f"NEW {w.coin} 5m | {w.title[:50]} | "
                                f"start={w.start_utc.strftime('%H:%M')} "
                                f"end={w.end_utc.strftime('%H:%M')} UTC"
                            )
                last_discovery = time.time()

            # ── Process each window ──
            for cid, w in list(active_windows.items()):
                secs_since_start = (now - w.start_utc).total_seconds()
                secs_to_end = (w.end_utc - now).total_seconds()

                # Place orders shortly after window opens
                if not w.orders_placed and WINDOW_LEAD_TIME < secs_since_start < 180:
                    skip, move_pct = should_skip_binance_momentum()
                    if skip:
                        log.info(
                            f"SKIP {w.coin} {w.title[:45]} — "
                            f"Binance 10m move {move_pct:+.3f}% > {SKIP_MOVE_10M_THRESHOLD*100:.2f}%"
                        )
                        w.orders_placed = True  # don't retry
                    else:
                        log.info(f"PLACING {w.coin} {w.title[:50]} (bin_mv={move_pct:+.3f}%)")
                        place_orders(client, w, 0)

                # Check fills during window — every 10s for fast opposite-side cancel
                if w.orders_placed and not w.resolved and secs_to_end > -60:
                    if cycle % 2 == 0:  # every 10s (LOOP_INTERVAL=5s)
                        check_fills(client, w)
                        check_both_sides_filled(w)

                # T-3s: cancel ALL unfilled tiers.
                # 2026-04-17 revised: oude T-15s cancel was te agressief. Bewijs 5:45-5:50
                # window: 1595sh HV fills bij T+290-297s (= T-10 tot T-3), wij misten dat
                # omdat onze T-15 cancel onze 1c/2c/3c al dood had gemaakt.
                # Nu: cancel pas bij secs_to_end <= 5 (fires op laatste tick voor T-0).
                # MUST cancel VOOR window close — fills post-window = settlement = 0% WR.
                if w.orders_placed and not w.resolved and 0 < secs_to_end <= 5:
                    cancel_unfilled_tiers(client, w, [0.01, 0.02, 0.03, 0.04], "T-3s")

                # Cancel unfilled after window ends
                if w.orders_placed and secs_to_end < -CANCEL_AFTER_END and not w.resolved:
                    cancel_orders(client, w)

                # Check resolution
                if w.orders_placed and secs_to_end < -30 and not w.resolved:
                    check_resolution(w)

                # Cleanup old windows (wait 10min for PM to resolve)
                if secs_to_end < -600:
                    if w.fills and not w.resolved:
                        check_resolution(w)  # last attempt
                    del active_windows[cid]

            # Stats every 5 min
            if cycle % 60 == 0 and cycle > 0:
                update_stats()

        except Exception as e:
            log.error(f"Loop error: {e}")

        if _shutdown["flag"]:
            log.info("Shutting down — cancelling all open orders")
            if client and not DRY_RUN:
                for w in active_windows.values():
                    if w.orders_placed and not w.resolved:
                        try:
                            cancel_orders(client, w)
                        except Exception as e:
                            log.error(f"cleanup cancel_orders failed: {e}")
            try:
                update_stats()
            except Exception:
                pass
            break

        cycle += 1
        # Interruptible sleep: wake on signal instead of waiting full interval.
        for _ in range(int(LOOP_INTERVAL)):
            if _shutdown["flag"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
