#!/usr/bin/env python3
"""Hourly profit skim: transfer 33% of realized profit to owner wallet.

Runs via cron every hour. Checks trades.jsonl for trades resolved in the
last hour, sums P&L, and if positive transfers 33% to SKIM_TO address.
"""
import json, os, sys, subprocess
from datetime import datetime, timedelta, timezone

TRADES_FILE = "/opt/bottie/data/trades.jsonl"
SKIM_LOG = "/opt/bottie/data/skim_log.jsonl"
SKIM_PCT = 0.33
SKIM_TO = "0x8FE97Deb95BE3ac307d40ccD9D51C36C2acc06ED"
MIN_SKIM = 1.0  # minimum $1 to bother transferring


def get_hourly_pnl():
    """Sum PnL of trades resolved in the last hour."""
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    cutoff = one_hour_ago.isoformat()

    total_pnl = 0.0
    resolved_count = 0

    with open(TRADES_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
            except json.JSONDecodeError:
                continue

            resolved_at = trade.get("resolved_at")
            if not resolved_at:
                continue

            # Only trades resolved in the last hour
            if resolved_at < cutoff:
                continue

            pnl = trade.get("pnl") or trade.get("actual_pnl") or 0
            if isinstance(pnl, (int, float)):
                total_pnl += pnl
                resolved_count += 1

    return total_pnl, resolved_count


def transfer(amount_usdc):
    """Call transfer_usdc.py to send USDC from Safe to owner wallet."""
    script = os.path.join(os.path.dirname(__file__), "transfer_usdc.py")
    env = os.environ.copy()

    # Load private key from .env
    env_file = "/opt/bottie/.env"
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("PRIVATE_KEY="):
                    env["PRIVATE_KEY"] = line.strip().split("=", 1)[1]

    result = subprocess.run(
        [sys.executable, script, SKIM_TO, str(round(amount_usdc, 2))],
        capture_output=True, text=True, env=env, timeout=120,
    )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": result.stderr or result.stdout}


def log_skim(entry):
    """Append skim event to log."""
    with open(SKIM_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    now = datetime.now(timezone.utc).isoformat()
    pnl, count = get_hourly_pnl()

    if count == 0:
        entry = {"timestamp": now, "action": "skip", "reason": "no_resolved_trades", "pnl": 0}
        log_skim(entry)
        print(json.dumps(entry))
        return

    if pnl <= 0:
        entry = {"timestamp": now, "action": "skip", "reason": "negative_pnl", "pnl": round(pnl, 2), "resolved": count}
        log_skim(entry)
        print(json.dumps(entry))
        return

    skim_amount = pnl * SKIM_PCT

    if skim_amount < MIN_SKIM:
        entry = {"timestamp": now, "action": "skip", "reason": "below_minimum", "pnl": round(pnl, 2), "skim_would_be": round(skim_amount, 2)}
        log_skim(entry)
        print(json.dumps(entry))
        return

    # Execute transfer
    result = transfer(skim_amount)

    entry = {
        "timestamp": now,
        "action": "skim",
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
