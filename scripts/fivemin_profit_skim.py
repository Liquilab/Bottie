#!/usr/bin/env python3
"""Fivemin bot profit skim: transfer 25% of realized profit from Crypto 5M Safe to owner.

Runs 2x/day via cron. Sums PnL of trades resolved in the last 12 hours and, if
positive, transfers 25% to SKIM_TO via fivemin_transfer_usdc.py (which handles
the PM bridge relay + Gnosis Safe execTransaction).

Cron (UTC): 0 6,18 * * *
"""
import json, os, sys, subprocess
from datetime import datetime, timedelta, timezone

TRADES_FILE = "/opt/bottie-test/data/fivemin_bot/trades.jsonl"
SKIM_LOG = "/opt/bottie-test/data/fivemin_skim_log.jsonl"
# Compound-first mode: 0% skim until bankroll > $1500.
# Was 0.25. Bump back to 0.10 at $1500, 0.25 at $3000.
SKIM_PCT = 0.00
SKIM_TO = "0x8FE97Deb95BE3ac307d40ccD9D51C36C2acc06ED"
WINDOW_HOURS = 12
MIN_SKIM = 1.0  # minimum $1 to bother transferring


def get_window_pnl():
    """Sum PnL of trades resolved in the last WINDOW_HOURS hours."""
    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(hours=WINDOW_HOURS)

    total_pnl = 0.0
    resolved_count = 0

    if not os.path.exists(TRADES_FILE):
        return 0.0, 0

    with open(TRADES_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
            except json.JSONDecodeError:
                continue

            # fivemin trades.jsonl uses 'timestamp' (ISO string) for resolved time
            ts = trade.get("timestamp")
            if not ts:
                continue

            # Skip non-resolved records (sell-intermediate lines have type=sell)
            if trade.get("type") == "sell":
                continue

            try:
                trade_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

            if trade_dt < cutoff_dt:
                continue

            pnl = trade.get("pnl", 0)
            if isinstance(pnl, (int, float)):
                total_pnl += pnl
                resolved_count += 1

    return total_pnl, resolved_count


def transfer(amount_usdc):
    """Call fivemin_transfer_usdc.py to send USDC from Crypto 5M Safe to owner."""
    script = os.path.join(os.path.dirname(__file__), "fivemin_transfer_usdc.py")
    env = os.environ.copy()

    # Load Crypto 5M private key from bottie-test .env
    env_file = "/opt/bottie-test/.env"
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("PRIVATE_KEY="):
                    env["PRIVATE_KEY"] = line.strip().split("=", 1)[1]

    result = subprocess.run(
        [sys.executable, script, SKIM_TO, str(round(amount_usdc, 2))],
        capture_output=True, text=True, env=env, timeout=180,
    )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": result.stderr or result.stdout}


def log_skim(entry):
    os.makedirs(os.path.dirname(SKIM_LOG), exist_ok=True)
    with open(SKIM_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    now = datetime.now(timezone.utc).isoformat()
    pnl, count = get_window_pnl()

    if count == 0:
        entry = {"timestamp": now, "action": "skip", "reason": "no_resolved_trades",
                 "pnl": 0, "window_hours": WINDOW_HOURS}
        log_skim(entry); print(json.dumps(entry)); return

    if pnl <= 0:
        entry = {"timestamp": now, "action": "skip", "reason": "negative_pnl",
                 "pnl": round(pnl, 2), "resolved": count, "window_hours": WINDOW_HOURS}
        log_skim(entry); print(json.dumps(entry)); return

    skim_amount = pnl * SKIM_PCT

    if skim_amount < MIN_SKIM:
        entry = {"timestamp": now, "action": "skip", "reason": "below_minimum",
                 "pnl": round(pnl, 2), "skim_would_be": round(skim_amount, 2),
                 "resolved": count}
        log_skim(entry); print(json.dumps(entry)); return

    result = transfer(skim_amount)

    entry = {
        "timestamp": now,
        "action": "skim",
        "window_hours": WINDOW_HOURS,
        "pnl": round(pnl, 2),
        "resolved": count,
        "skim_pct": SKIM_PCT,
        "skim_amount": round(skim_amount, 2),
        "to": SKIM_TO,
        "transfer": result,
    }
    log_skim(entry)
    print(json.dumps(entry))


if __name__ == "__main__":
    main()
