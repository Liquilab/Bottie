#!/usr/bin/env python3
"""Daily healthcheck: per wallet per league — why did we trade or not?
Cron: 0 9 * * * cd /opt/bottie && python3 scripts/healthcheck.py >> logs/healthcheck.log 2>&1
"""
import json, subprocess, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(os.environ.get("BOTTIE_DATA", "data"))
TRADES_FILE = DATA_DIR / "trades.jsonl"
REPORT_FILE = DATA_DIR / "healthcheck.json"
CONFIG_FILE = Path("config.yaml")

def get_active_wallets():
    try:
        import yaml
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f)
        wallets = []
        for w in cfg.get("copy_trading", {}).get("watchlist", []):
            if w.get("weight", 0) > 0:
                wallets.append({
                    "address": w["address"],
                    "name": w["name"],
                    "leagues": w.get("leagues", []),
                    "min_source_usdc": w.get("min_source_usdc", 0),
                    "min_source_usdc_per_league": w.get("min_source_usdc_per_league", {}),
                })
        return wallets
    except Exception as e:
        print("ERROR parsing config: %s" % e)
        return []

def get_recent_trades(hours=24):
    """Load trades from the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    trades = []
    if not TRADES_FILE.exists():
        return trades
    with open(TRADES_FILE) as f:
        for line in f:
            try:
                t = json.loads(line.strip())
                ts = t.get("timestamp", "")
                if ts > cutoff.isoformat():
                    trades.append(t)
            except:
                continue
    return trades

def get_recent_logs(hours=24):
    """Parse journalctl for SIGNAL, T1 SKIP, DISCOVER, BOUGHT logs."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
        result = subprocess.run(
            ["journalctl", "-u", "bottie", "--no-pager", "--since", since],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout
    except:
        return ""

def parse_logs(log_text, wallets):
    """Extract per-wallet stats from bot logs."""
    stats = {}
    for w in wallets:
        name = w["name"]
        stats[name] = {
            "signals": 0,
            "discovers": 0,
            "t1_skips": 0,
            "t1_confirms": 0,
            "skip_reasons": defaultdict(int),
        }

    for line in log_text.split("\n"):
        for w in wallets:
            name = w["name"]
            if name not in line:
                continue

            if "SIGNAL:" in line and name in line:
                stats[name]["signals"] += 1

            if "DISCOVER:" in line and name in line:
                stats[name]["discovers"] += 1

            if "T1 SKIP:" in line and name in line:
                stats[name]["t1_skips"] += 1
                # Extract reason
                m = re.search(r"T1 SKIP:.*?position \$[\d.]+ < min \$[\d.]+ \((\w+)\)", line)
                if m:
                    stats[name]["skip_reasons"]["below_min_%s" % m.group(1)] += 1
                elif "hauptbet" in line.lower():
                    stats[name]["skip_reasons"]["hauptbet_wrong_type"] += 1
                else:
                    stats[name]["skip_reasons"]["other"] += 1

            if "T1 CONFIRMED:" in line and name in line:
                stats[name]["t1_confirms"] += 1

    return stats

def main():
    now = datetime.now(timezone.utc)
    print("\n=== HEALTHCHECK %s ===" % now.strftime("%Y-%m-%d %H:%M UTC"))

    wallets = get_active_wallets()
    if not wallets:
        print("No active wallets")
        return

    trades = get_recent_trades(24)
    log_text = get_recent_logs(24)
    log_stats = parse_logs(log_text, wallets)

    report = {"timestamp": now.isoformat(), "wallets": []}
    issues = []

    for w in wallets:
        name = w["name"]
        addr = w["address"]
        leagues = w["leagues"]

        # Trades from this wallet in last 24h
        wallet_trades = [t for t in trades if (t.get("copy_wallet") or "")[:12] == addr[:12]]

        ls = log_stats.get(name, {})
        signals = ls.get("signals", 0)
        discovers = ls.get("discovers", 0)
        t1_skips = ls.get("t1_skips", 0)
        t1_confirms = ls.get("t1_confirms", 0)
        skip_reasons = dict(ls.get("skip_reasons", {}))

        # Per-league trade count
        trades_per_league = defaultdict(int)
        for t in wallet_trades:
            sport = t.get("sport", "?")
            trades_per_league[sport] += 1

        wallet_report = {
            "name": name,
            "address": addr[:14],
            "leagues": leagues,
            "signals_24h": signals,
            "discovers_24h": discovers,
            "t1_skips_24h": t1_skips,
            "t1_confirms_24h": t1_confirms,
            "trades_24h": len(wallet_trades),
            "trades_per_league": dict(trades_per_league),
            "skip_reasons": skip_reasons,
            "status": "OK",
        }

        # Determine status
        if len(wallet_trades) > 0:
            status = "TRADED"
        elif t1_confirms > 0:
            status = "CONFIRMED_NO_FILL"
        elif signals > 0 and t1_skips > 0:
            status = "SIGNALS_BELOW_THRESHOLD"
        elif signals > 0:
            status = "SIGNALS_NO_GAMES"
        elif discovers > 0:
            status = "DISCOVERED_NO_SIGNALS"
        else:
            status = "SILENT"
            issues.append("⚠️ %s: 0 signals, 0 discovers in 24h" % name)

        wallet_report["status"] = status

        print("\n%s [%s]:" % (name, status))
        print("  Leagues: %s" % ", ".join(leagues))
        print("  Signals: %d | Discovers: %d | T1 Skips: %d | Confirms: %d | Trades: %d" % (
            signals, discovers, t1_skips, t1_confirms, len(wallet_trades)))
        if skip_reasons:
            print("  Skip reasons: %s" % skip_reasons)
        if trades_per_league:
            print("  Trades per league: %s" % dict(trades_per_league))

        report["wallets"].append(wallet_report)

    # Overall summary
    total_trades = len(trades)
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades if t.get("result"))

    report["summary"] = {
        "total_trades_24h": total_trades,
        "total_pnl_24h": round(total_pnl, 2),
        "issues": issues,
    }

    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print("\n=== SUMMARY ===")
    print("Total trades 24h: %d | PnL: $%.2f" % (total_trades, total_pnl))
    if issues:
        for i in issues:
            print(i)
    else:
        print("✅ All wallets active")

    print("\nSaved: %s" % REPORT_FILE)

if __name__ == "__main__":
    main()
