#!/usr/bin/env python3
"""Daily drift check: detect edge erosion on active wallets.
Cron: 0 6 * * * cd /opt/bottie && python3 scripts/drift_check.py >> logs/drift_check.log 2>&1
"""
import json, urllib.request, time, os, sys
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

API = "https://data-api.polymarket.com"
DATA_DIR = Path(os.environ.get("BOTTIE_DATA", "data"))
BASELINE_FILE = DATA_DIR / "wallet_baselines.json"
REPORT_FILE = DATA_DIR / "drift_report.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"
CONFIG_FILE = Path("config.yaml")

# Drift threshold: alert if recent WR drops more than this below baseline
DRIFT_THRESHOLD = 0.15  # 15%
INACTIVE_DAYS = 14

# Kill switch thresholds — applied to our bot's realized copies per wallet
KILL_MIN_N = 30
KILL_WR_FLOOR = 0.40
KILL_PNL_FLOOR = -50.0

def fetch(u):
    time.sleep(0.4)
    req = urllib.request.Request(u, headers={"User-Agent": "B/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def get_active_wallets():
    """Parse config.yaml for active wallets (weight > 0)."""
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

def get_closed_per_league(address):
    """Get ALL closed positions, grouped by league."""
    offset = 0
    all_closed = []
    while True:
        try:
            data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, address, offset))
        except:
            time.sleep(3)
            try:
                data = fetch("%s/closed-positions?user=%s&limit=50&offset=%d" % (API, address, offset))
            except:
                break
        if not data:
            break
        all_closed.extend(data)
        if len(data) < 50:
            break
        offset += 50

    by_league = defaultdict(list)
    for t in all_closed:
        slug = t.get("eventSlug", "") or ""
        league = slug.split("-")[0] if slug else "?"
        pnl = float(t.get("realizedPnl", 0) or 0)
        inv = float(t.get("totalBought", 0) or 0)
        ts = t.get("endDate") or t.get("timestamp")
        by_league[league].append({"pnl": pnl, "inv": inv, "ts": ts, "win": pnl > 0})

    return by_league, len(all_closed)

def get_ghost_losses_per_league(address):
    """Get ghost losses (open positions with cv=0) per league."""
    try:
        pos = fetch("%s/positions?user=%s&limit=500&sizeThreshold=0.01" % (API, address))
    except:
        return {}

    by_league = defaultdict(lambda: {"count": 0, "total": 0})
    for p in pos:
        iv = float(p.get("initialValue", 0) or 0)
        cv = float(p.get("currentValue", 0) or 0)
        if cv == 0 and iv > 0:
            slug = p.get("eventSlug", "") or ""
            league = slug.split("-")[0] if slug else "?"
            by_league[league]["count"] += 1
            by_league[league]["total"] += iv

    return dict(by_league)

def load_baselines():
    if BASELINE_FILE.exists():
        with open(BASELINE_FILE) as f:
            return json.load(f)
    return {}

def save_baselines(baselines):
    with open(BASELINE_FILE, "w") as f:
        json.dump(baselines, f, indent=2)

def realized_per_wallet_killswitch():
    """Kill switch: realized PnL + WR per wallet over last KILL_MIN_N resolved copies.

    Reads data/trades.jsonl (SSOT for our bot's actual trades) — NOT tracked
    wallet's history. Catches the case where tracked wallet's baseline WR stays
    high but OUR copies of them go bad (lag, selection, regime change).

    Returns (section_for_report, list_of_alerts).
    """
    if not TRADES_FILE.exists():
        return {"enabled": False, "reason": "trades.jsonl missing"}, []

    buckets = defaultdict(list)
    for line in open(TRADES_FILE):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("resolved_at"):
            continue
        if r.get("pnl") is None:
            continue
        cw = r.get("consensus_wallets") or []
        if not cw:
            continue
        wallet = cw[0]
        if wallet in ("Manual", "WhaleConsensus"):
            continue  # not a followed-wallet signal
        buckets[wallet].append({
            "resolved_at": r["resolved_at"],
            "pnl": float(r["pnl"]),
            "result": r.get("result"),
            "sport": r.get("sport", "?"),
        })

    section = {"enabled": True, "wallets": []}
    alerts = []
    print("\n=== REALIZED KILL SWITCH (last %d copies/wallet) ===" % KILL_MIN_N)
    for wallet, trades in sorted(buckets.items()):
        trades.sort(key=lambda t: t["resolved_at"], reverse=True)
        recent = trades[:KILL_MIN_N]
        n = len(recent)
        pnl = sum(t["pnl"] for t in recent)
        wins = sum(1 for t in recent if t.get("result") == "win")
        wr = wins / n if n else 0.0

        status = "OK"
        if n < KILL_MIN_N:
            status = "INSUFFICIENT_N"
        elif wr < KILL_WR_FLOOR or pnl < KILL_PNL_FLOOR:
            status = "DISABLE_RECOMMENDED"
            alerts.append(
                "KILL %s: n=%d wr=%.0f%% pnl=$%+.2f (floor wr=%.0f%% pnl=$%.0f)"
                % (wallet, n, wr*100, pnl, KILL_WR_FLOOR*100, KILL_PNL_FLOOR)
            )

        print("  %-16s n=%-3d wr=%5.1f%% pnl=$%+8.2f %s"
              % (wallet, n, wr*100, pnl, status))
        section["wallets"].append({
            "wallet": wallet, "n": n, "wr": round(wr, 3),
            "pnl": round(pnl, 2), "status": status,
        })
    return section, alerts


def main():
    now = datetime.now(timezone.utc)
    print("\n=== DRIFT CHECK %s ===" % now.strftime("%Y-%m-%d %H:%M UTC"))

    wallets = get_active_wallets()
    if not wallets:
        print("No active wallets found")
        return

    baselines = load_baselines()
    report = {"timestamp": now.isoformat(), "wallets": []}
    alerts = []

    for wallet in wallets:
        addr = wallet["address"]
        name = wallet["name"]
        target_leagues = wallet["leagues"]

        print("\n--- %s (%s) ---" % (name, addr[:12]))

        by_league, total_closed = get_closed_per_league(addr)
        ghost_losses = get_ghost_losses_per_league(addr)

        wallet_report = {"name": name, "address": addr, "leagues": []}

        for league in target_leagues:
            trades = by_league.get(league, [])
            ghosts = ghost_losses.get(league, {"count": 0, "total": 0})

            if not trades:
                print("  %s: no closed trades" % league)
                wallet_report["leagues"].append({
                    "league": league, "status": "NO_DATA"
                })
                continue

            # All-time stats
            all_wins = sum(1 for t in trades if t["win"])
            all_losses = len(trades) - all_wins + ghosts["count"]
            all_total = len(trades) + ghosts["count"]
            all_wr = all_wins / all_total if all_total > 0 else 0
            all_pnl = sum(t["pnl"] for t in trades) - ghosts["total"]

            # Recent 30 trades (by timestamp, most recent)
            sorted_trades = sorted(trades, key=lambda t: str(t.get("ts", "")), reverse=True)
            recent = sorted_trades[:30]
            recent_wins = sum(1 for t in recent if t["win"])
            recent_losses = len(recent) - recent_wins
            recent_wr = recent_wins / len(recent) if recent else 0
            recent_pnl = sum(t["pnl"] for t in recent)

            # Last trade timestamp
            last_ts = str(sorted_trades[0].get("ts", "")) if sorted_trades else ""
            try:
                if last_ts and last_ts[:10] != "":
                    last_date = datetime.strptime(last_ts[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    days_since = (now - last_date).days
                else:
                    days_since = 999
            except:
                days_since = 999

            # Baseline: store or compare
            baseline_key = "%s_%s" % (addr[:12], league)
            if baseline_key not in baselines:
                baselines[baseline_key] = {
                    "wr": all_wr,
                    "pnl": all_pnl,
                    "n": all_total,
                    "set_at": now.isoformat()
                }
                status = "BASELINE_SET"
            else:
                baseline_wr = baselines[baseline_key]["wr"]
                drift = baseline_wr - recent_wr

                if days_since > INACTIVE_DAYS:
                    status = "INACTIVE"
                    alerts.append("⚠️ %s %s: inactive %d days" % (name, league, days_since))
                elif drift > DRIFT_THRESHOLD:
                    status = "DRIFT_ALERT"
                    alerts.append("🔴 %s %s: WR dropped %.0f%% → %.0f%% (baseline %.0f%%)" % (
                        name, league, all_wr * 100, recent_wr * 100, baseline_wr * 100))
                elif drift > DRIFT_THRESHOLD / 2:
                    status = "DRIFT_WARNING"
                    alerts.append("🟡 %s %s: WR %.0f%% → %.0f%% (baseline %.0f%%)" % (
                        name, league, all_wr * 100, recent_wr * 100, baseline_wr * 100))
                else:
                    status = "OK"

                # Update baseline if better (prevents ratcheting down)
                if all_wr > baselines[baseline_key]["wr"] and all_total > baselines[baseline_key]["n"]:
                    baselines[baseline_key]["wr"] = all_wr
                    baselines[baseline_key]["pnl"] = all_pnl
                    baselines[baseline_key]["n"] = all_total

            league_report = {
                "league": league,
                "status": status,
                "all_wr": round(all_wr * 100, 1),
                "all_pnl": round(all_pnl),
                "all_n": all_total,
                "recent_wr": round(recent_wr * 100, 1),
                "recent_pnl": round(recent_pnl),
                "recent_n": len(recent),
                "ghost_losses": ghosts["count"],
                "ghost_total": round(ghosts["total"]),
                "days_since_last": days_since,
            }
            wallet_report["leagues"].append(league_report)

            print("  %s: %s | all=%.0f%% WR (%d trades) | recent30=%.0f%% WR | ghosts=%d (-$%.0f) | %dd ago" % (
                league, status, all_wr * 100, all_total, recent_wr * 100,
                ghosts["count"], ghosts["total"], days_since))

        report["wallets"].append(wallet_report)

    # Realized kill switch (our bot's actual copy performance per wallet)
    killswitch_section, kill_alerts = realized_per_wallet_killswitch()
    report["realized_killswitch"] = killswitch_section
    alerts.extend(kill_alerts)

    # Save
    save_baselines(baselines)
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    # Summary
    print("\n=== SUMMARY ===")
    if alerts:
        for a in alerts:
            print(a)
    else:
        print("All wallets OK")

    print("\nSaved: %s, %s" % (BASELINE_FILE, REPORT_FILE))

if __name__ == "__main__":
    main()
