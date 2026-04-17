#!/usr/bin/env python3
"""Bottie Trading Dashboard — single-file, no external deps, port 8080."""

import json, re, glob, os
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    CET = ZoneInfo("Europe/Amsterdam")
except ImportError:
    CET = None

# Use CWD (/opt/bottie) for data files, __file__ dir for code/templates
BASE_DIR       = Path.cwd()
CODE_DIR       = Path(__file__).parent
TRADES_FILE    = BASE_DIR / "data" / "trades.jsonl"
DAG_FILE       = BASE_DIR / "data" / "research_dag.jsonl"
SCOUT_FILE     = BASE_DIR / "data" / "scout_report.json"
PLAYBOOK_FILE  = BASE_DIR / "data" / "playbook.md"
CONFIG_FILE    = BASE_DIR / "config.yaml"
PM_CACHE_FILE  = BASE_DIR / "data" / "pm_cache.json"
CONSENSUS_BULK = BASE_DIR / "data" / "consensus_bulk.json"
CONSENSUS_RESULTS = BASE_DIR / "data" / "consensus_results.json"
EDGE_REPORT_FILE = BASE_DIR / "data" / "edge_analysis_report.md"

# Auth token — all routes require /t/<TOKEN>/ prefix (like webhook URLs)
AUTH_TOKEN = os.environ.get("DASHBOARD_TOKEN", "8vNADas4jmnOk3IbpeBFrgDHkKHN9Epq")

# Polymarket Data API — source of truth
PM_DATA_API = "https://data-api.polymarket.com"
LB_API     = "https://lb-api.polymarket.com"

# Cannae (bottie, /opt/bottie/)
PM_FUNDER       = "0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42"
INITIAL_BANKROLL = 1400.0  # moved here for clarity

# Crypto 5M (bottie-test, /opt/bottie-test/)
CRYPTO5M_FUNDER          = "0x8A3A19AeC04eeB6E3C183ee5750D06fe5c08066a"
CRYPTO5M_TRADES_FILE     = Path("/opt/bottie-test/data/trades.jsonl")
CRYPTO5M_CONFIG_FILE     = Path("/opt/bottie-test/config.yaml")


# ── PM API Data (source of truth) ──────────────────────────────────────────

import urllib.request, urllib.error, time

_pm_caches = {}
_journal_cache = {"data": None, "ts": 0}  # 30s TTL for fivemin-bot journalctl  # per-funder cache: {funder: {"data": ..., "ts": ...}}
_lb_cache   = {}  # per-address lb-api cache: {address: {"val": ..., "ts": ...}}

def fetch_pm_data(funder=None):
    """Fetch real data from Polymarket API. Cached 15s per funder."""
    if funder is None:
        funder = PM_FUNDER
    now = time.time()
    cache = _pm_caches.setdefault(funder, {"data": None, "ts": 0})
    if cache["data"] and now - cache["ts"] < 15:
        return cache["data"]

    result = {"trades": [], "positions": [], "value": 0, "positions_value": 0, "cash": 0, "error": None}

    def pm_get(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Bottie-Dashboard/1.0",
            "Accept": "application/json",
        })
        return urllib.request.urlopen(req, timeout=15)

    try:
        all_trades = []
        offset = 0
        while offset < 10000:
            url = "%s/trades?user=%s&limit=1000&offset=%d" % (PM_DATA_API, funder, offset)
            batch = json.loads(pm_get(url).read())
            all_trades.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        result["trades"] = all_trades
    except Exception as e:
        result["error"] = "trades: %s" % e

    try:
        all_positions = []
        pos_offset = 0
        while pos_offset <= 10000:
            url = "%s/positions?user=%s&limit=500&offset=%d&sizeThreshold=0.01" % (PM_DATA_API, funder, pos_offset)
            batch = json.loads(pm_get(url).read())
            all_positions.extend(batch)
            if len(batch) < 500:
                break
            pos_offset += 500
        result["positions"] = all_positions
    except Exception as e:
        result["error"] = "positions: %s" % e

    try:
        url = "%s/value?user=%s" % (PM_DATA_API, funder)
        val = json.loads(pm_get(url).read())
        if isinstance(val, list) and val:
            result["positions_value"] = float(val[0].get("value", 0))
    except Exception:
        pass

    # Cash = on-chain USDC.e balance via Polygon RPC
    try:
        addr = funder.lower().replace("0x", "")
        data = "0x70a08231" + addr.rjust(64, "0")
        payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call",
                              "params": [{"to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "data": data}, "latest"], "id": 1})
        rpc_req = urllib.request.Request("https://polygon-bor-rpc.publicnode.com",
                                         data=payload.encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "Bottie-Dashboard/1.0"},
                                         method="POST")
        rpc_resp = json.loads(urllib.request.urlopen(rpc_req, timeout=10).read())
        hex_val = rpc_resp.get("result", "0x0").replace("0x", "")
        result["cash"] = int(hex_val, 16) / 1_000_000.0
    except Exception:
        result["cash"] = 0

    result["value"] = result["positions_value"] + result["cash"]

    cache["data"] = result
    cache["ts"] = now
    return result


def fetch_lb_profit(address):
    """All-time profit from lb-api (source of truth for PnL). Cached 60s."""
    now = time.time()
    entry = _lb_cache.get(address, {"val": None, "ts": 0})
    if entry["val"] is not None and now - entry["ts"] < 60:
        return entry["val"]
    try:
        req = urllib.request.Request(
            "%s/profit?address=%s" % (LB_API, address),
            headers={"User-Agent": "Bottie-Dashboard/1.0", "Accept": "application/json"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if isinstance(data, list) and data:
            val = float(data[0].get("amount", 0))
            _lb_cache[address] = {"val": val, "ts": now}
            return val
    except Exception:
        pass
    return None


def sf(v):
    """Safe float conversion."""
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        try: return float(v)
        except: return 0.0
    return 0.0


def compute_pm_kpis(funder=None, initial_bankroll=None):
    """Compute KPIs from Polymarket API data (source of truth)."""
    if funder is None:
        funder = PM_FUNDER
    if initial_bankroll is None:
        initial_bankroll = INITIAL_BANKROLL
    pm = fetch_pm_data(funder)
    trades = pm["trades"]
    positions = pm["positions"]

    # Filter out crypto up/down
    trades = [t for t in trades if "up or down" not in (t.get("title") or "").lower()]

    buys = [t for t in trades if (t.get("side") or "").upper() == "BUY"]
    sells = [t for t in trades if (t.get("side") or "").upper() == "SELL"]

    total_bought = sum(sf(t.get("size", 0)) * sf(t.get("price", 0)) for t in buys)
    total_sold = sum(sf(t.get("size", 0)) * sf(t.get("price", 0)) for t in sells)

    active = [p for p in positions if sf(p.get("size", 0)) > 0]
    position_value = sum(sf(p.get("currentValue", 0)) for p in active)
    position_cost = sum(sf(p.get("initialValue", 0)) for p in active)

    portfolio_value = pm["value"] if pm["value"] > 0 else position_value
    cash = pm.get("cash", 0)

    # lb-api: source of truth for all-time PnL
    lb_profit = fetch_lb_profit(funder)

    return {
        "portfolio_value": portfolio_value,
        "cash": cash,
        "position_value": position_value,
        "position_cost": position_cost,
        "unrealized_pnl": position_value - position_cost,
        "total_bought": total_bought,
        "total_sold": total_sold,
        "open_count": len(active),
        "total_trades": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "deposited": initial_bankroll,
        "rendement": portfolio_value - initial_bankroll if portfolio_value > 0 else 0,
        "rendement_pct": (portfolio_value / initial_bankroll - 1) * 100 if portfolio_value > 0 and initial_bankroll > 0 else 0,
        "lb_profit": lb_profit,
        "pm_error": pm["error"],
    }


# ── Data Loading (trades.jsonl — for wallet attribution only) ──────────────

def load_trades():
    if not TRADES_FILE.exists():
        return []
    trades = []
    with open(TRADES_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                    # Filter out manual trades UNLESS they're still open (show all open positions)
                    if t.get("signal_source") == "manual" and t.get("result") is not None:
                        continue
                    title = (t.get("market_title") or "").lower()
                    if "up or down" in title:
                        continue
                    if "bitcoin up" in title or "ethereum up" in title or "solana up" in title or "xrp up" in title:
                        continue
                    trades.append(t)
                except Exception:
                    pass
    return trades

def load_trades_crypto5m():
    """Load Crypto 5M (bottie-test) trades from /opt/bottie-test/data/trades.jsonl."""
    if not CRYPTO5M_TRADES_FILE.exists():
        return []
    trades = []
    with open(CRYPTO5M_TRADES_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                    if t.get("signal_source") == "manual" and t.get("result") is not None:
                        continue
                    title = (t.get("market_title") or "").lower()
                    if "up or down" in title:
                        continue
                    trades.append(t)
                except Exception:
                    pass
    return trades


def parse_config_wallets_crypto5m():
    """Parse Crypto 5M watchlist from /opt/bottie-test/config.yaml."""
    if not CRYPTO5M_CONFIG_FILE.exists():
        return {}
    try:
        import yaml
        with open(CRYPTO5M_CONFIG_FILE) as f:
            config = yaml.safe_load(f)
        wallets = {}
        for w in config.get("copy_trading", {}).get("watchlist", []):
            addr = w.get("address", "").lower()
            name = w.get("name", addr[:10])
            weight = w.get("weight", 0.5)
            tier = "T1" if weight >= 0.85 else "T2" if weight >= 0.65 else "T3"
            wallets[addr] = {
                "name": name, "weight": weight, "tier": tier,
                "market_types": w.get("market_types", []),
                "min_price": w.get("min_price", 0),
                "max_price": w.get("max_price", 1),
                "leagues": w.get("leagues", []),
                "sports": w.get("sports", []),
                "max_legs_per_event": w.get("max_legs_per_event", 0),
            }
        return wallets
    except Exception:
        return {}


def load_dag():
    if not DAG_FILE.exists():
        return []
    entries = []
    for line in DAG_FILE.read_text().splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries


def load_scout_report():
    if not SCOUT_FILE.exists():
        return {}
    try:
        return json.loads(SCOUT_FILE.read_text())
    except Exception:
        return {}


def load_playbook():
    if not PLAYBOOK_FILE.exists():
        return ""
    return PLAYBOOK_FILE.read_text()


def _load_config():
    """Load config.yaml as dict."""
    if not CONFIG_FILE.exists():
        return {}
    import yaml
    return yaml.safe_load(CONFIG_FILE.read_text()) or {}

def parse_config_wallets():
    if not CONFIG_FILE.exists():
        return {}
    try:
        import yaml
        with open(CONFIG_FILE) as f:
            config = yaml.safe_load(f)
        wallets = {}
        for w in config.get("copy_trading", {}).get("watchlist", []):
            addr = w.get("address", "").lower()
            name = w.get("name", addr[:10])
            weight = w.get("weight", 0.5)
            tier = "T1" if weight >= 0.85 else "T2" if weight >= 0.65 else "T3"
            market_types = w.get("market_types", [])
            min_price = w.get("min_price", 0)
            max_price = w.get("max_price", 1)
            leagues = w.get("leagues", [])
            sports = w.get("sports", [])
            max_legs = w.get("max_legs_per_event", 0)
            wallets[addr] = {
                "name": name, "weight": weight, "tier": tier,
                "market_types": market_types,
                "min_price": min_price, "max_price": max_price,
                "leagues": leagues, "sports": sports,
                "max_legs_per_event": max_legs,
            }
        return wallets
    except Exception:
        return {}

HYPOTHESES_DIR = BASE_DIR / "data" / "hypotheses"

def load_hypotheses():
    if not HYPOTHESES_DIR.exists():
        return []
    files = sorted(glob.glob(str(HYPOTHESES_DIR / "*.json")), reverse=True)[:20]
    out = []
    for f in files:
        try:
            data = json.loads(Path(f).read_text())
            data["_mtime"] = os.path.getmtime(f)
            out.append(data)
        except Exception:
            pass
    return out


# ── Aggregations ────────────────────────────────────────────────────────────

def compute_kpis(trades, funder=None, initial_bankroll=None):
    """Compute KPIs — uses PM API for portfolio/value, trades.jsonl for WR attribution."""
    pm = compute_pm_kpis(funder=funder, initial_bankroll=initial_bankroll)

    # WR from trades.jsonl (for relative comparison only)
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    resolved = [t for t in filled if t.get("result") in ("win", "loss", "take_profit", "sold")]
    wins = [t for t in resolved if t.get("result") in ("win", "take_profit")]
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0

    today = datetime.now(timezone.utc).date()
    daily_resolved = [t for t in resolved if t.get("resolved_at") and t["resolved_at"][:10] == str(today)]
    daily_pnl = sum((t.get("pnl") or 0) for t in daily_resolved)
    daily_wins = sum(1 for t in daily_resolved if t.get("result") in ("win", "take_profit"))
    daily_losses = sum(1 for t in daily_resolved if t.get("result") == "loss")

    return {
        # PM API data (source of truth)
        "portfolio_value": pm["portfolio_value"],
        "cash": pm.get("cash", 0),
        "position_value": pm["position_value"],
        "unrealized_pnl": pm["unrealized_pnl"],
        "open_count": pm["open_count"],
        "open_value": pm["position_cost"],
        "deposited": pm["deposited"],
        "rendement": pm["rendement"],
        "rendement_pct": pm["rendement_pct"],
        "total_trades": pm["total_trades"],
        "lb_profit": pm.get("lb_profit"),
        "pm_error": pm["pm_error"],
        # trades.jsonl data (relative only)
        "total_pnl": sum(t.get("pnl") or 0 for t in resolved),
        "win_rate": win_rate,
        "resolved_count": len(resolved),
        "wins_count": len(wins),
        "losses_count": len(resolved) - len(wins),
        "daily_pnl": daily_pnl,
        "daily_wins": daily_wins,
        "daily_losses": daily_losses,
        "dry_run": not filled and any(t.get("dry_run") for t in trades),
    }

def compute_wallet_stats(trades, wallet_map):
    """Per-wallet stats combining trades.jsonl attribution with PM positions data."""
    pm = fetch_pm_data()
    pm_positions = pm.get("positions", [])

    # Build map: conditionId:outcome → current value from PM
    pm_value_map = {}
    for p in pm_positions:
        if sf(p.get("size", 0)) > 0:
            key = (p.get("conditionId", "") + ":" + (p.get("outcome") or "")).lower()
            pm_value_map[key] = {
                "current_value": sf(p.get("currentValue", 0)),
                "initial_value": sf(p.get("initialValue", 0)),
                "cur_price": sf(p.get("curPrice", 0)),
            }

    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    by_wallet = {}
    for t in filled:
        addr = (t.get("copy_wallet") or "").lower()
        if not addr:
            addr = "_manual"
        if addr not in by_wallet:
            by_wallet[addr] = []
        by_wallet[addr].append(t)

    stats = []
    # Only show currently configured wallets + manual — hide removed/old wallets
    all_addrs = set(wallet_map.keys()) | {"_manual"}
    for addr in all_addrs:
        group = by_wallet.get(addr, [])
        resolved = [t for t in group if t.get("result") in ("win", "loss", "take_profit", "sold")]
        wins = [t for t in resolved if t.get("result") in ("win", "take_profit")]
        losses = [t for t in resolved if t.get("result") == "loss"]
        tp = [t for t in resolved if t.get("result") == "take_profit"]
        sold = [t for t in resolved if t.get("result") == "sold"]
        pnl_resolved = sum(t.get("pnl") or 0 for t in resolved)
        wr = len(wins) / len(resolved) * 100 if resolved else None
        total_invested = sum(t.get("size_usdc") or 0 for t in group)
        avg_size = total_invested / len(group) if group else 0
        roi = (pnl_resolved / total_invested * 100) if total_invested > 0 else 0

        # Avg entry price
        avg_entry = sum(t.get("price") or 0 for t in group) / len(group) if group else 0

        # Recent form: last 10 resolved trades
        recent_resolved = sorted(resolved, key=lambda t: t.get("resolved_at") or t.get("timestamp") or "", reverse=True)[:10]
        recent_form = "".join("W" if t.get("result") in ("win", "take_profit") else "L" for t in recent_resolved)

        # Best and worst single trade
        best_trade = max((t.get("pnl") or 0 for t in resolved), default=0)
        worst_trade = min((t.get("pnl") or 0 for t in resolved), default=0)

        # Open positions: match with PM for current value
        open_trades = [t for t in group if t.get("result") is None]
        open_invested = 0.0
        open_current = 0.0
        for t in open_trades:
            key = (t.get("condition_id", "") + ":" + (t.get("outcome") or "")).lower()
            pm_pos = pm_value_map.get(key)
            if pm_pos:
                open_invested += pm_pos["initial_value"]
                open_current += pm_pos["current_value"]
            else:
                open_invested += t.get("size_usdc", 0) or 0

        unrealized = open_current - open_invested if open_current > 0 else 0

        info = wallet_map.get(addr, {})
        stats.append({
            "addr": addr,
            "name": info.get("name", "manual" if addr == "_manual" else addr[:10] + "..."),
            "tier": info.get("tier", "?"),
            "weight": info.get("weight", 0),
            "trades": len(group),
            "resolved": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "tp": len(tp),
            "sold": len(sold),
            "win_rate": wr,
            "pnl": pnl_resolved,
            "total_invested": total_invested,
            "avg_size": avg_size,
            "avg_entry": avg_entry,
            "roi": roi,
            "recent_form": recent_form,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "open_count": len(open_trades),
            "open_invested": open_invested,
            "open_current": open_current,
            "unrealized": unrealized,
            "total_value": pnl_resolved + unrealized,
        })
    stats.sort(key=lambda x: x["total_value"], reverse=True)
    return stats

def compute_sport_stats(trades):
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    by_sport = {}
    for t in filled:
        sport = t.get("sport") or "unknown"
        by_sport.setdefault(sport, []).append(t)

    stats = []
    for sport, group in by_sport.items():
        resolved = [t for t in group if t.get("result") in ("win", "loss", "take_profit", "sold")]
        wins = [t for t in resolved if t.get("result") in ("win", "take_profit")]
        pnl = sum(t.get("pnl") or 0 for t in resolved)
        wr = len(wins) / len(resolved) * 100 if resolved else None
        avg_conf = sum(t.get("confidence") or 0 for t in group) / len(group) if group else 0
        stats.append({
            "sport": sport,
            "trades": len(group),
            "resolved": len(resolved),
            "win_rate": wr,
            "pnl": pnl,
            "avg_conf": avg_conf,
        })
    stats.sort(key=lambda x: x["pnl"], reverse=True)
    return stats

def compute_source_stats(trades):
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    copy_t = [t for t in filled if t.get("signal_source") == "copy"]
    arb_t  = [t for t in filled if (t.get("signal_source") or "").startswith("odds_arb")]

    def stats(group):
        resolved = [t for t in group if t.get("result") in ("win", "loss", "take_profit", "sold")]
        wins = [t for t in resolved if t.get("result") in ("win", "take_profit")]
        return {
            "total": len(group),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": len(wins) / len(resolved) * 100 if resolved else None,
            "pnl": sum(t.get("pnl") or 0 for t in resolved),
            "avg_size": sum(t.get("size_usdc") or 0 for t in group) / len(group) if group else 0,
            "avg_conf": sum(t.get("confidence") or 0 for t in group) / len(group) if group else 0,
            "avg_edge": sum(t.get("edge_pct") or 0 for t in group) / len(group) if group else 0,
        }

    return {"copy": stats(copy_t), "arb": stats(arb_t)}

def compute_4h_pnl(trades):
    """Group resolved trades by 4-hour UTC buckets — last 5 days (30 buckets)."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss", "take_profit", "sold")]
    by_bucket = {}
    for t in filled:
        ts_str = t.get("resolved_at") or t.get("timestamp") or ""
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            bucket_hour = (ts.hour // 4) * 4
            bucket = ts.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
            key = bucket.strftime("%Y-%m-%dT%H:%M")
            by_bucket[key] = by_bucket.get(key, 0) + (t.get("pnl") or 0)
        except Exception:
            pass
    # Last 5 days = 30 buckets of 4h
    now = datetime.now(timezone.utc)
    bucket_hour = (now.hour // 4) * 4
    current = now.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
    result = []
    for i in range(29, -1, -1):
        b = current - timedelta(hours=i * 4)
        key = b.strftime("%Y-%m-%dT%H:%M")
        label = b.strftime("%d/%H")  # "19/08" = day 19, hour 08
        result.append({"label": label, "key": key, "pnl": by_bucket.get(key, 0)})
    return result


# ── HTML Rendering ───────────────────────────────────────────────────────────

def fmt_pnl(v, show_sign=True):
    if v is None: return '<span class="muted">—</span>'
    sign = "+" if v >= 0 else ""
    cls = "green" if v >= 0 else "red"
    return f'<span class="{cls}">{sign}${v:.2f}</span>'

def fmt_pct(v):
    if v is None: return '<span class="muted">—</span>'
    cls = "green" if v >= 55 else "red" if v < 45 else "yellow"
    return f'<span class="{cls}">{v:.1f}%</span>'

def fmt_result(t):
    r = t.get("result")
    if not t.get("filled"): return '<span class="muted">unfilled</span>'
    if t.get("dry_run"):    return '<span class="muted">dry run</span>'
    if r == "win":          return '<span class="green">WIN</span>'
    if r == "loss":         return '<span class="red">LOSS</span>'
    if r == "take_profit":  return '<span class="green">SOLD TP</span>'
    if r == "sold":         return '<span class="yellow">SOLD</span>'
    if r == "phantom":      return '<span class="muted">PHANTOM</span>'
    return '<span class="yellow">OPEN</span>'

def fmt_age(ts_str):
    if not ts_str: return ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        if h > 0: return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return ""

def render_why(trade, wallet_map):
    src = trade.get("signal_source", "")
    if src == "copy":
        addr = (trade.get("copy_wallet") or "").lower()
        info = wallet_map.get(addr, {})
        name = info.get("name", addr[:8] + "…" if addr else "?")
        delay_s = (trade.get("signal_delay_ms") or 0) / 1000
        # Color by wallet name
        wallet_colors = {"cannae": "#388bfd", "sovereign": "#3fb950", "ewelmealt": "#d29922"}
        name_color = wallet_colors.get(name.lower(), "#8b949e")
        html = f'<span class="badge" style="background:{name_color};color:#fff">{name}</span>'
        html += f' <span class="muted" style="font-size:0.75rem">{delay_s:.1f}s</span>'
        return html
    elif src.startswith("odds_arb:"):
        bookmaker = src.split(":", 1)[1]
        edge = trade.get("edge_pct") or 0
        return (f'<span class="badge" style="background:#f0883e;color:#000">ARB</span> '
                f'<strong>{bookmaker}</strong> '
                f'<span class="badge" style="background:#3fb950;color:#000">+{edge:.1f}%</span>')
    return f'<span class="muted">{src or "?"}</span>'

def render_kpi_row(kpis, wallet_map, trades=None, funder=None, label="Bottie"):
    # PM API data (source of truth)
    portfolio = kpis.get("portfolio_value", 0)
    rendement = kpis.get("rendement", 0)
    rendement_pct = kpis.get("rendement_pct", 0)
    deposited = kpis.get("deposited", INITIAL_BANKROLL)
    unrealized = kpis.get("unrealized_pnl", 0)
    rend_color = "#3fb950" if rendement >= 0 else "#f85149"
    unr_color = "#3fb950" if unrealized >= 0 else "#f85149"

    wr = kpis["win_rate"]
    wr_color = "#3fb950" if wr >= 55 else "#f85149" if wr < 45 else "#d29922"

    goal = 10000.0
    progress = min(100, max(0, portfolio / goal * 100)) if portfolio > 0 else 0

    pm_error = kpis.get("pm_error")
    error_badge = f' <span style="color:#f85149;font-size:11px">⚠ PM API: {pm_error}</span>' if pm_error else ""

    cash = kpis.get("cash", 0)
    pos_val = kpis.get("position_value", 0)

    # lb-api: source of truth for all-time PnL
    lb_profit = kpis.get("lb_profit")
    if lb_profit is not None:
        lb_color = "#3fb950" if lb_profit >= 0 else "#f85149"
        lb_str = f'{"+" if lb_profit >= 0 else ""}${lb_profit:,.0f}'
        lb_sub = "lb-api (source of truth)"
    else:
        lb_color = "#8b949e"
        lb_str = "—"
        lb_sub = "lb-api niet beschikbaar"

    bot_open = count_real_open_bets(trades, funder) if trades else kpis["open_count"]
    pm_total = kpis["open_count"]

    tiles = [
        ("Portfolio (PM)", f'${portfolio:.0f}', "#388bfd",
         f'cash: ${cash:.0f} + posities: ${pos_val:.0f}'),
        ("PnL All-time", lb_str, lb_color, lb_sub),
        ("Open Posities", f'${pos_val:.0f}', unr_color,
         f'{bot_open} bot bets | {pm_total} PM totaal | cash: ${cash:.0f}'),
        ("Win Rate", f"{wr:.1f}%" if kpis["resolved_count"] else "—", wr_color,
         f'{kpis["resolved_count"]} resolved (trades.jsonl)'),
    ]

    tiles_html = ""
    for tile_label, value, color, subtitle in tiles:
        tiles_html += f"""
        <div class="kpi-tile" style="border-top:3px solid {color}">
          <div class="kpi-label">{tile_label}</div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{subtitle}</div>
        </div>"""

    return f"""
    <div class="section-title" style="margin-bottom:8px">{label}</div>
    <div class="kpi-row">{tiles_html}</div>{error_badge}
    <div class="goal-bar-wrap">
      <div class="goal-label">
        DOEL: ${deposited:.0f} → ${goal:.0f}
        <span class="muted" style="float:right">{progress:.1f}% &nbsp; ${portfolio:.0f}</span>
      </div>
      <div class="goal-bar"><div class="goal-fill" style="width:{progress:.1f}%"></div></div>
    </div>"""

def count_real_open_bets(trades, funder=None):
    """Count open bets that actually exist on PM — for accurate headers."""
    open_bets_raw = [t for t in trades if t.get("filled") and t.get("result") is None and not t.get("dry_run")]
    pm = fetch_pm_data(funder)
    pm_positions = pm.get("positions", [])
    pm_open_keys = set()
    for p in pm_positions:
        if sf(p.get("size", 0)) > 0:
            key = (p.get("conditionId", "") + ":" + (p.get("outcome") or "")).lower()
            pm_open_keys.add(key)
    if pm_open_keys:
        return len([t for t in open_bets_raw if
                    (t.get("condition_id", "") + ":" + (t.get("outcome") or "")).lower() in pm_open_keys])
    return len(open_bets_raw)


def render_open_bets(trades, wallet_map):
    open_bets_raw = [t for t in trades if t.get("filled") and t.get("result") is None and not t.get("dry_run")]

    # Cross-reference with PM positions API — only show bets that are actually still open
    pm = fetch_pm_data()
    pm_positions = pm.get("positions", [])
    pm_open_keys = set()
    for p in pm_positions:
        if sf(p.get("size", 0)) > 0:
            key = (p.get("conditionId", "") + ":" + (p.get("outcome") or "")).lower()
            pm_open_keys.add(key)

    if pm_open_keys:
        open_bets = [t for t in open_bets_raw if
                     (t.get("condition_id", "") + ":" + (t.get("outcome") or "")).lower() in pm_open_keys]
    else:
        open_bets = open_bets_raw  # fallback if PM API fails

    if not open_bets:
        return '<div class="empty">Geen open bets.</div>'
    open_bets.sort(key=lambda t: t.get("timestamp") or "", reverse=True)

    # Compute per-wallet actual WR and EV from our resolved trades
    from collections import defaultdict
    wallet_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss"):
            if "up or down" in (t.get("market_title") or "").lower():
                continue
            w = (t.get("copy_wallet") or "").lower()
            if w:
                wallet_perf[w]["pnl"] += t.get("pnl") or 0
                if t["result"] == "win":
                    wallet_perf[w]["wins"] += 1
                else:
                    wallet_perf[w]["losses"] += 1

    rows = ""
    for t in open_bets:
        age = fmt_age(t.get("timestamp"))
        price = t.get("price") or 0
        w = (t.get("copy_wallet") or "").lower()
        perf = wallet_perf.get(w, {"wins": 0, "losses": 0, "pnl": 0.0})
        n = perf["wins"] + perf["losses"]
        our_wr = perf["wins"] / n if n > 0 else 0
        our_ev = perf["pnl"] / n if n > 0 else 0
        wr_color = "#3fb950" if our_wr >= 0.55 else "#f85149" if our_wr < 0.45 else "#bc8cff"
        ev_color = "#3fb950" if our_ev > 0 else "#f85149"
        delay_ms = t.get("signal_delay_ms") or 0
        delay_str = f"{delay_ms/1000:.0f}s" if delay_ms > 0 else "—"
        rows += f"""
        <tr>
          <td><span class="badge sport">{t.get('sport','?')[:8]}</span></td>
          <td class="market-title">{t.get('market_title','?')}</td>
          <td><span class="badge {'green' if t.get('side')=='BUY' else 'red'}">{t.get('side','?')}</span> {t.get('outcome','')}</td>
          <td>{price:.0%}</td>
          <td style="color:{wr_color}">{our_wr:.0%} <span class="muted">({n}t)</span></td>
          <td style="color:{ev_color}">${our_ev:+.2f}</td>
          <td>${t.get('size_usdc',0):.2f}</td>
          <td>{delay_str}</td>
          <td>{render_why(t, wallet_map)}</td>
          <td class="muted">{age}</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Sport</th><th>Market</th><th>Side / Outcome</th>
        <th>Entry</th><th>Our WR</th><th>Our EV</th><th>Size</th>
        <th>Delay</th><th>Waarom</th><th>Leeftijd</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""

def render_wallet_table(stats, wallet_map, compact=False):
    if not stats:
        return '<div class="empty">Geen wallet data.</div>'
    rows = ""
    for i, w in enumerate(stats, 1):
        wr_html = fmt_pct(w["win_rate"])
        pnl_html = fmt_pnl(w["pnl"])
        total = w.get("total_value", 0)
        total_color = "#3fb950" if total >= 0 else "#f85149"
        dim = ' style="opacity:0.5"' if w["trades"] == 0 else ""

        # Record: W-L
        record = f'{w["wins"]}-{w["losses"]}'
        if w.get("tp", 0) > 0:
            record += f' <span class="muted" style="font-size:0.75em">+{w["tp"]}tp</span>'
        if w.get("sold", 0) > 0:
            record += f' <span class="muted" style="font-size:0.75em">+{w["sold"]}s</span>'

        # ROI
        roi = w.get("roi", 0)
        roi_color = "#3fb950" if roi >= 0 else "#f85149"

        # Recent form dots
        form = w.get("recent_form", "")
        form_dots = ""
        for ch in form:
            if ch == "W":
                form_dots += '<span style="color:#3fb950">&#9679;</span>'
            else:
                form_dots += '<span style="color:#f85149">&#9679;</span>'

        # Get per-wallet filter info
        addr = w.get("addr", "")
        winfo = wallet_map.get(addr, {})
        mtypes = ", ".join(winfo.get("market_types", [])) or "all"
        price_range = f'{winfo.get("min_price",0):.0%}-{winfo.get("max_price",1):.0%}' if winfo.get("min_price") else ""

        if compact:
            rows += f"""
        <tr{dim}>
          <td class="muted">{i}</td>
          <td><strong>{w['name']}</strong></td>
          <td class="muted" style="font-size:0.75em">{mtypes}</td>
          <td>{record}</td>
          <td>{wr_html}</td>
          <td>{pnl_html}</td>
          <td style="color:{total_color};font-weight:bold">{"+" if total >= 0 else ""}${total:.0f}</td>
        </tr>"""
        else:
            unr = w.get("unrealized", 0)
            unr_html = f'<span style="color:{"#3fb950" if unr >= 0 else "#f85149"}">{"+" if unr >= 0 else ""}${unr:.0f}</span>'
            rows += f"""
        <tr{dim}>
          <td class="muted">{i}</td>
          <td><strong>{w['name']}</strong></td>
          <td class="muted" style="font-size:0.8em">{mtypes}</td>
          <td>{price_range}</td>
          <td>{w['trades']}</td>
          <td>{record}</td>
          <td>{wr_html}</td>
          <td>${w.get('avg_size',0):.2f}</td>
          <td>{pnl_html}</td>
          <td style="color:{roi_color}">{roi:+.1f}%</td>
          <td style="font-family:monospace;letter-spacing:1px">{form_dots}</td>
          <td>{w.get('open_count',0)}</td>
          <td>{unr_html}</td>
          <td style="color:{total_color};font-weight:bold">{"+" if total >= 0 else ""}${total:.0f}</td>
        </tr>"""

    if compact:
        return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Wallet</th><th>Markets</th><th>Record</th>
        <th>Win%</th><th>P&L</th><th>Totaal</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Wallet</th><th>Markets</th><th>Entry Range</th>
        <th>Signals</th><th>Record</th><th>Win%</th><th>Avg Size</th>
        <th>P&L</th><th>ROI</th><th>Vorm</th>
        <th>Open</th><th>Unreal.</th><th>Totaal</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""

def render_sport_grid(stats):
    if not stats:
        return '<div class="empty">Geen sport data.</div>'
    cards = ""
    for s in stats:
        wr = s["win_rate"]
        wr_bar = f'<div class="mini-bar"><div class="mini-fill" style="width:{wr or 0:.0f}%;background:{"#3fb950" if (wr or 0)>=55 else "#f85149" if (wr or 0)<45 else "#d29922"}"></div></div>' if wr is not None else ""
        pnl_color = "#3fb950" if s["pnl"] >= 0 else "#f85149"
        cards += f"""
        <div class="sport-card">
          <div class="sport-name">{s['sport'].replace('_',' ').title()}</div>
          <div class="sport-stats">
            <span>{s['trades']} bets</span>
            <span style="color:{pnl_color}">{"+" if s['pnl']>=0 else ""}${s['pnl']:.2f}</span>
          </div>
          {wr_bar}
          <div class="muted" style="font-size:0.75rem">
            {'Win: ' + f'{wr:.1f}%' if wr is not None else 'Geen resolved'}
            &nbsp;|&nbsp; Conf avg: {s['avg_conf']:.0%}
          </div>
        </div>"""
    return f'<div class="sport-grid">{cards}</div>'

def render_source_comparison(src_stats):
    def box(label, s, accent):
        wr = f"{s['win_rate']:.1f}%" if s["win_rate"] is not None else "—"
        pnl_s = f'{"+" if s["pnl"]>=0 else ""}${s["pnl"]:.2f}'
        pnl_c = "#3fb950" if s["pnl"] >= 0 else "#f85149"
        extra = f'<div class="stat-row"><span>Gem. edge</span><span>{s["avg_edge"]:.1f}%</span></div>' if label == "Odds Arb" else ""
        return f"""
        <div class="source-box" style="border-top:3px solid {accent}">
          <div class="source-title">{label}</div>
          <div class="stat-row"><span>Bets</span><span>{s['total']}</span></div>
          <div class="stat-row"><span>Win rate</span><span>{wr}</span></div>
          <div class="stat-row"><span>P&L</span><span style="color:{pnl_c}">{pnl_s}</span></div>
          <div class="stat-row"><span>Gem. size</span><span>${s['avg_size']:.2f}</span></div>
          <div class="stat-row"><span>Gem. conf</span><span>{s['avg_conf']:.0%}</span></div>
          {extra}
        </div>"""

    return f"""
    <div class="source-row">
      {box("Copy Trading", src_stats["copy"], "#388bfd")}
      {box("Odds Arb", src_stats["arb"], "#f0883e")}
    </div>"""

def render_pnl_chart(buckets):
    max_abs = max((abs(d["pnl"]) for d in buckets), default=1) or 1
    bars = ""
    for d in buckets:
        pnl = d["pnl"]
        h = max(2, abs(pnl) / max_abs * 60)
        color = "#3fb950" if pnl >= 0 else ("#f85149" if pnl < 0 else "#30363d")
        label = d.get("label", d.get("key", "")[-5:])
        tooltip = f'{d.get("key","")}: {"+"if pnl>=0 else ""}${pnl:.2f}'
        bars += f"""
        <div class="chart-bar-wrap" title="{tooltip}">
          <div class="chart-bar" style="height:{h:.0f}px;background:{color}"></div>
          <div class="chart-label">{label}</div>
        </div>"""
    return f'<div class="pnl-chart">{bars}</div>'

def render_hypotheses(hypotheses):
    if not hypotheses:
        return '<div class="empty">Nog geen autoresearch cycli. Autoresearch draait elke paar uur.</div>'
    items = ""
    for h in hypotheses:
        ts = h.get("timestamp") or h.get("created_at") or ""
        desc = h.get("description") or h.get("hypothesis") or "Onbekend"
        bt = h.get("backtest_result") or h.get("backtest") or {}
        changes = h.get("config_changes") or {}
        deployed = h.get("deployed", False)
        roi_imp = bt.get("roi_improvement") or bt.get("roi_pct") or 0
        wr = bt.get("win_rate") or 0
        n = bt.get("trades") or bt.get("n_trades") or 0
        badge = '<span class="badge" style="background:#3fb950;color:#000">DEPLOYED</span>' if deployed else '<span class="badge" style="background:#8b949e;color:#fff">getest</span>'
        changes_html = ""
        if changes:
            changes_html = f'<div class="hyp-changes"><code>{json.dumps(changes, indent=2)}</code></div>'
        items += f"""
        <div class="hyp-card">
          <div class="hyp-header">
            <span class="muted">{ts[:16] if ts else '?'}</span>
            {badge}
          </div>
          <div class="hyp-desc">{desc}</div>
          <div class="hyp-stats">
            <span>ROI: <strong style="color:{'#3fb950' if roi_imp>=0 else '#f85149'}">{'+'if roi_imp>=0 else ''}{roi_imp:.1f}%</strong></span>
            <span>Win rate: <strong>{wr:.1f}%</strong></span>
            <span>Trades: <strong>{n}</strong></span>
          </div>
          {changes_html}
        </div>"""
    return items

def render_resolved_trades(trades, wallet_map, limit=50):
    """All closed trades — chronological, grouped by event."""
    closed = [t for t in trades if t.get("filled") and not t.get("dry_run")
              and t.get("result") in ("win", "loss", "take_profit", "sold")]
    if not closed:
        return '<div class="empty">Nog geen gesloten trades.</div>'
    # Sort by buy timestamp, newest first
    closed.sort(key=lambda t: t.get("timestamp") or "", reverse=True)
    closed = closed[:limit]

    # Group consecutive trades with same event_slug for visual grouping
    from collections import OrderedDict
    groups = OrderedDict()
    for t in closed:
        slug = t.get("event_slug") or t.get("condition_id") or id(t)
        groups.setdefault(slug, []).append(t)

    rows = ""
    prev_slug = None
    for slug, group in groups.items():
        is_multi = len(group) > 1
        group_pnl = sum(t.get("pnl") or 0 for t in group)
        group_cost = sum(t.get("size_usdc") or 0 for t in group)

        for i, t in enumerate(group):
            pnl = t.get("pnl") or 0
            pnl_html = fmt_pnl(pnl)
            addr = (t.get("copy_wallet") or "").lower()
            info = wallet_map.get(addr, {})
            wallet_name = info.get("name", addr[:10] + "..." if addr else "—")
            ts = (t.get("timestamp") or "")[:16].replace("T", " ")
            price = t.get("price") or 0

            # Visual grouping: top border on first row of new event group
            group_cls = ""
            if is_multi and i == 0:
                group_cls = ' class="group-first"'
            elif is_multi and i > 0:
                group_cls = ' class="group-cont"'

            # Show event badge on first row of multi-bet events
            event_badge = ""
            if is_multi and i == 0:
                event_badge = f' <span class="badge event-group">{len(group)} bets → {fmt_pnl(group_pnl, show_sign=True)}</span>'

            rows += f"""
        <tr{group_cls}>
          <td class="muted" style="white-space:nowrap">{ts}</td>
          <td>{fmt_result(t)}</td>
          <td class="market-title">{t.get('market_title','?')}{event_badge}</td>
          <td>{t.get('outcome','')}</td>
          <td>{price:.0%}</td>
          <td>${t.get('size_usdc',0):.2f}</td>
          <td><strong>{wallet_name}</strong></td>
          <td>{pnl_html}</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Gekocht</th><th>Uitkomst</th><th>Market</th><th>Side</th>
        <th>Entry</th><th>Inzet</th><th>Wallet</th><th>P&L</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def render_evolution_log(dag_entries):
    """Show autoresearch evolution decisions."""
    if not dag_entries:
        return '<div class="empty">Nog geen evolutie beslissingen. Autoresearch draait elke 2 uur.</div>'

    rows = ""
    for e in dag_entries[:30]:
        action = e.get("action", "?")
        name = e.get("wallet_name", "?")
        ts = e.get("timestamp", "")[:16]
        mutation = e.get("mutation_type", "")
        fitness = e.get("portfolio_fitness")
        score = e.get("wallet_score")
        old_w = e.get("old_weight")
        new_w = e.get("new_weight")
        outcome_pnl = e.get("outcome_pnl")

        if action == "add":
            action_html = '<span class="badge green">ADD</span>'
            detail = f'weight={new_w:.2f}' if new_w else ""
        elif action == "remove":
            action_html = '<span class="badge red">REMOVE</span>'
            detail = f'was {old_w:.2f}' if old_w else ""
        elif action == "reweight":
            action_html = '<span class="badge yellow">REWEIGHT</span>'
            detail = f'{old_w:.2f} → {new_w:.2f}' if old_w and new_w else ""
        else:
            action_html = f'<span class="badge">{action}</span>'
            detail = ""

        outcome_html = ""
        if outcome_pnl is not None:
            outcome_html = fmt_pnl(outcome_pnl)
        else:
            outcome_html = '<span class="muted">pending</span>'

        rows += f"""
        <tr>
          <td class="muted">{ts}</td>
          <td>{action_html}</td>
          <td><strong>{name}</strong></td>
          <td>{detail}</td>
          <td class="muted">{mutation}</td>
          <td>{f"{score:.0f}" if score else "—"}</td>
          <td>{f"{fitness:.1f}" if fitness else "—"}</td>
          <td>{outcome_html}</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Wanneer</th><th>Actie</th><th>Wallet</th><th>Detail</th>
        <th>Mutatie</th><th>Score</th><th>Fitness</th><th>Resultaat</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def render_scout_report(scout):
    """Show wallet scout findings."""
    if not scout:
        return '<div class="empty">Geen scout rapport gevonden. Wallet scout draait elk uur.</div>'

    ts = scout.get("timestamp", "")[:16]
    evaluated = scout.get("candidates_evaluated", 0)

    # Top candidates
    adds = scout.get("recommended_additions", [])
    removals = scout.get("recommended_removals", [])
    current = scout.get("current_wallet_scores", [])

    sections = f'<div class="muted" style="margin-bottom:12px">Laatste scan: {ts} | {evaluated} wallets geëvalueerd</div>'

    # Current wallet performance
    if current:
        rows = ""
        for w in current[:15]:
            name = w.get("name", "?")
            score = w.get("score", 0)
            wr = w.get("win_rate", 0)
            sharpe = w.get("sharpe", 0)
            closed = w.get("closed_positions", 0)
            sport = w.get("sport_pct", 0)
            score_color = "#3fb950" if score >= 70 else "#d29922" if score >= 40 else "#f85149"
            rows += f"""
            <tr>
              <td><strong>{name}</strong></td>
              <td style="color:{score_color}">{score:.0f}</td>
              <td>{wr:.0%}</td>
              <td>{sharpe:.2f}</td>
              <td>{sport:.0%}</td>
              <td>{closed}</td>
            </tr>"""
        sections += f"""
        <div style="margin-bottom:16px">
          <div style="font-weight:700;margin-bottom:8px">Huidige Wallets</div>
          <div class="table-wrap"><table>
            <thead><tr><th>Wallet</th><th>Score</th><th>WR</th><th>Sharpe</th><th>Sport%</th><th>Closed</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </div>"""

    # Recommended additions
    if adds:
        rows = ""
        for a in adds:
            rows += f"""
            <tr>
              <td><strong>{a.get("name","?")}</strong></td>
              <td class="green">{a.get("score",0):.0f}</td>
              <td>{a.get("win_rate",0):.0%}</td>
              <td>{a.get("sharpe",0):.2f}</td>
              <td>{a.get("sport_pct",0):.0%}</td>
              <td>{a.get("closed_positions",0)}</td>
            </tr>"""
        sections += f"""
        <div style="margin-bottom:16px">
          <div style="font-weight:700;margin-bottom:8px;color:var(--green)">Aanbevolen Toevoegingen</div>
          <div class="table-wrap"><table>
            <thead><tr><th>Wallet</th><th>Score</th><th>WR</th><th>Sharpe</th><th>Sport%</th><th>Closed</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </div>"""

    # Recommended removals
    if removals:
        rows = ""
        for r in removals:
            rows += f"""
            <tr>
              <td><strong>{r.get("name","?")}</strong></td>
              <td class="red">{r.get("score",0):.0f}</td>
              <td>{r.get("win_rate",0):.0%}</td>
              <td>{r.get("reason","")}</td>
            </tr>"""
        sections += f"""
        <div>
          <div style="font-weight:700;margin-bottom:8px;color:var(--red)">Aanbevolen Verwijderingen</div>
          <div class="table-wrap"><table>
            <thead><tr><th>Wallet</th><th>Score</th><th>WR</th><th>Reden</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </div>"""

    return sections


def render_playbook(playbook_text):
    """Show the LLM-curated playbook."""
    if not playbook_text:
        return '<div class="empty">Nog geen playbook. Curator draait elke 6 uur.</div>'
    # Simple markdown-to-html: lines starting with - become list items
    lines = playbook_text.strip().split("\n")
    html = '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;font-size:0.85rem;line-height:1.6">'
    for line in lines:
        line = line.strip()
        if line.startswith("# "):
            html += f'<div style="font-weight:700;margin:8px 0 4px">{line[2:]}</div>'
        elif line.startswith("- "):
            html += f'<div style="padding-left:12px">• {line[2:]}</div>'
        elif line:
            html += f'<div>{line}</div>'
    html += '</div>'
    return html


def render_all_trades(trades, wallet_map):
    filled = [t for t in trades if t.get("filled")]
    if not filled:
        return '<div class="empty">Nog geen trades.</div>'
    filled.sort(key=lambda t: t.get("timestamp") or "", reverse=True)

    rows = ""
    for t in filled[:200]:  # cap at 200
        conf = t.get("confidence") or 0
        price = t.get("price") or 0
        edge_disp = f'{t["edge_pct"]:+.1f}%' if t.get("edge_pct") else "—"
        rows += f"""
        <tr class="{'dry-row' if t.get('dry_run') else ''}">
          <td class="muted" style="white-space:nowrap">{(t.get('timestamp') or '')[:16]}</td>
          <td><span class="badge sport">{t.get('sport','?')[:8]}</span></td>
          <td class="market-title">{t.get('market_title','?')}</td>
          <td>{t.get('outcome','')}</td>
          <td>{price:.0%}</td>
          <td style="color:#bc8cff">{conf:.0%}</td>
          <td>{edge_disp}</td>
          <td>${t.get('size_usdc',0):.2f}</td>
          <td>{render_why(t, wallet_map)}</td>
          <td>{fmt_result(t)}</td>
          <td>{fmt_pnl(t.get('pnl'))}</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Tijd</th><th>Sport</th><th>Market</th><th>Outcome</th>
        <th>Entry</th><th>Conf</th><th>Edge</th><th>Size</th>
        <th>Waarom</th><th>Resultaat</th><th>P&L</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --muted: #8b949e;
  --green: #3fb950; --red: #f85149; --yellow: #d29922;
  --blue: #388bfd; --orange: #f0883e; --purple: #bc8cff;
}
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
a { color: var(--blue); text-decoration: none; }
.green { color: var(--green); } .red { color: var(--red); }
.yellow { color: var(--yellow); } .muted { color: var(--muted); }

/* Header + Nav */
.header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: 1px; }
.header-right { display: flex; gap: 16px; align-items: center; color: var(--muted); font-size: 0.8rem; }
#countdown { color: var(--yellow); font-family: monospace; }
.nav { display: flex; gap: 0; background: var(--surface); border-bottom: 1px solid var(--border); }
.nav a { padding: 10px 20px; color: var(--muted); font-size: 0.85rem; font-weight: 600; border-bottom: 2px solid transparent; }
.nav a:hover { color: var(--text); background: rgba(255,255,255,0.03); }
.nav a.active { color: var(--blue); border-bottom-color: var(--blue); }
@media(max-width:600px) {
  .nav { flex-wrap: wrap; }
  .nav a { padding: 8px 12px; font-size: 0.75rem; }
  .header { padding: 10px 12px; }
  .header h1 { font-size: 0.95rem; }
  .main { padding: 12px 8px; }
  .kpi-row { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .kpi-value { font-size: 1.1rem; }
  .market-title { max-width: 160px; font-size: 0.8rem; }
  tbody td { padding: 6px 8px; font-size: 0.8rem; }
  thead th { padding: 8px; font-size: 0.65rem; }
  .sport-grid { grid-template-columns: repeat(2, 1fr); }
}
.stop-btn { background: var(--red); color: #fff; border: none; border-radius: 6px; padding: 6px 14px; font-size: 0.8rem; font-weight: 700; cursor: pointer; margin-right: 8px; }
.stop-btn:hover { opacity: 0.8; }

/* Layout */
.main { max-width: 1600px; margin: 0 auto; padding: 20px 24px; }
.section { margin-bottom: 32px; }
.section-title { font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--muted); margin-bottom: 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media(max-width:900px) { .two-col { grid-template-columns: 1fr; } }

/* KPI */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
@media(max-width:900px) { .kpi-row { grid-template-columns: repeat(2,1fr); } }
.kpi-sub { font-size: 0.75rem; color: var(--muted); margin-top: 4px; }
.kpi-tile { background: var(--surface); border-radius: 8px; padding: 16px; }
.kpi-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 8px; }
.kpi-value { font-size: 1.4rem; font-weight: 700; font-family: 'Courier New', monospace; }

/* Goal bar */
.goal-bar-wrap { margin-bottom: 24px; }
.goal-label { font-size: 0.8rem; color: var(--muted); margin-bottom: 6px; }
.goal-bar { height: 8px; background: var(--border); border-radius: 4px; }
.goal-fill { height: 100%; background: linear-gradient(90deg, var(--blue), var(--green)); border-radius: 4px; transition: width 0.5s; }

/* Tables */
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; }
thead th { background: var(--surface); padding: 10px 12px; text-align: left; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; position: sticky; top: 0; }
tbody tr { border-bottom: 1px solid var(--border); }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: rgba(255,255,255,0.03); }
tbody td { padding: 9px 12px; vertical-align: middle; }
.dry-row { opacity: 0.5; text-decoration: line-through; }
.market-title { max-width: 280px; font-size: 0.85rem; }

/* Badges */
.badge { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 0.7rem; font-weight: 600; vertical-align: middle; }
.badge.sport { background: #21262d; color: var(--muted); }
.badge.green { background: var(--green); color: #000; }
.badge.red { background: var(--red); color: #fff; }
.badge.yellow { background: var(--yellow); color: #000; }
.badge.event-group { background: #30363d; color: var(--muted); font-size: 0.65rem; margin-left: 6px; }

/* Event grouping in trade log */
tr.group-first { border-top: 2px solid var(--border); }
tr.group-cont td { padding-top: 2px; padding-bottom: 2px; }
tr.group-cont td:first-child { color: transparent; }
tr.group-cont .market-title { padding-left: 12px; font-size: 0.85em; }

/* Wallet detail cards */
.wallet-detail-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; overflow: hidden; }
.wallet-detail-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.wallet-detail-card table { width: 100%; }
.wallet-detail-card th { background: transparent; }

/* Sport grid */
.sport-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.sport-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.sport-name { font-weight: 700; margin-bottom: 6px; }
.sport-stats { display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 0.85rem; }
.mini-bar { height: 4px; background: var(--border); border-radius: 2px; margin-bottom: 6px; }
.mini-fill { height: 100%; border-radius: 2px; }

/* Source comparison */
.source-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media(max-width:600px) { .source-row { grid-template-columns: 1fr; } }
.source-box { background: var(--surface); border-radius: 8px; padding: 16px; }
.source-title { font-weight: 700; font-size: 1rem; margin-bottom: 12px; }
.stat-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border); font-size: 0.85rem; }
.stat-row:last-child { border-bottom: none; }

/* PnL chart */
.pnl-chart { display: flex; align-items: flex-end; gap: 4px; height: 80px; padding-top: 8px; }
.chart-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: default; }
.chart-bar { width: 100%; border-radius: 2px 2px 0 0; min-height: 2px; transition: opacity 0.2s; }
.chart-bar-wrap:hover .chart-bar { opacity: 0.7; }
.chart-label { font-size: 0.6rem; color: var(--muted); white-space: nowrap; }

/* Hypotheses */
.hyp-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 10px; }
.hyp-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.hyp-desc { font-size: 0.9rem; margin-bottom: 8px; }
.hyp-stats { display: flex; gap: 16px; font-size: 0.8rem; color: var(--muted); margin-bottom: 8px; }
.hyp-changes { background: var(--bg); border-radius: 6px; padding: 8px; margin-top: 8px; }
.hyp-changes code { font-size: 0.75rem; color: var(--muted); white-space: pre; }
.empty { color: var(--muted); padding: 24px; text-align: center; background: var(--surface); border-radius: 8px; border: 1px dashed var(--border); }

/* Game cards */
.game-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 16px; overflow: hidden; }
.game-card-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.game-card-header .game-title { font-weight: 700; font-size: 0.95rem; }
.game-card-header .game-meta { font-size: 0.75rem; color: var(--muted); }
.game-card-header .game-pnl { font-size: 1.1rem; font-weight: 700; font-family: 'Courier New', monospace; }
.game-leg { display: grid; grid-template-columns: 1fr auto auto auto auto; gap: 12px; align-items: center; padding: 10px 16px; border-bottom: 1px solid var(--border); }
.game-leg:last-child { border-bottom: none; }
.game-leg .leg-name { font-size: 0.85rem; }
.game-leg .leg-name .leg-outcome { font-weight: 600; }
.game-leg .leg-prices { font-family: 'Courier New', monospace; font-size: 0.85rem; text-align: right; }
.game-leg .leg-pnl { font-family: 'Courier New', monospace; font-size: 0.9rem; font-weight: 600; text-align: right; min-width: 70px; }
.game-leg .leg-size { font-size: 0.8rem; color: var(--muted); text-align: right; min-width: 60px; }
.sell-btn { background: var(--red); color: #fff; border: none; border-radius: 6px; padding: 6px 14px; font-size: 0.75rem; font-weight: 700; cursor: pointer; white-space: nowrap; }
.sell-btn:hover { opacity: 0.8; }
.sell-btn:disabled { opacity: 0.4; cursor: not-allowed; }
@media(max-width:600px) {
  .game-leg { grid-template-columns: 1fr auto auto; gap: 8px; padding: 8px 12px; }
  .game-leg .leg-prices { font-size: 0.75rem; }
  .game-leg .leg-size { display: none; }
  .game-card-header { padding: 10px 12px; }
}

.score-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.85rem; font-weight:600; margin-left:8px; font-family:monospace; }
.score-badge.live { background:rgba(255,50,50,0.15); color:#ff4444; animation: pulse 2s infinite; }
.score-badge.pre { background:rgba(100,100,100,0.15); color:#888; }
.score-badge.final { background:rgba(100,100,100,0.15); color:#aaa; }
.score-detail { font-size:0.7rem; font-weight:400; opacity:0.7; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.7} }
"""



# -- Live Scores (ESPN API) --

_score_cache = {"data": {}, "ts": 0}

ESPN_SPORTS = {
    "nhl": "hockey/nhl",
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nfl": "football/nfl",
    "cbb": "basketball/mens-college-basketball",
    "cfb": "football/college-football",
}

ESPN_TEAM_MAP = {
    # NHL
    "MTL": "mon", "PHI": "phi", "BOS": "bos", "NJ": "nj", "NYI": "nyi",
    "CAR": "car", "WSH": "wsh", "CBJ": "cbj", "ANA": "ana", "MIN": "min",
    "WPG": "wpg", "UTAH": "utah", "COL": "col", "CGY": "cal", "PIT": "pit",
    "STL": "stl", "LAK": "lak", "VAN": "van", "DAL": "dal", "BUF": "buf",
    "FLA": "fla", "DET": "det", "TB": "tb", "NYR": "nyr", "SEA": "sea",
    "VGK": "las", "OTT": "ott", "TOR": "tor", "NSH": "nsh", "CHI": "chi",
    "SJ": "sj", "EDM": "edm",
    # NBA
    "MIA": "mia", "CHA": "cha", "PHX": "phx", "POR": "por", "LAL": "lal",
    "BKN": "bkn", "NYK": "nyk", "MIL": "mil",
    "CLE": "cle", "IND": "ind", "ATL": "atl", "ORL": "orl",
    "WAS": "was", "HOU": "hou", "MEM": "mem",
    "NOP": "nop", "SAS": "sas", "DEN": "den", "OKC": "okc",
    "UTA": "uta", "GSW": "gsw", "LAC": "lac", "SAC": "sac",
}


def fetch_live_scores():
    """Fetch live scores from ESPN. Cached 30s."""
    now = time.time()
    if _score_cache["data"] and now - _score_cache["ts"] < 30:
        return _score_cache["data"]

    scores = {}

    for league, espn_path in ESPN_SPORTS.items():
        try:
            url = "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard" % espn_path
            req = urllib.request.Request(url, headers={"User-Agent": "Bottie/1"})
            data = json.loads(urllib.request.urlopen(req, timeout=8).read())

            for ev in data.get("events", []):
                comp = ev.get("competitions", [{}])[0]
                teams = comp.get("competitors", [])
                if len(teams) != 2:
                    continue

                status_obj = ev.get("status", {})
                status_type = status_obj.get("type", {})
                status_detail = status_type.get("shortDetail", "")
                status_state = status_type.get("state", "")

                home = teams[0]
                away = teams[1]
                if away.get("homeAway") == "home":
                    home, away = away, home

                home_abbr = home.get("team", {}).get("abbreviation", "?")
                away_abbr = away.get("team", {}).get("abbreviation", "?")
                home_score = home.get("score", "0")
                away_score = away.get("score", "0")

                h_slug = ESPN_TEAM_MAP.get(home_abbr, home_abbr.lower())
                a_slug = ESPN_TEAM_MAP.get(away_abbr, away_abbr.lower())

                score_entry = {
                    "home": home_abbr,
                    "away": away_abbr,
                    "home_score": home_score,
                    "away_score": away_score,
                    "status": status_detail,
                    "state": status_state,
                }
                # Store both orderings for matching
                scores["%s-%s-%s" % (league, a_slug, h_slug)] = score_entry
                scores["%s-%s-%s" % (league, h_slug, a_slug)] = score_entry

        except Exception:
            pass

    _score_cache["data"] = scores
    _score_cache["ts"] = now
    return scores


def match_score(event_slug, scores):
    """Match a PM event_slug to a live score entry."""
    if not event_slug or not scores:
        return None
    slug = event_slug.lower().replace("-more-markets", "")
    parts = slug.split("-")
    if len(parts) >= 3:
        key = "%s-%s-%s" % (parts[0], parts[1], parts[2])
        if key in scores:
            return scores[key]
    return None


def render_score_badge(score):
    """Render a live score badge HTML."""
    if not score:
        return ""
    state = score.get("state", "")
    if state == "pre":
        return '<span class="score-badge pre">%s</span>' % score["status"]
    elif state == "in":
        return (
            '<span class="score-badge live">'
            '\U0001f534 %s %s - %s %s'
            ' <span class="score-detail">%s</span>'
            '</span>'
        ) % (score["away"], score["away_score"], score["home_score"], score["home"], score["status"])
    elif state == "post":
        h_s = score.get("home_score", "0")
        a_s = score.get("away_score", "0")
        winner_home = int(h_s) > int(a_s)
        h_disp = "<b>%s</b>" % h_s if winner_home else h_s
        a_disp = "<b>%s</b>" % a_s if not winner_home else a_s
        return (
            '<span class="score-badge final">'
            '%s %s - %s %s'
            ' <span class="score-detail">FINAL</span>'
            '</span>'
        ) % (score["away"], a_disp, h_disp, score["home"])
    return ""


def build_games_data(trades, funder=None):
    """Build per-game data with live prices for open positions."""
    if funder is None:
        funder = PM_FUNDER
    open_trades = [t for t in trades if t.get("filled") and t.get("result") is None and not t.get("dry_run")]

    pm = fetch_pm_data(funder)
    pm_positions = pm.get("positions", [])

    # Build PM position lookup: conditionId:outcome → position data (with curPrice, size)
    pm_lookup = {}
    for p in pm_positions:
        if sf(p.get("size", 0)) > 0.01:
            key = (p.get("conditionId", "") + ":" + (p.get("outcome") or "")).lower()
            pm_lookup[key] = p

    # Match our trades with PM positions, group by event_slug
    from collections import OrderedDict
    games = OrderedDict()

    for t in open_trades:
        key = (t.get("condition_id", "") + ":" + (t.get("outcome") or "")).lower()
        pm_pos = pm_lookup.get(key)
        if not pm_pos:
            continue  # position no longer exists on PM

        slug = t.get("event_slug") or t.get("market_title", "unknown")[:30]
        entry_price = sf(t.get("price", 0))
        cur_price = sf(pm_pos.get("curPrice", 0))
        shares = sf(pm_pos.get("size", 0))
        cost = sf(pm_pos.get("initialValue", 0)) or (entry_price * shares)
        cur_value = sf(pm_pos.get("currentValue", 0)) or (cur_price * shares)
        token_id = pm_pos.get("asset", t.get("token_id", ""))

        leg = {
            "title": t.get("market_title", "?"),
            "outcome": t.get("outcome", "?"),
            "sport": t.get("sport", "?"),
            "entry_price": entry_price,
            "cur_price": cur_price,
            "shares": shares,
            "cost": cost,
            "cur_value": cur_value,
            "pnl": cur_value - cost,
            "pnl_pct": ((cur_price / entry_price) - 1) * 100 if entry_price > 0 else 0,
            "token_id": token_id,
            "condition_id": t.get("condition_id", ""),
            "age": fmt_age(t.get("timestamp")),
        }

        if slug not in games:
            # Extract game name from slug: league-team1-team2-YYYY-MM-DD
            game_name = slug.replace("-more-markets", "")
            parts = game_name.split("-")
            league = parts[0] if parts else "?"
            # Find date part (YYYY-MM-DD at end)
            date_idx = -1
            for i, p in enumerate(parts):
                if len(p) == 4 and p.isdigit():
                    date_idx = i
                    break
            if date_idx > 0:
                teams = " ".join(p.upper() for p in parts[1:date_idx])
                date_part = "-".join(parts[date_idx:])
            else:
                teams = "-".join(parts[1:])
                date_part = ""
            games[slug] = {
                "slug": slug,
                "league": league,
                "teams": teams,
                "date": date_part,
                "legs": [],
                "total_cost": 0,
                "total_value": 0,
                "total_pnl": 0,
            }

        games[slug]["legs"].append(leg)
        games[slug]["total_cost"] += leg["cost"]
        games[slug]["total_value"] += leg["cur_value"]
        games[slug]["total_pnl"] += leg["pnl"]

    # Attach live scores
    scores = fetch_live_scores()
    for g in games.values():
        g["score"] = match_score(g["slug"], scores)

    return list(games.values())


def render_game_cards(trades, wallet_map, token="", funder=None):
    """Render open positions grouped by game as cards with live P&L and sell buttons."""
    games = build_games_data(trades, funder)

    if not games:
        return '<div class="empty">Geen open posities.</div>'

    # Sort: live games first (largest absolute PnL), then by cost
    games.sort(key=lambda g: -abs(g["total_pnl"]))

    total_cost = sum(g["total_cost"] for g in games)
    total_value = sum(g["total_value"] for g in games)
    total_pnl = total_value - total_cost
    pnl_color = "var(--green)" if total_pnl >= 0 else "var(--red)"

    summary = f"""
    <div class="kpi-row" style="margin-bottom:20px">
      <div class="kpi-tile">
        <div class="kpi-label">Open Games</div>
        <div class="kpi-value">{len(games)}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Deployed</div>
        <div class="kpi-value">${total_cost:.2f}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Huidige Waarde</div>
        <div class="kpi-value">${total_value:.2f}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Unrealized P&L</div>
        <div class="kpi-value" style="color:{pnl_color}">${total_pnl:+.2f}</div>
      </div>
    </div>"""

    prefix = f"/t/{token}" if token else ""
    cards = ""
    for g in games:
        gpnl_color = "var(--green)" if g["total_pnl"] >= 0 else "var(--red)"
        league_badge = f'<span class="badge sport">{g["league"]}</span>'

        legs_html = ""
        for leg in g["legs"]:
            lpnl_color = "var(--green)" if leg["pnl"] >= 0 else "var(--red)"
            arrow = "+" if leg["pnl_pct"] >= 0 else ""
            price_change_color = "var(--green)" if leg["cur_price"] >= leg["entry_price"] else "var(--red)"

            # Sell button — disable if price near 1.00 (awaiting resolution) or near 0 (worthless)
            near_resolution = leg["cur_price"] > 0.95 or leg["cur_price"] < 0.05
            if near_resolution:
                sell_btn = f'<button class="sell-btn" disabled title="Wacht op resolutie">WAIT</button>'
            else:
                sell_btn = (
                    f'<button class="sell-btn" onclick="sellLeg(this, \'{leg["token_id"]}\', {leg["shares"]:.1f}, '
                    f'{leg["cur_price"]:.3f}, \'{leg["outcome"]} {leg["title"][:30]}\')">'
                    f'SELL</button>'
                )

            legs_html += f"""
            <div class="game-leg">
              <div class="leg-name">
                <span class="leg-outcome">{leg["outcome"]}</span>
                <span class="muted" style="font-size:0.8rem"> {leg["title"][:45]}</span>
              </div>
              <div class="leg-prices">
                <span class="muted">{leg["entry_price"]:.0%}</span>
                <span style="color:{price_change_color}"> &rarr; {leg["cur_price"]:.0%}</span>
              </div>
              <div class="leg-pnl" style="color:{lpnl_color}">${leg["pnl"]:+.2f}<br><span style="font-size:0.7rem">{arrow}{leg["pnl_pct"]:.1f}%</span></div>
              <div class="leg-size">{leg["shares"]:.0f}sh<br><span class="muted">${leg["cur_value"]:.2f}</span></div>
              {sell_btn}
            </div>"""

        cards += f"""
        <div class="game-card">
          <div class="game-card-header">
            <div>
              {league_badge}
              <span class="game-title">{g["teams"]}</span>
              <span class="game-meta" style="margin-left:8px">{g["date"]}</span>
              {render_score_badge(g.get("score"))}
            </div>
            <div class="game-pnl" style="color:{gpnl_color}">${g["total_pnl"]:+.2f}</div>
          </div>
          {legs_html}
        </div>"""

    sell_js = """
<script>
async function sellLeg(btn, tokenId, shares, curPrice, label) {
  if (!confirm('Verkoop ' + shares.toFixed(0) + ' shares ' + label + ' @ ' + (curPrice*100).toFixed(0) + 'ct?')) return;
  btn.disabled = true;
  btn.textContent = '...';
  try {
    const resp = await fetch(window.location.pathname.replace(/\\/games.*/, '/sell'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token_id: tokenId, shares: shares, min_price: curPrice * 0.95})
    });
    const data = await resp.json();
    if (data.ok) {
      btn.textContent = 'SOLD $' + data.usdc.toFixed(2);
      btn.style.background = 'var(--green)';
      setTimeout(() => location.reload(), 2000);
    } else {
      alert('Sell failed: ' + data.error);
      btn.disabled = false;
      btn.textContent = 'SELL';
    }
  } catch(e) {
    alert('Error: ' + e);
    btn.disabled = false;
    btn.textContent = 'SELL';
  }
}
</script>"""

    return summary + cards + sell_js


def render_games_page(trades, wallet_map, token="", account="cannae"):
    funder = CRYPTO5M_FUNDER if account == "crypto5m" else PM_FUNDER
    if account == "crypto5m":
        trades = load_trades_crypto5m()
        wallet_map = parse_config_wallets_crypto5m()
    body = f"""
    <div class="section">
      <div class="section-title">Live Games</div>
      {render_game_cards(trades, wallet_map, token=token, funder=funder)}
    </div>"""
    return page_wrap("/games", body, token, account=account)


def page_wrap(active_page, body_html, token="", account="cannae"):
    if CET:
        now_str = datetime.now(CET).strftime("%Y-%m-%d %H:%M %Z")
    else:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prefix = f"/t/{token}" if token else ""
    pages = [
        ("5M", "/5m"),
        ("Settings", "/settings"),
    ]
    nav = ""
    for label, href in pages:
        cls = ' class="active"' if href == active_page else ""
        nav += f'<a href="{prefix}{href}?account={account}"{cls}>{label}</a>'

    account_selector = ""

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  {"" if active_page in ("/settings", "/games") else '<meta http-equiv="refresh" content="30">'}
  {"" if active_page != "/games" else '<meta http-equiv="refresh" content="15">'}
  <title>Bottie — {active_page}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="header">
  <h1>BOTTIE</h1>
  <div class="header-right">
    <button onclick="location.reload()" style="background:var(--blue);color:#fff;border:none;border-radius:6px;padding:6px 14px;font-size:0.8rem;font-weight:600;cursor:pointer;margin-right:8px">&#x21BB;</button>
    {account_selector}
    <span style="margin-left:8px">{now_str}</span>
    {"" if active_page == "/settings" else '<span id="countdown">30</span>s'}
  </div>
</div>
<div class="nav">{nav}</div>
<div class="main">{body_html}</div>
<script>
{"" if active_page == "/settings" else "let t = 30; const el = document.getElementById('countdown'); if (el) setInterval(() => { el.textContent = --t; if(t<=0) location.reload(); }, 1000);"}
</script>
</body>
</html>"""


def _load_recent_game_modes(service="bottie", since="24 hours ago"):
    """Parse 'GAME MODE' / 'GAME SKIP' lines from journal. Returns counts + recent list."""
    import subprocess, re
    counts = {}
    recent = []  # list of (ts, mode, slug)
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "--since", since, "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=15
        )
        # Pattern: 2026-04-07T01:23:46+0000 vultr bottie-bin[14323]: ... GAME MODE: <slug> → <mode>
        # Or: ... GAME SKIP: <slug> — Cannae game total $X < min $Y
        for line in result.stdout.splitlines():
            if "GAME MODE:" in line:
                m = re.search(r"GAME MODE:\s+(\S+)\s*[→—-]\s*(.+?)(?:\s+\(|\s*$)", line)
                if m:
                    slug, mode = m.group(1), m.group(2).strip()
                    # Categorize mode
                    if "3-LEG" in mode:
                        cat = "3-LEG"
                    elif "OPP_NO" in mode:
                        cat = "OPP_NO"
                    elif "WIN_NO+DRAW_YES" in mode:
                        cat = "WIN_NO+DRAW_YES (legacy)"
                    elif "WIN_YES+DRAW_NO" in mode:
                        cat = "WIN_YES+DRAW_NO (legacy)"
                    elif "DRAW_YES_ONLY" in mode:
                        cat = "DRAW_YES_ONLY"
                    elif "WIN_NO" in mode:
                        cat = "WIN_NO solo"
                    elif "WIN_YES" in mode:
                        cat = "WIN_YES (banned-fallback)"
                    else:
                        cat = "OTHER"
                    counts[cat] = counts.get(cat, 0) + 1
                    ts = line.split()[0][:16].replace("T", " ")
                    recent.append((ts, cat, slug, mode))
            elif "GAME SKIP:" in line and "Cannae total game CV" in line:
                counts["SKIP (dust)"] = counts.get("SKIP (dust)", 0) + 1
    except Exception:
        pass
    return counts, recent[-20:]  # last 20 events


def render_game_mode_panel(service="bottie"):
    counts, recent = _load_recent_game_modes(service)
    total = sum(counts.values())

    counts_html = ""
    if counts:
        for cat in sorted(counts, key=lambda k: -counts[k]):
            n = counts[cat]
            color = "#3fb950" if "3-LEG" in cat else "#388bfd" if "WIN_NO solo" in cat else "#d29922" if "SKIP" in cat else "#8b949e"
            counts_html += f'<div class="stat-row"><span style="color:{color}">{cat}</span><span><b>{n}</b></span></div>'
    else:
        counts_html = '<div class="muted">Geen game modes in laatste 24h</div>'

    recent_html = ""
    if recent:
        rows = ""
        for ts, cat, slug, full_mode in reversed(recent):
            color = "#3fb950" if "3-LEG" in cat else "#388bfd" if "WIN_NO solo" in cat else "#8b949e"
            rows += f'<tr><td class="muted" style="font-size:0.75rem">{ts}</td><td style="color:{color};font-weight:600">{cat}</td><td style="font-family:monospace;font-size:0.75rem">{slug}</td></tr>'
        recent_html = f'<table class="data-table" style="margin-top:8px"><thead><tr><th>Tijd</th><th>Mode</th><th>Game</th></tr></thead><tbody>{rows}</tbody></table>'
    else:
        recent_html = '<div class="muted">Nog geen events sinds restart</div>'

    return f"""
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;margin-top:12px">
      <div style="font-weight:700;margin-bottom:8px">Recente GAME MODES (24h, totaal: {total})</div>
      {counts_html}
      <div style="margin-top:12px;font-weight:600;font-size:0.85rem">Laatste 20 events:</div>
      {recent_html}
    </div>"""


def render_football_rules_panel(config):
    """Show the football decision tree."""
    ss = config.get("sport_sizing", {})
    min_stakes = ss.get("min_cannae_game_usdc", {}) or {}
    if min_stakes:
        min_str = f"${list(min_stakes.values())[0]:.0f}" if len(set(min_stakes.values())) == 1 else "varies"
        leagues = ", ".join(sorted(min_stakes.keys()))
    else:
        min_str = "geen"
        leagues = "—"

    rules_html = """
    <table class="data-table" style="margin-top:8px;font-size:0.82rem">
      <thead><tr><th>Cannae heeft</th><th>Bottie koopt</th><th>Trigger</th></tr></thead>
      <tbody>
        <tr><td>WIN_NO_A + DRAW_YES</td><td><b style="color:#3fb950">3-LEG</b>: WIN_NO_A 5% + DRAW_YES 2.5% + WIN_NO_B 2.5%</td><td>draw_yes/win_no ≥ 5%</td></tr>
        <tr><td>WIN_YES_A + DRAW_NO</td><td><b style="color:#3fb950">3-LEG</b>: WIN_YES_A 5% (exempt) + DRAW_NO 2.5% + WIN_NO_B 2.5%</td><td>draw_no/win_yes ≥ 5%</td></tr>
        <tr><td>WIN_NO_A solo (geen draw of dust)</td><td>WIN_NO_A confidence sized</td><td>—</td></tr>
        <tr><td>WIN_YES_A + DRAW_YES (price ≥0.55)</td><td>OPP_NO substitute (1 leg)</td><td>legacy</td></tr>
        <tr><td>WIN_YES_A + DRAW_YES (price &lt;0.55)</td><td>DRAW_YES alleen</td><td>legacy</td></tr>
        <tr><td>WIN_YES_A + DRAW_NO (price &lt;0.50)</td><td>OPP_NO substitute</td><td>legacy</td></tr>
        <tr><td>WIN_YES_A solo</td><td>OPP_NO substitute (banned WIN_YES)</td><td>legacy</td></tr>
      </tbody>
    </table>"""

    return f"""
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;margin-top:12px">
      <div style="font-weight:700;margin-bottom:8px">Football decision tree</div>
      <div class="stat-row"><span>Min Cannae game stake</span><span><b>{min_str}</b></span></div>
      <div class="muted" style="font-size:0.75rem">Toegepast op: {leagues}</div>
      {rules_html}
      <div class="muted" style="font-size:0.75rem;margin-top:8px">Cap: 10% per game (5+2.5+2.5 = 10%) | win_yes_ban: actief (3-LEG case 1 exempt)</div>
    </div>"""


def render_strategy_summary(wallet_map):
    """Show current strategy info: wallets, filters, sizing."""
    cards = ""
    wallet_colors = {"cannae": "#388bfd", "sovereign": "#3fb950", "ewelmealt": "#d29922"}
    for addr, info in wallet_map.items():
        name = info.get("name", addr[:10])
        mtypes = ", ".join(info.get("market_types", [])) or "all"
        min_p = info.get("min_price", 0)
        max_p = info.get("max_price", 1)
        color = wallet_colors.get(name.lower(), "#8b949e")
        leagues_str = ", ".join(info.get("leagues", [])) or "all"
        if len(leagues_str) > 60:
            leagues_str = leagues_str[:57] + "..."
        cards += f"""
        <div class="source-box" style="border-top:3px solid {color}">
          <div class="source-title">{name}</div>
          <div class="stat-row"><span>Markets</span><span>{mtypes}</span></div>
          <div class="stat-row"><span>Leagues</span><span style="font-size:0.75em">{leagues_str}</span></div>
          <div class="stat-row"><span>Entry range</span><span>{min_p:.0%} - {max_p:.0%}</span></div>
          <div class="stat-row"><span>Adres</span><span class="muted" style="font-size:0.75em">{addr[:10]}...{addr[-6:]}</span></div>
        </div>"""

    # Read sizing from config
    try:
        config = _load_config()
        ss = config.get("sport_sizing", {})
        risk = config.get("risk", {})
        sizing_lines = f"""Voetbal ML: {ss.get('voetbal_ml_pct', '?')}% | Draw: {ss.get('voetbal_draw_pct', '?')}%<br>
        NBA ML: {ss.get('nba_ml_pct', '?')}% | Spread: {ss.get('nba_spread_pct', '?')}%<br>
        NHL ML: {ss.get('nhl_ml_pct', '?')}% | Min bet: ${ss.get('min_bet_usdc', '?')}"""
        max_open = risk.get("max_open_bets", "?")
        max_deploy = risk.get("max_deployment_pct", "?")
    except Exception:
        sizing_lines = "Config niet beschikbaar"
        max_open = "?"
        max_deploy = "?"

    sizing_info = f"""
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;margin-top:12px">
      <div style="font-weight:700;margin-bottom:8px">Sizing (proportioneel: leg_weight × conviction × max_pct)</div>
      <div style="font-family:monospace;font-size:0.85rem;color:var(--purple)">
        {sizing_lines}
      </div>
      <div class="muted" style="font-size:0.8rem;margin-top:4px">Max {max_open} open bets | {max_deploy}% deployment cap | Taker mode</div>
    </div>"""

    # Football decision tree + min stake panel
    try:
        config = _load_config()
        football_panel = render_football_rules_panel(config)
    except Exception:
        football_panel = ""

    # Recent game modes panel (live from journal)
    try:
        game_modes_panel = render_game_mode_panel("bottie")
    except Exception:
        game_modes_panel = ""

    return f"""
    <div class="source-row">{cards}</div>
    {sizing_info}
    {football_panel}
    {game_modes_panel}"""


def _load_cannae_slugs(service="bottie"):
    """Parse game slugs from bot logs — all SIGNAL lines from any wallet."""
    import subprocess
    cannae_slugs = {}
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "--since", "60 min ago", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            # Legacy: $   3538 | 7 legs (win+win+ou+ou+spread) | 15:15 UTC | win@5%+win@5%+draw@5% | es2-vld-bur-2026-03-28
            if "legs (" in line and "|" in line:
                parts = line.split("|")
                if len(parts) >= 5:
                    slug = parts[-1].strip()
                    try:
                        amount = float(parts[0].split("$")[1].strip())
                    except:
                        amount = 0
                    legs = parts[1].strip()
                    sizing = parts[3].strip() if len(parts) > 3 else ""
                    cannae_slugs[slug] = {"amount": amount, "legs": legs, "sizing": sizing, "wallet": "unknown"}
            # New: SIGNAL: WalletName (79ct) | Will X win? | 123$ | ...
            elif "SIGNAL:" in line and "|" in line:
                try:
                    sig_part = line.split("SIGNAL:")[1].strip()
                    parts = sig_part.split("|")
                    wallet_price = parts[0].strip()  # "NoSpreader (79ct)"
                    wallet_name = wallet_price.split("(")[0].strip()
                    question = parts[1].strip() if len(parts) > 1 else ""
                    amount_str = parts[2].strip() if len(parts) > 2 else "0"
                    try:
                        amount = float(amount_str.replace("$", "").strip())
                    except:
                        amount = 0
                    # Extract slug from question or use event_slug from later in the line
                    slug = ""
                    for p in parts:
                        p = p.strip()
                        if "-" in p and len(p.split("-")) >= 3 and "20" in p:
                            slug = p
                            break
                    if not slug:
                        # Try to find slug in the full line
                        import re
                        m = re.search(r'delay=\d+s\s*$', line)
                        # Can't always get slug from SIGNAL, skip if not found
                        continue
                    if slug not in cannae_slugs:
                        cannae_slugs[slug] = {"amount": amount, "legs": question[:40], "sizing": "", "wallet": wallet_name}
                    else:
                        # Append wallet name if different
                        existing = cannae_slugs[slug].get("wallet", "")
                        if wallet_name not in existing:
                            cannae_slugs[slug]["wallet"] = f"{existing}+{wallet_name}"
                except:
                    pass
    except:
        pass
    return cannae_slugs

def render_live_board(trades, service="bottie"):
    """Live flight board — upcoming games in whitelisted leagues with wallet info."""
    import os
    svc_base = Path("/opt/bottie-test") if service == "bottie-test" else BASE_DIR
    schedule_file = svc_base / "data" / "schedule_cache.json"

    # Whitelisted leagues + wallet mapping for ALL active wallets (weight > 0)
    cfg_file = CRYPTO5M_CONFIG_FILE if service == "bottie-test" else CONFIG_FILE
    whitelisted_leagues = set()
    wallet_league_map = {}  # league -> [wallet_names]
    try:
        import yaml
        with open(cfg_file) as f:
            _cfg = yaml.safe_load(f) or {}
        _wl = _cfg.get("copy_trading", {}).get("watchlist", [])
        for w in _wl:
            if w.get("weight", 0) > 0:
                for lg in (w.get("leagues") or []):
                    whitelisted_leagues.add(lg)
                    wallet_league_map.setdefault(lg, []).append(w.get("name", "?"))
    except Exception:
        pass

    games = []
    if schedule_file.exists():
        games = json.load(open(schedule_file))

    now = datetime.now(timezone.utc)

    # Show ALL upcoming games in whitelisted leagues (next 24h)
    upcoming = []
    for g in games:
        slug = g.get("event_slug", "")
        if not slug or "-more-markets" in slug:
            continue
        league = slug.split("-")[0] if slug else ""
        if whitelisted_leagues and league not in whitelisted_leagues:
            continue
        try:
            start = datetime.fromisoformat(g["start_time"].replace("Z", "+00:00"))
        except:
            continue
        diff_min = (start - now).total_seconds() / 60
        if diff_min < 0 or diff_min >= 24*60:
            continue
        # Which wallet covers this league?
        wallets = wallet_league_map.get(league, ["?"])
        upcoming.append((slug, g, start, diff_min, {"wallet": ", ".join(wallets)}))

    upcoming.sort(key=lambda x: x[2])

    # Whitelist header — show active wallets with their leagues
    if whitelisted_leagues:
        # Build wallet -> leagues mapping
        wallet_leagues = {}
        for lg, wallets in wallet_league_map.items():
            for w in wallets:
                wallet_leagues.setdefault(w, []).append(lg)
        wallet_badges = ""
        for wname, wleagues in sorted(wallet_leagues.items()):
            league_list = ", ".join(sorted(wleagues))
            wallet_badges += f'<span style="margin:2px 6px 2px 0;font-size:11px"><b>{wname}</b>: {league_list}</span>'
        wl_header = f"""
        <div style="margin-bottom:8px;font-size:12px">
          <span class="muted">Active wallets:</span> {wallet_badges}
        </div>"""
    else:
        wl_header = ""

    if not upcoming:
        return wl_header + '<div class="empty">Geen games in de pipeline.</div>'

    # Check which event_slugs already have fills
    filled_slugs = set()
    for t in trades:
        if t.get("filled") and not t.get("dry_run") and t.get("event_slug"):
            filled_slugs.add(t["event_slug"])

    rows = ""
    for slug, sched, start, diff_min, info in upcoming[:30]:
        title = sched.get("title", slug) if sched else slug
        league = slug.split("-")[0] if slug else ""

        if diff_min < 0:
            status = f'<span style="color:#3fb950">LIVE {abs(diff_min):.0f}min</span>'
        elif diff_min < 10:
            status = f'<span style="color:#f85149;font-weight:700">T-{diff_min:.0f}min!</span>'
        elif diff_min < 30:
            status = f'<span style="color:#f0883e">T-{diff_min:.0f}min</span>'
        elif diff_min < 60:
            status = f'<span style="color:#d29922">T-{diff_min:.0f}min</span>'
        else:
            hours = diff_min / 60
            status = f'<span class="muted">{hours:.1f}h</span>'

        if CET:
            cet = start.astimezone(CET)
        else:
            cet = start + timedelta(hours=1)
        time_str = cet.strftime("%H:%M")

        is_filled = slug in filled_slugs
        fill_badge = ' <span class="badge green">FILLED</span>' if is_filled else ""
        row_style = 'opacity:0.5' if is_filled else ''

        wallet_name = info.get("wallet", "?")
        wallet_badge = f'<span class="badge" style="background:#1a3a2a;color:#3fb950;font-size:11px">{wallet_name}</span>'

        rows += f"""
        <tr style="{row_style}">
          <td style="font-weight:600">{time_str}</td>
          <td><span class="badge sport">{league}</span></td>
          <td>{title}{fill_badge}</td>
          <td>{wallet_badge}</td>
          <td>{status}</td>
        </tr>"""

    return f"""
    {wl_header}
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>CET</th><th>League</th><th>Game</th><th>Wallet</th><th>Status</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def render_bot_health(trades):
    """Bot health bar — last activity, uptime indicator."""
    import os

    # Last trade timestamp
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    last_trade_ts = max((t.get("timestamp", "") for t in filled), default="") if filled else ""

    # Last trades.jsonl modification
    trades_file = BASE_DIR / "data" / "trades.jsonl"
    trades_age_min = 999
    if trades_file.exists():
        trades_age_min = (time.time() - os.path.getmtime(trades_file)) / 60

    # Schedule cache age = proxy for "bot is polling"
    schedule_file = BASE_DIR / "data" / "schedule_cache.json"
    sched_age_min = 999
    if schedule_file.exists():
        sched_age_min = (time.time() - os.path.getmtime(schedule_file)) / 60

    # Health status
    # Schedule refreshes every 60min, trades.jsonl updates on fills
    activity_age = min(sched_age_min, trades_age_min)
    if activity_age < 70:
        health = '<span style="color:#3fb950;font-weight:700">● ONLINE</span>'
    elif activity_age < 120:
        health = '<span style="color:#d29922;font-weight:700">● IDLE</span>'
    else:
        health = '<span style="color:#f85149;font-weight:700">● OFFLINE</span>'

    # Last trade age
    if last_trade_ts:
        try:
            lt = datetime.fromisoformat(last_trade_ts.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - lt
            if age.total_seconds() < 3600:
                trade_age = f"{age.total_seconds()/60:.0f}min geleden"
            else:
                trade_age = f"{age.total_seconds()/3600:.1f}h geleden"
        except:
            trade_age = last_trade_ts[:16]
    else:
        trade_age = "—"

    return f"""
    <div style="display:flex;gap:24px;align-items:center;padding:8px 16px;background:#161b22;border-radius:8px;margin-bottom:16px;font-size:13px">
      <span>{health}</span>
      <span class="muted">Laatste poll: {sched_age_min:.0f}min</span>
      <span class="muted">Laatste trade: {trade_age}</span>
      <span class="muted">Schedule: {sched_age_min:.0f}min oud</span>
    </div>"""


def render_daily_pnl(trades):
    """Daily P&L breakdown — last 14 days."""
    from collections import defaultdict

    filled = [t for t in trades if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss", "take_profit", "sold")]

    by_day = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "invested": 0.0})
    for t in filled:
        resolved = t.get("resolved_at") or t.get("timestamp") or ""
        day = resolved[:10]
        if not day: continue
        by_day[day]["trades"] += 1
        by_day[day]["pnl"] += t.get("actual_pnl") if t.get("actual_pnl") is not None else (t.get("pnl") or 0)
        by_day[day]["invested"] += t.get("size_usdc") or 0
        if t.get("result") in ("win", "take_profit"):
            by_day[day]["wins"] += 1
        elif t.get("result") == "loss":
            by_day[day]["losses"] += 1

    if not by_day:
        return '<div class="empty">Geen resolved trades.</div>'

    # Sort by day descending, last 14 days
    days = sorted(by_day.keys(), reverse=True)[:14]

    cum_pnl = 0
    # Calculate cumulative (need forward order)
    all_days_asc = sorted(by_day.keys())
    cum_by_day = {}
    running = 0
    for d in all_days_asc:
        running += by_day[d]["pnl"]
        cum_by_day[d] = running

    rows = ""
    for day in days:
        d = by_day[day]
        pnl = d["pnl"]
        cum = cum_by_day.get(day, 0)
        roi = (pnl / d["invested"] * 100) if d["invested"] > 0 else 0
        pnl_color = "#3fb950" if pnl >= 0 else "#f85149"
        cum_color = "#3fb950" if cum >= 0 else "#f85149"
        wr = d["wins"] / d["trades"] * 100 if d["trades"] > 0 else 0
        wr_color = "#3fb950" if wr >= 55 else "#f85149" if wr < 45 else "#d29922"

        # Bar width (proportional, max 200px)
        bar_width = min(200, abs(pnl) / 5)  # $5 per pixel
        bar_color = "#3fb950" if pnl >= 0 else "#f85149"
        bar = f'<div style="display:inline-block;height:12px;width:{bar_width}px;background:{bar_color};border-radius:2px"></div>'

        rows += f"""
        <tr>
          <td style="font-weight:600">{day[5:]}</td>
          <td>{d["trades"]}</td>
          <td style="color:{wr_color}">{d["wins"]}W/{d["losses"]}L ({wr:.0f}%)</td>
          <td style="color:{pnl_color};font-weight:600">${pnl:+.0f}</td>
          <td>{bar}</td>
          <td style="color:{cum_color}">${cum:+.0f}</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Dag</th><th>Trades</th><th>W/L</th><th>P&L</th><th></th><th>Cum.</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def render_cannae_intel():
    """Cannae intelligence summary from quant analysis report."""
    report_file = BASE_DIR / "repo" / "research" / "cannae_quant_analysis" / "report.json"
    # Also try relative path from /opt/bottie
    if not report_file.exists():
        report_file = Path("/opt/bottie/repo/research/cannae_quant_analysis/report.json")
    if not report_file.exists():
        return '<div class="empty">Geen Cannae rapport gevonden.</div>'

    try:
        r = json.load(open(report_file))
    except:
        return '<div class="empty">Cannae rapport onleesbaar.</div>'

    gen = (r.get("generated_at") or "?")[:16]
    dr = r.get("data_range", {})
    overall = r.get("overall", {})

    # Market type tiles
    mt_html = ""
    for k in ["win", "draw", "spread", "ou", "btts"]:
        mt = r.get("by_market_type", {}).get(k, {})
        if not mt: continue
        wr = mt.get("wr", 0) * 100
        roi = mt.get("roi", 0) * 100
        pnl = mt.get("pnl", 0)
        bets = mt.get("bets", 0)
        wr_color = "#3fb950" if wr >= 60 else "#d29922" if wr >= 50 else "#f85149"
        roi_color = "#3fb950" if roi >= 10 else "#d29922" if roi >= 0 else "#f85149"
        mt_html += f"""
        <div class="kpi-tile" style="border-top:3px solid {wr_color};flex:1">
          <div class="kpi-label">{k.upper()}</div>
          <div class="kpi-value" style="color:{wr_color}">{wr:.0f}% WR</div>
          <div class="kpi-sub" style="color:{roi_color}">ROI {roi:+.0f}% | {bets} bets</div>
        </div>"""

    # Top leagues
    leagues = r.get("by_league", {})
    league_rows = ""
    for lg, d in sorted(leagues.items(), key=lambda x: -x[1].get("pnl", 0))[:10]:
        wr = d.get("wr", 0) * 100
        roi = d.get("roi", 0) * 100
        pnl = d.get("pnl", 0)
        bets = d.get("bets", 0)
        wr_color = "#3fb950" if wr >= 60 else "#f85149"
        league_rows += f"""
        <tr>
          <td><span class="badge sport">{lg}</span></td>
          <td>{bets}</td>
          <td style="color:{wr_color}">{wr:.0f}%</td>
          <td style="color:{"#3fb950" if roi > 0 else "#f85149"}">{roi:+.0f}%</td>
          <td style="color:{"#3fb950" if pnl > 0 else "#f85149"}">${pnl:,.0f}</td>
        </tr>"""

    edge = r.get("edge_decay", {})
    trend = edge.get("trend", "?")
    trend_color = "#3fb950" if trend == "improving" else "#f85149" if trend == "declining" else "#d29922"

    return f"""
    <div style="margin-bottom:8px">
      <span class="muted">Data: {dr.get("from","?")} — {dr.get("to","?")} | {r.get("resolved_bets",0)} bets | Updated: {gen}</span>
    </div>
    <div class="kpi-row">{mt_html}</div>
    <div style="margin:12px 0">
      <span>Edge trend: </span><span style="color:{trend_color};font-weight:600">{trend}</span>
    </div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>League</th><th>Bets</th><th>WR</th><th>ROI</th><th>PnL</th></tr></thead>
      <tbody>{league_rows}</tbody>
    </table>
    </div>"""


def render_crypto5m_section():
    """Render Crypto 5M (bottie-test) KPI row for the overview page."""
    crypto5m_trades    = load_trades_crypto5m()
    crypto5m_wallets   = parse_config_wallets_crypto5m()
    crypto5m_kpis      = compute_kpis(crypto5m_trades, funder=CRYPTO5M_FUNDER)
    crypto5m_open      = count_real_open_bets(crypto5m_trades, CRYPTO5M_FUNDER)

    html = render_kpi_row(crypto5m_kpis, crypto5m_wallets, crypto5m_trades, funder=CRYPTO5M_FUNDER, label="Crypto 5M (bottie-test)")

    # Bot health: last STATUS line from bottie-test logs
    try:
        import subprocess
        log = subprocess.run(
            ["journalctl", "-u", "bottie-test", "--no-pager", "-n", "200"],
            capture_output=True, text=True, timeout=5
        ).stdout
        status_lines = [l for l in log.splitlines() if "STATUS:" in l]
        last_status = status_lines[-1] if status_lines else ""
        svc_color = "#3fb950" if last_status else "#f85149"
        status_snippet = last_status.split("STATUS:")[-1].strip()[:120] if last_status else "geen STATUS log"
        html += f'<div style="margin:6px 0 12px 0;font-size:0.8rem;color:{svc_color}">● bottie-test &nbsp;<span class="muted">{status_snippet}</span></div>'
    except Exception:
        pass

    return html


def render_overview(trades, wallet_map, account="cannae"):
    kpis = compute_kpis(trades, funder=PM_FUNDER, initial_bankroll=INITIAL_BANKROLL)
    sport_stats = compute_sport_stats(trades)

    body = render_bot_health(trades)
    body += render_kpi_row(kpis, wallet_map, trades, funder=PM_FUNDER, label="Bottie")
    body += f"""
    <div class="section">
      <div class="section-title">Open Bets ({count_real_open_bets(trades)})</div>
      {render_open_bets(trades, wallet_map)}
    </div>
    <div class="section">
      <div class="section-title">Live Board</div>
      {render_live_board(trades, service="bottie")}
    </div>
    <div class="section">
      <div class="section-title">Dagelijkse P&L</div>
      {render_daily_pnl(trades)}
    </div>
    <div class="section">
      <div class="section-title">Per Sport</div>
      {render_sport_grid(sport_stats)}
    </div>
    <div class="section">
      <div class="section-title">Trade Log (laatste 30)</div>
      {render_resolved_trades(trades, wallet_map, limit=30)}
    </div>"""
    return page_wrap("/", body, account=account)


def render_trades_page(trades, wallet_map, account="cannae"):
    kpis = compute_kpis(trades)
    real_open = count_real_open_bets(trades)
    body = f"""
    <div class="section">
      <div class="section-title">Open Bets ({real_open})</div>
      {render_open_bets(trades, wallet_map)}
    </div>
    <div class="section">
      <div class="section-title">Trade Log (laatste 50)</div>
      {render_resolved_trades(trades, wallet_map, limit=50)}
    </div>
    <div class="section">
      <div class="section-title">Alle Trades (laatste 200)</div>
      {render_all_trades(trades, wallet_map)}
    </div>"""
    return page_wrap("/trades", body, account=account)


def render_wallet_detail(trades, wallet_map, stats):
    """Per-wallet trade breakdown — shows last 10 trades per wallet."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    # Group by wallet
    by_wallet = {}
    for t in filled:
        addr = (t.get("copy_wallet") or "").lower() or "_manual"
        by_wallet.setdefault(addr, []).append(t)

    # Only show wallets that are in config or have trades
    cards = ""
    for w in stats:
        addr = w["addr"]
        group = by_wallet.get(addr, [])
        if not group:
            continue
        group.sort(key=lambda t: t.get("timestamp") or "", reverse=True)
        recent = group[:10]

        # Header stats
        pnl = w["pnl"]
        pnl_color = "#3fb950" if pnl >= 0 else "#f85149"
        wr = w["win_rate"]

        trade_rows = ""
        for t in recent:
            price = t.get("price") or 0
            ts = (t.get("timestamp") or "")[:16].replace("T", " ")
            trade_rows += f"""
              <tr>
                <td class="muted" style="white-space:nowrap;font-size:0.8em">{ts}</td>
                <td style="font-size:0.85em">{t.get('market_title','?')[:50]}</td>
                <td>{t.get('outcome','')}</td>
                <td>{price:.0%}</td>
                <td>${t.get('size_usdc',0):.2f}</td>
                <td>{fmt_result(t)}</td>
                <td>{fmt_pnl(t.get('pnl'))}</td>
              </tr>"""

        # Form dots
        form = w.get("recent_form", "")
        form_html = ""
        for ch in form:
            color = "#3fb950" if ch == "W" else "#f85149"
            form_html += f'<span style="color:{color};font-size:1.1em">&#9679;</span>'

        # Get per-wallet filter info for detail card
        winfo = wallet_map.get(addr, {})
        mtypes_str = ", ".join(winfo.get("market_types", [])) or "all"
        price_range_str = f'{winfo.get("min_price",0):.0%}-{winfo.get("max_price",1):.0%}' if winfo.get("min_price") else ""

        cards += f"""
        <div class="wallet-detail-card">
          <div class="wallet-detail-header">
            <div>
              <strong style="font-size:1.05em">{w['name']}</strong>
              <span class="muted" style="margin-left:8px;font-size:0.8em">{mtypes_str} | {price_range_str}</span>
            </div>
            <div style="display:flex;gap:16px;align-items:center">
              <span>{form_html}</span>
              <span>{w['wins']}-{w['losses']}</span>
              <span>{f'{wr:.0f}%' if wr is not None else '—'}</span>
              <span style="color:{pnl_color};font-weight:bold">{"+" if pnl >= 0 else ""}${pnl:.2f}</span>
              <span class="muted">ROI {w['roi']:+.1f}%</span>
            </div>
          </div>
          <table style="font-size:0.85em">
            <thead><tr>
              <th>Tijd</th><th>Market</th><th>Side</th><th>Entry</th><th>Size</th><th>Result</th><th>P&L</th>
            </tr></thead>
            <tbody>{trade_rows}</tbody>
          </table>
        </div>"""

    return cards


def render_wallets_page(trades, wallet_map, account="cannae"):
    wallet_stats = compute_wallet_stats(trades, wallet_map)
    body = f"""
    <div class="section">
      <div class="section-title">Wallet Performance</div>
      {render_wallet_table(wallet_stats, wallet_map)}
    </div>
    <div class="section">
      <div class="section-title">Per Wallet Detail</div>
      {render_wallet_detail(trades, wallet_map, wallet_stats)}
    </div>"""
    return page_wrap("/wallets", body, account=account)


def render_research_page(account="cannae"):
    dag_entries = load_dag()
    playbook = load_playbook()
    scout = load_scout_report()
    body = f"""
    <div class="section">
      <div class="section-title">Evolutie Log (autoresearch)</div>
      {render_evolution_log(dag_entries)}
    </div>
    <div class="section">
      <div class="section-title">Playbook (LLM Curator)</div>
      {render_playbook(playbook)}
    </div>"""
    return page_wrap("/research", body, account=account)


# ── Consensus Page ────────────────────────────────────────────────────────────

def load_consensus_config():
    """Parse consensus-specific config from config.yaml."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        import yaml
        with open(CONFIG_FILE) as f:
            config = yaml.safe_load(f)
        ct = config.get("copy_trading", {})
        cons = ct.get("consensus", {})
        watchlist = ct.get("watchlist", [])
        return {
            "min_traders": cons.get("min_traders", 1),
            "window_minutes": cons.get("window_minutes", 120),
            "multiplier_2": cons.get("multiplier_2", 1.5),
            "multiplier_3plus": cons.get("multiplier_3plus", 2.0),
            "batch_size": ct.get("batch_size", 4),
            "warm_poll_interval": ct.get("warm_poll_interval_seconds", 60),
            "poll_interval": ct.get("poll_interval_seconds", 15),
            "watchlist_count": len(watchlist),
            "active_wallets": len([w for w in watchlist if (w.get("weight") or 0) > 0]),
        }
    except Exception:
        return {}


def load_consensus_bulk():
    if not CONSENSUS_BULK.exists():
        return None
    try:
        return json.loads(CONSENSUS_BULK.read_text())
    except Exception:
        return None


def load_consensus_results():
    if not CONSENSUS_RESULTS.exists():
        return None
    try:
        return json.loads(CONSENSUS_RESULTS.read_text())
    except Exception:
        return None


def compute_consensus_split(trades):
    """Split trades by consensus_count: solo (1) vs consensus (2+)."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    resolved = [t for t in filled if t.get("result") in ("win", "loss", "take_profit", "sold")]

    solo = [t for t in resolved if (t.get("consensus_count") or 1) < 2]
    consensus = [t for t in resolved if (t.get("consensus_count") or 1) >= 2]

    def stats(group, label):
        wins = [t for t in group if t.get("result") in ("win", "take_profit")]
        pnl = sum(t.get("pnl") or 0 for t in group)
        wr = len(wins) / len(group) * 100 if group else 0
        avg_size = sum(t.get("size_usdc") or 0 for t in group) / len(group) if group else 0
        return {
            "label": label,
            "count": len(group),
            "wins": len(wins),
            "losses": len(group) - len(wins),
            "wr": wr,
            "pnl": pnl,
            "avg_size": avg_size,
        }

    return {
        "solo": stats(solo, "Solo (1 wallet)"),
        "consensus": stats(consensus, "Consensus (2+ wallets)"),
        "total_resolved": len(resolved),
    }


def render_consensus_config_tiles(cfg):
    """Config tiles for consensus settings."""
    if not cfg:
        return '<div class="empty">Config niet beschikbaar.</div>'

    mode = "CONSENSUS" if cfg.get("min_traders", 1) >= 2 else "SOLO (legacy)"
    mode_color = "#3fb950" if cfg.get("min_traders", 1) >= 2 else "#d29922"

    tiles = [
        ("Modus", mode, mode_color,
         f'min_traders = {cfg.get("min_traders", 1)}'),
        ("Wallets", f'{cfg.get("active_wallets", 0)}', "#388bfd",
         f'van {cfg.get("watchlist_count", 0)} in config'),
        ("Window", f'{cfg.get("window_minutes", 30)} min', "#bc8cff",
         f'consensus bets ouder → geprund'),
        ("Polling", f'{cfg.get("poll_interval", 15)}s / {cfg.get("warm_poll_interval", 60)}s', "#8b949e",
         f'hot / warm | batch={cfg.get("batch_size", 8)}'),
    ]

    html = ""
    for label, value, color, sub in tiles:
        html += f"""
        <div class="kpi-tile" style="border-top:3px solid {color}">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{sub}</div>
        </div>"""
    return f'<div class="kpi-row">{html}</div>'


def render_consensus_vs_solo(split):
    """Side-by-side comparison of solo vs consensus trades."""
    def box(data, accent):
        wr = f"{data['wr']:.1f}%" if data['count'] else "—"
        pnl_color = "#3fb950" if data["pnl"] >= 0 else "#f85149"
        pnl_str = f'{"+" if data["pnl"] >= 0 else ""}${data["pnl"]:.2f}'
        return f"""
        <div class="source-box" style="border-top:3px solid {accent}">
          <div class="source-title">{data['label']}</div>
          <div class="stat-row"><span>Trades</span><span>{data['count']}</span></div>
          <div class="stat-row"><span>Record</span><span>{data['wins']}-{data['losses']}</span></div>
          <div class="stat-row"><span>Win Rate</span><span>{wr}</span></div>
          <div class="stat-row"><span>P&L</span><span style="color:{pnl_color}">{pnl_str}</span></div>
          <div class="stat-row"><span>Avg Size</span><span>${data['avg_size']:.2f}</span></div>
        </div>"""

    return f"""
    <div class="source-row">
      {box(split["solo"], "#f85149")}
      {box(split["consensus"], "#3fb950")}
    </div>"""


def render_consensus_signals(trades, wallet_map):
    """Show recent trades that had consensus (2+ wallets). Filterable by consensus count."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")
              and (t.get("consensus_count") or 1) >= 2]
    if not filled:
        return '<div class="empty">Nog geen consensus trades. Wacht op 2+ wallets die dezelfde markt kiezen.</div>'

    def extract_resolve_date(trade):
        """Extract resolve date from market title or event_slug."""
        import re
        title = trade.get("market_title") or ""
        slug = trade.get("event_slug") or ""
        # Match YYYY-MM-DD in title or slug
        for text in [title, slug]:
            m = re.search(r'(20\d{2}-\d{2}-\d{2})', text)
            if m:
                return m.group(1)
        # Match "March 16" etc
        months = {"january":"01","february":"02","march":"03","april":"04","may":"05","june":"06",
                  "july":"07","august":"08","september":"09","october":"10","november":"11","december":"12"}
        for month_name, month_num in months.items():
            m = re.search(month_name + r'\s+(\d{1,2})', title.lower())
            if m:
                return "2026-%s-%02d" % (month_num, int(m.group(1)))
        return ""

    # Add resolve_date to each trade for sorting
    for t in filled:
        t["_resolve_date"] = extract_resolve_date(t)

    # Sort: open trades with soonest resolve date first, then by timestamp
    filled.sort(key=lambda t: (
        0 if t.get("result") is None else 1,  # open first
        t.get("_resolve_date") or "9999",       # soonest resolve first
        -(t.get("consensus_count") or 1),       # highest consensus first within same date
    ))

    # Count per consensus level for filter buttons
    from collections import Counter
    cc_counts = Counter((t.get("consensus_count") or 1) for t in filled)
    filter_buttons = '<div style="margin-bottom:12px;display:flex;gap:6px;flex-wrap:wrap">'
    filter_buttons += '<button class="cc-filter active" onclick="filterCC(0)" style="cursor:pointer;padding:4px 12px;border-radius:12px;border:1px solid var(--border);background:var(--blue);color:#fff;font-size:0.8rem">All (%d)</button>' % len(filled)
    for cc_val in sorted(cc_counts.keys()):
        filter_buttons += '<button class="cc-filter" onclick="filterCC(%d)" style="cursor:pointer;padding:4px 12px;border-radius:12px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:0.8rem">%d wallets (%d)</button>' % (cc_val, cc_val, cc_counts[cc_val])
    filter_buttons += '</div>'

    rows = ""
    for t in filled[:100]:
        pnl = t.get("pnl")
        result_html = fmt_result(t)
        pnl_html = fmt_pnl(pnl) if pnl is not None else '<span class="muted">open</span>'
        ts = (t.get("timestamp") or "")[:16].replace("T", " ")
        cc = t.get("consensus_count") or 1
        cc_color = "#3fb950" if cc >= 3 else "#d29922"

        # Show consensus wallet names
        cw = t.get("consensus_wallets") or []
        if cw:
            wallets_html = ", ".join(f'<strong>{w[:15]}</strong>' for w in cw[:5])
        else:
            # Fallback: show copy_wallet if available
            cw_name = (t.get("copy_wallet") or "")[:15]
            wallets_html = f'<strong>{cw_name}</strong>' if cw_name else '<span class="muted">?</span>'

        resolve = t.get("_resolve_date") or ""
        resolve_short = resolve[5:] if resolve else "?"  # MM-DD
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        resolve_color = "#3fb950" if resolve and resolve <= today_str else "#d29922" if resolve and resolve <= tomorrow_str else "#8b949e"

        rows += f"""
        <tr data-cc="{cc}">
          <td style="color:{resolve_color};white-space:nowrap;font-weight:600">{resolve_short}</td>
          <td><span class="badge" style="background:{cc_color};color:#000;cursor:pointer" onclick="filterCC({cc})">{cc} wallets</span></td>
          <td><span class="badge sport">{t.get('sport','?')[:8]}</span></td>
          <td class="market-title">{t.get('market_title','?')}</td>
          <td>{t.get('outcome','')}</td>
          <td>{t.get('price',0):.0%}</td>
          <td>${t.get('size_usdc',0):.2f}</td>
          <td style="font-size:0.8rem">{wallets_html}</td>
          <td>{result_html}</td>
          <td>{pnl_html}</td>
        </tr>"""

    return f"""
    {filter_buttons}
    <div class="table-wrap">
    <table id="consensus-table">
      <thead><tr>
        <th>Resolves</th><th>Consensus</th><th>Sport</th><th>Market</th>
        <th>Side</th><th>Entry</th><th>Size</th><th>Traders</th><th>Resultaat</th><th>P&L</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
    <script>
    function filterCC(n) {{
      document.querySelectorAll('.cc-filter').forEach(b => {{
        b.style.background = 'var(--surface)';
        b.style.color = 'var(--text)';
        b.classList.remove('active');
      }});
      if (n === 0) {{
        document.querySelectorAll('.cc-filter')[0].style.background = 'var(--blue)';
        document.querySelectorAll('.cc-filter')[0].style.color = '#fff';
      }} else {{
        document.querySelectorAll('.cc-filter').forEach(b => {{
          if (b.textContent.startsWith(n + ' wallets')) {{
            b.style.background = 'var(--blue)';
            b.style.color = '#fff';
          }}
        }});
      }}
      document.querySelectorAll('#consensus-table tbody tr').forEach(row => {{
        if (n === 0) {{
          row.style.display = '';
        }} else {{
          row.style.display = row.dataset.cc == String(n) ? '' : 'none';
        }}
      }});
    }}
    </script>"""


def render_consensus_pool(bulk):
    """Show wallet discovery pool from consensus_bulk.json."""
    if not bulk:
        return '<div class="empty">Geen discovery data. Draai: <code>python research/consensus/prepare.py</code></div>'

    ts = bulk.get("timestamp", "")[:16]
    wallets = bulk.get("wallets", [])

    # Apply filters matching prepare.py
    valid = [w for w in wallets
             if w.get("closed_count", 0) >= 10
             and w.get("both_sides_ratio", 1) <= 0.15
             and w.get("last_activity_days", 999) <= 2
             and w.get("sport_pct", 0) >= 0.30
             and w.get("win_rate", 0) >= 0.50]
    valid.sort(key=lambda w: w.get("win_rate", 0) * max(w.get("sharpe", 0.01), 0.01), reverse=True)

    rows = ""
    for i, w in enumerate(valid[:30], 1):
        wr = w.get("win_rate", 0)
        wr_color = "#3fb950" if wr >= 0.60 else "#d29922" if wr >= 0.50 else "#f85149"
        sharpe = w.get("sharpe", 0)
        sharpe_color = "#3fb950" if sharpe >= 0.3 else "#d29922" if sharpe >= 0 else "#f85149"
        rows += f"""
        <tr>
          <td class="muted">{i}</td>
          <td><strong>{w.get('name','?')[:20]}</strong></td>
          <td style="color:{wr_color}">{wr:.0%}</td>
          <td style="color:{sharpe_color}">{sharpe:.2f}</td>
          <td>{w.get('sport_pct',0):.0%}</td>
          <td>{w.get('top_sport','?')}</td>
          <td>{w.get('closed_count',0)}</td>
          <td>{w.get('both_sides_ratio',0):.0%}</td>
          <td>{w.get('last_activity_days','?')}d</td>
        </tr>"""

    return f"""
    <div class="muted" style="margin-bottom:12px">
      Scan: {ts} | {len(wallets)} totaal | {len(valid)} na filters
    </div>
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Wallet</th><th>WR</th><th>Sharpe</th>
        <th>Sport%</th><th>Top</th><th>Closed</th><th>Both Sides</th><th>Activiteit</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def render_consensus_pairs(results):
    """Show top consensus pairs from score.py results."""
    if not results:
        return '<div class="empty">Geen pair data. Draai: <code>python research/consensus/score.py</code></div>'

    pairs = results.get("top_pairs", [])
    if not pairs:
        return '<div class="empty">Geen pairs met consensus gevonden.</div>'

    rows = ""
    for i, p in enumerate(pairs[:15], 1):
        wr = p.get("consensus_wr")
        wr_str = f"{wr:.0%}" if wr is not None else "—"
        wr_color = "#3fb950" if wr and wr >= 0.55 else "#f85149" if wr and wr < 0.45 else "#d29922"
        score_color = "#3fb950" if p.get("score", 0) >= 2 else "#d29922"
        rows += f"""
        <tr>
          <td class="muted">{i}</td>
          <td><strong>{p.get('names','?')}</strong></td>
          <td>{p.get('shared_events',0)}</td>
          <td>{p.get('agreement_rate',0):.0%}</td>
          <td style="color:{wr_color}">{wr_str}</td>
          <td>{p.get('consensus_total',0)}</td>
          <td style="color:{score_color}">{p.get('score',0):.1f}</td>
        </tr>"""

    return f"""
    <div class="muted" style="margin-bottom:12px">
      {len(pairs)} pairs met consensus | {results.get('valid_wallets',0)} wallets geanalyseerd
    </div>
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Pair</th><th>Shared Events</th><th>Agreement</th>
        <th>Consensus WR</th><th>Trades</th><th>Score</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def render_consensus_portfolios(results):
    """Show recommended portfolios from score.py."""
    if not results:
        return ""
    portfolios = results.get("portfolios", [])
    if not portfolios:
        return ""

    cards = ""
    for i, p in enumerate(portfolios[:3], 1):
        cons_wr = p.get("consensus_wr")
        wr_str = f"{cons_wr:.0%}" if cons_wr else "—"
        wr_color = "#3fb950" if cons_wr and cons_wr >= 0.55 else "#d29922"

        wallet_list = ""
        for w in p.get("wallets", []):
            wr = w.get("wr", 0)
            wallet_list += f'<div style="display:flex;justify-content:space-between;padding:2px 0;font-size:0.85em">'
            wallet_list += f'<span><strong>{w.get("name","?")[:18]}</strong></span>'
            wallet_list += f'<span style="color:{"#3fb950" if wr >= 0.55 else "#d29922"}">{wr:.0%}</span>'
            wallet_list += f'<span class="muted">{w.get("sport","?")}</span>'
            wallet_list += f'</div>'

        cards += f"""
        <div class="source-box" style="border-top:3px solid {"#3fb950" if i == 1 else "#388bfd" if i == 2 else "#8b949e"}">
          <div class="source-title">Portfolio #{i} <span class="muted" style="font-weight:400;font-size:0.8em">score={p.get('score',0):.0f}</span></div>
          <div class="stat-row"><span>Wallets</span><span>{p.get('size',0)}</span></div>
          <div class="stat-row"><span>Active Pairs</span><span>{p.get('active_pairs',0)}</span></div>
          <div class="stat-row"><span>Consensus WR</span><span style="color:{wr_color}">{wr_str}</span></div>
          <div class="stat-row"><span>Consensus Trades</span><span>{p.get('consensus_wins',0)}/{p.get('consensus_events',0)}</span></div>
          <div style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
            {wallet_list}
          </div>
        </div>"""

    return f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px">{cards}</div>'


def compute_edge_by_bracket(trades):
    """Compute edge per price bracket from trades.jsonl (self-calculated PnL)."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    # Filter: result != phantom, timestamp >= 2026-03-17
    resolved = [t for t in filled if t.get("result") in ("win", "loss")
                and (t.get("timestamp") or "") >= "2026-03-17"]

    brackets = [
        ("0-20ct", 0.0, 0.20),
        ("20-40ct", 0.20, 0.40),
        ("40-60ct", 0.40, 0.60),
        ("60-80ct", 0.60, 0.80),
        ("80-100ct", 0.80, 1.01),
    ]
    results = []
    for label, lo, hi in brackets:
        group = [t for t in resolved if lo <= (t.get("price") or 0) < hi]
        if not group:
            results.append({"label": label, "count": 0, "wins": 0, "wr": 0, "edge": 0, "pnl": 0, "avg_price": 0})
            continue
        wins = [t for t in group if t.get("result") == "win"]
        # Self-calculated PnL
        pnl = 0.0
        for t in group:
            price = t.get("price") or 0
            shares = t.get("shares") or (t.get("size_usdc", 0) / price if price > 0 else 0)
            if t.get("result") == "win":
                pnl += shares * (1.0 - price)
            else:
                pnl -= t.get("size_usdc") or (shares * price)
        wr = len(wins) / len(group) if group else 0
        avg_price = sum(t.get("price") or 0 for t in group) / len(group)
        edge = wr - avg_price  # edge = actual WR - implied probability
        results.append({
            "label": label, "count": len(group), "wins": len(wins),
            "wr": wr, "edge": edge, "pnl": pnl, "avg_price": avg_price,
        })
    return results


def compute_edge_by_market_type(trades):
    """Compute edge per market type (win/ml/draw/ou/spread)."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    resolved = [t for t in filled if t.get("result") in ("win", "loss")
                and (t.get("timestamp") or "") >= "2026-03-17"]

    by_type = {}
    for t in resolved:
        title = (t.get("market_title") or "").lower()
        if "draw" in title:
            mtype = "draw"
        elif "over" in title or "under" in title:
            mtype = "over/under"
        elif "spread" in title or "handicap" in title:
            mtype = "spread"
        elif "moneyline" in title or " ml " in title:
            mtype = "moneyline"
        else:
            mtype = "win"
        by_type.setdefault(mtype, []).append(t)

    results = []
    for mtype, group in sorted(by_type.items()):
        wins = [t for t in group if t.get("result") == "win"]
        pnl = 0.0
        for t in group:
            price = t.get("price") or 0
            shares = t.get("shares") or (t.get("size_usdc", 0) / price if price > 0 else 0)
            if t.get("result") == "win":
                pnl += shares * (1.0 - price)
            else:
                pnl -= t.get("size_usdc") or (shares * price)
        wr = len(wins) / len(group) if group else 0
        avg_price = sum(t.get("price") or 0 for t in group) / len(group)
        results.append({
            "type": mtype, "count": len(group), "wins": len(wins),
            "wr": wr, "edge": wr - avg_price, "pnl": pnl,
        })
    return results


def compute_edge_by_league(trades):
    """Compute edge per league/sport."""
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    resolved = [t for t in filled if t.get("result") in ("win", "loss")
                and (t.get("timestamp") or "") >= "2026-03-17"]

    by_league = {}
    for t in resolved:
        league = t.get("sport") or "unknown"
        by_league.setdefault(league, []).append(t)

    results = []
    for league, group in sorted(by_league.items()):
        wins = [t for t in group if t.get("result") == "win"]
        pnl = 0.0
        for t in group:
            price = t.get("price") or 0
            shares = t.get("shares") or (t.get("size_usdc", 0) / price if price > 0 else 0)
            if t.get("result") == "win":
                pnl += shares * (1.0 - price)
            else:
                pnl -= t.get("size_usdc") or (shares * price)
        wr = len(wins) / len(group) if group else 0
        avg_price = sum(t.get("price") or 0 for t in group) / len(group)
        results.append({
            "league": league, "count": len(group), "wins": len(wins),
            "wr": wr, "edge": wr - avg_price, "pnl": pnl,
        })
    results.sort(key=lambda x: x["pnl"], reverse=True)
    return results


def render_edge_table(bracket_data, col_name="Bracket"):
    """Generic edge table renderer."""
    rows = ""
    total_trades = sum(b["count"] for b in bracket_data)
    total_pnl = sum(b["pnl"] for b in bracket_data)
    for b in bracket_data:
        key = b.get("label") or b.get("type") or b.get("league") or "?"
        cnt = b["count"]
        if cnt == 0:
            rows += f'<tr><td>{key}</td><td class="muted">0</td><td colspan="5" class="muted">geen data</td></tr>'
            continue
        wr = b["wr"]
        edge = b["edge"]
        pnl = b["pnl"]
        wr_color = "#3fb950" if wr >= 0.55 else "#f85149" if wr < 0.45 else "#d29922"
        edge_color = "#3fb950" if edge > 0.02 else "#f85149" if edge < -0.02 else "#d29922"
        pnl_color = "#3fb950" if pnl >= 0 else "#f85149"
        ci_note = "&#10003;" if cnt >= 30 else "&#9888;" if cnt >= 10 else "&#10007;"
        ci_color = "#3fb950" if cnt >= 30 else "#d29922" if cnt >= 10 else "#f85149"
        rows += f"""
        <tr>
          <td><strong>{key}</strong></td>
          <td>{cnt}</td>
          <td>{b['wins']}-{cnt - b['wins']}</td>
          <td style="color:{wr_color}">{wr:.1%}</td>
          <td style="color:{edge_color}">{edge:+.1%}</td>
          <td style="color:{pnl_color}">{"+" if pnl >= 0 else ""}${pnl:.2f}</td>
          <td style="color:{ci_color}">{ci_note}</td>
        </tr>"""
    # Total row
    total_wr = sum(b["wins"] for b in bracket_data) / total_trades if total_trades else 0
    total_avg_price = sum(b.get("avg_price", 0) * b["count"] for b in bracket_data) / total_trades if total_trades else 0
    pnl_color = "#3fb950" if total_pnl >= 0 else "#f85149"
    rows += f"""
    <tr style="border-top:2px solid var(--border);font-weight:700">
      <td>TOTAAL</td><td>{total_trades}</td>
      <td>{sum(b['wins'] for b in bracket_data)}-{total_trades - sum(b['wins'] for b in bracket_data)}</td>
      <td>{total_wr:.1%}</td><td></td>
      <td style="color:{pnl_color}">{"+" if total_pnl >= 0 else ""}${total_pnl:.2f}</td><td></td>
    </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>{col_name}</th><th>Trades</th><th>Record</th><th>Win Rate</th>
        <th>Edge</th><th>P&L (calc)</th><th>CI</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
    <div class="muted" style="font-size:0.75rem;margin-top:6px">
      &#10003; = 30+ trades (betrouwbaar) | &#9888; = 10-29 (indicatief) | &#10007; = &lt;10 (onbetrouwbaar)
      &nbsp;|&nbsp; P&L = zelf berekend (shares × price), niet het pnl-veld
      &nbsp;|&nbsp; Edge = WR − gemiddelde entry price
      &nbsp;|&nbsp; Filter: result=win/loss, timestamp ≥ 2026-03-17
    </div>"""


def render_edge_report_summary():
    """Show last edge_analysis_report.md summary if available."""
    if not EDGE_REPORT_FILE.exists():
        return '<div class="empty">Geen edge_analysis_report.md gevonden. Draai scripts/edge_analysis.py op de VPS.</div>'
    try:
        text = EDGE_REPORT_FILE.read_text()
        # Show first 60 lines max
        lines = text.strip().split("\n")[:60]
        preview = "\n".join(lines)
        return f"""
        <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;font-size:0.82rem;line-height:1.6;max-height:400px;overflow-y:auto">
          <pre style="white-space:pre-wrap;color:var(--text);margin:0">{preview}</pre>
        </div>"""
    except Exception:
        return '<div class="empty">Kon edge rapport niet laden.</div>'


def render_edge_page(trades, wallet_map, account="cannae"):
    """Edge analytics page — price brackets, market types, leagues."""
    bracket_data = compute_edge_by_bracket(trades)
    type_data = compute_edge_by_market_type(trades)
    league_data = compute_edge_by_league(trades)

    body = f"""
    <div class="section">
      <div class="section-title">Strategie Config</div>
      {render_strategy_summary(wallet_map)}
    </div>
    <div class="section">
      <div class="section-title">Edge per Price Bracket</div>
      {render_edge_table(bracket_data, "Bracket")}
    </div>
    <div class="two-col">
      <div class="section">
        <div class="section-title">Edge per Market Type</div>
        {render_edge_table(type_data, "Type")}
      </div>
      <div class="section">
        <div class="section-title">Edge per League/Sport</div>
        {render_edge_table(league_data, "League")}
      </div>
    </div>
    <div class="section">
      <div class="section-title">Edge Analysis Report (VPS script output)</div>
      {render_edge_report_summary()}
    </div>"""

    return page_wrap("/edge", body, account=account)


def render_ops_page(trades, wallet_map, account="cannae"):
    """Operations health page — data freshness, anomalies, PnL validation."""
    import os

    # Data freshness
    freshness_items = []
    trades_file = BASE_DIR / "data" / "trades.jsonl"
    if trades_file.exists():
        mtime = os.path.getmtime(trades_file)
        age_h = (time.time() - mtime) / 3600
        color = "#3fb950" if age_h < 1 else "#d29922" if age_h < 24 else "#f85149"
        freshness_items.append(("trades.jsonl", f"{age_h:.1f}h oud", color))
    else:
        freshness_items.append(("trades.jsonl", "NIET GEVONDEN", "#f85149"))

    cannae_dir = BASE_DIR / "research" / "cannae_trades"
    if cannae_dir.exists():
        csvs = list(cannae_dir.glob("*.csv"))
        if csvs:
            newest = max(os.path.getmtime(str(f)) for f in csvs)
            age_h = (time.time() - newest) / 3600
            color = "#3fb950" if age_h < 6 else "#d29922" if age_h < 24 else "#f85149"
            freshness_items.append(("cannae_trades/*.csv", f"{age_h:.1f}h oud ({len(csvs)} bestanden)", color))
        else:
            freshness_items.append(("cannae_trades/*.csv", "GEEN CSVs", "#f85149"))
    else:
        freshness_items.append(("cannae_trades/", "DIR NIET GEVONDEN", "#f85149"))

    edge_report = BASE_DIR / "data" / "edge_analysis_report.md"
    if edge_report.exists():
        age_h = (time.time() - os.path.getmtime(str(edge_report))) / 3600
        color = "#3fb950" if age_h < 12 else "#d29922" if age_h < 48 else "#f85149"
        freshness_items.append(("edge_analysis_report.md", f"{age_h:.1f}h oud", color))

    freshness_html = ""
    for fname, status, color in freshness_items:
        freshness_html += f"""
        <div class="stat-row">
          <span>{fname}</span>
          <span style="color:{color};font-weight:600">{status}</span>
        </div>"""
    freshness_section = f"""
    <div class="source-box" style="border-top:3px solid #388bfd">
      <div class="source-title">Data Versheid</div>
      {freshness_html}
    </div>"""

    # Anomaly detection from trades
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    resolved = [t for t in filled if t.get("result") in ("win", "loss", "take_profit", "sold")]

    # Filter recent (last 7 days)
    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    recent = [t for t in filled if (t.get("timestamp") or "") >= cutoff_7d]
    recent_resolved = [t for t in resolved if (t.get("resolved_at") or t.get("timestamp") or "") >= cutoff_7d]

    # Large trades (>$30)
    large_trades = [t for t in recent if (t.get("size_usdc") or 0) > 30]

    # Phantom fills (last 7d)
    phantoms = [t for t in trades if t.get("result") == "phantom" and (t.get("timestamp") or "") >= cutoff_7d]

    # Draw trades (title contains "draw")
    draw_trades = [t for t in recent if "draw" in (t.get("market_title") or "").lower()]

    anomaly_items = []
    if large_trades:
        anomaly_items.append((f"{len(large_trades)} trades &gt;$30", "#f85149", "overschrijding sizing"))
    if len(phantoms) > 3:
        anomaly_items.append((f"{len(phantoms)} phantom fills (7d)", "#f85149", "spike in phantom detectie"))
    elif phantoms:
        anomaly_items.append((f"{len(phantoms)} phantom fills (7d)", "#d29922", "normaal"))
    if draw_trades:
        draw_pnl = sum(t.get("pnl") or 0 for t in draw_trades if t.get("result") in ("win", "loss"))
        anomaly_items.append((f"{len(draw_trades)} draw trades (7d)", "#f85149", f"P&L: ${draw_pnl:.2f}"))
    if not anomaly_items:
        anomaly_items.append(("Geen anomalieën gedetecteerd", "#3fb950", "alles normaal"))

    anomaly_html = ""
    for msg, color, detail in anomaly_items:
        anomaly_html += f"""
        <div class="stat-row">
          <span style="color:{color}">{msg}</span>
          <span class="muted">{detail}</span>
        </div>"""
    anomaly_section = f"""
    <div class="source-box" style="border-top:3px solid #f0883e">
      <div class="source-title">Anomalieën (7d)</div>
      {anomaly_html}
    </div>"""

    # PnL self-calculated vs reported
    cutoff_17 = "2026-03-17"
    valid = [t for t in resolved if (t.get("timestamp") or "") >= cutoff_17 and t.get("result") in ("win", "loss")]
    calc_pnl = 0.0
    reported_pnl = 0.0
    for t in valid:
        price = t.get("price") or 0
        shares = t.get("shares") or (t.get("size_usdc", 0) / price if price > 0 else 0)
        if t.get("result") == "win":
            calc_pnl += shares * (1.0 - price)
        else:
            calc_pnl -= t.get("size_usdc") or (shares * price)
        reported_pnl += t.get("pnl") or 0

    diff = abs(calc_pnl - reported_pnl)
    diff_color = "#3fb950" if diff < 1 else "#d29922" if diff < 5 else "#f85149"

    pnl_section = f"""
    <div class="source-box" style="border-top:3px solid #bc8cff">
      <div class="source-title">PnL Validatie (vanaf 2026-03-17)</div>
      <div class="stat-row"><span>Trades gevalideerd</span><span>{len(valid)}</span></div>
      <div class="stat-row"><span>Zelf berekend</span><span style="color:{"#3fb950" if calc_pnl >= 0 else "#f85149"}">{"+" if calc_pnl >= 0 else ""}${calc_pnl:.2f}</span></div>
      <div class="stat-row"><span>Gerapporteerd (pnl veld)</span><span style="color:{"#3fb950" if reported_pnl >= 0 else "#f85149"}">{"+" if reported_pnl >= 0 else ""}${reported_pnl:.2f}</span></div>
      <div class="stat-row"><span>Verschil</span><span style="color:{diff_color}">${diff:.2f}</span></div>
    </div>"""

    # Quick stats tiles
    wins_7d = [t for t in recent_resolved if t.get("result") in ("win", "take_profit")]
    wr_7d = len(wins_7d) / len(recent_resolved) * 100 if recent_resolved else 0
    pnl_7d = sum(t.get("pnl") or 0 for t in recent_resolved)
    trades_per_day = len(recent) / 7.0 if recent else 0

    tiles_html = ""
    tiles = [
        ("Trades (7d)", str(len(recent)), "#388bfd", f"{trades_per_day:.1f}/dag"),
        ("Resolved (7d)", str(len(recent_resolved)), "#388bfd", f"{len(wins_7d)}W-{len(recent_resolved)-len(wins_7d)}L"),
        ("WR (7d)", f"{wr_7d:.1f}%", "#3fb950" if wr_7d >= 55 else "#f85149", f"van {len(recent_resolved)} trades"),
        ("P&L (7d)", f'{"+" if pnl_7d >= 0 else ""}${pnl_7d:.2f}', "#3fb950" if pnl_7d >= 0 else "#f85149", "gerapporteerd pnl-veld"),
    ]
    for label, value, color, sub in tiles:
        tiles_html += f"""
        <div class="kpi-tile" style="border-top:3px solid {color}">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{sub}</div>
        </div>"""

    body = f"""
    <div class="kpi-row">{tiles_html}</div>
    <div class="source-row" style="margin-bottom:24px">
      {freshness_section}
      {anomaly_section}
    </div>
    <div class="section">
      {pnl_section}
    </div>"""

    return page_wrap("/ops", body, account=account)


# ── Settings Page ─────────────────────────────────────────────────────────────

WITHDRAW_WALLETS = {
    "Koen": "0x87af7B1D1E76d218816313653a16183c9fa884a9",
    "Liesbeth": "0xa6B2c1c45048998729411eEf1e3001e59364D8B3",
}


def render_health_page(token="", account="cannae"):
    """Render health dashboard: drift check + healthcheck + wallet status."""
    import json
    from pathlib import Path

    drift_data = {}
    health_data = {}
    baselines = {}

    try:
        with open("data/drift_report.json") as f:
            drift_data = json.load(f)
    except:
        pass

    try:
        with open("data/healthcheck.json") as f:
            health_data = json.load(f)
    except:
        pass

    try:
        with open("data/wallet_baselines.json") as f:
            baselines = json.load(f)
    except:
        pass

    # Drift section
    drift_html = '<div class="section"><div class="section-title">Drift Check</div>'
    if not drift_data:
        drift_html += '<div class="empty">Geen drift data. Eerste check draait om 06:00 UTC.</div>'
    else:
        drift_ts = drift_data.get("timestamp", "?")[:19]
        drift_html += '<div class="muted" style="margin-bottom:10px">Laatste check: %s</div>' % drift_ts
        drift_html += '<table class="trades-table"><tr><th>Wallet</th><th>League</th><th>Status</th><th>All WR</th><th>Recent WR</th><th>N</th><th>Ghosts</th><th>Days</th></tr>'

        for w in drift_data.get("wallets", []):
            name = w.get("name", "?")
            for lg in w.get("leagues", []):
                league = lg.get("league", "?")
                status = lg.get("status", "?")
                all_wr = lg.get("all_wr", 0)
                recent_wr = lg.get("recent_wr", 0)
                all_n = lg.get("all_n", 0)
                ghosts = lg.get("ghost_losses", 0)
                days = lg.get("days_since_last", 999)

                if status == "DRIFT_ALERT":
                    color = "var(--red)"
                    badge = "ALERT"
                elif status == "DRIFT_WARNING":
                    color = "#ffaa00"
                    badge = "WARNING"
                elif status == "INACTIVE":
                    color = "#888"
                    badge = "INACTIVE"
                elif status == "BASELINE_SET":
                    color = "var(--blue)"
                    badge = "NEW"
                else:
                    color = "var(--green)"
                    badge = "OK"

                drift_html += '<tr>'
                drift_html += '<td>%s</td>' % name
                drift_html += '<td>%s</td>' % league.upper()
                drift_html += '<td style="color:%s;font-weight:700">%s</td>' % (color, badge)
                drift_html += '<td>%.1f%%</td>' % all_wr
                drift_html += '<td style="color:%s">%.1f%%</td>' % (color, recent_wr)
                drift_html += '<td>%d</td>' % all_n
                drift_html += '<td>%d</td>' % ghosts
                drift_html += '<td>%s</td>' % (str(days) + "d" if days < 999 else "?")
                drift_html += '</tr>'

        drift_html += '</table>'
    drift_html += '</div>'

    # Healthcheck section
    health_html = '<div class="section"><div class="section-title">Healthcheck (24h)</div>'
    if not health_data:
        health_html += '<div class="empty">Geen healthcheck data. Eerste check draait om 09:00 UTC.</div>'
    else:
        health_ts = health_data.get("timestamp", "?")[:19]
        summary = health_data.get("summary", {})
        total_trades = summary.get("total_trades_24h", 0)
        total_pnl = summary.get("total_pnl_24h", 0)
        pnl_color = "var(--green)" if total_pnl >= 0 else "var(--red)"

        health_html += '<div class="kpi-row" style="margin-bottom:15px">'
        health_html += '<div class="kpi-tile"><div class="kpi-label">Trades 24h</div><div class="kpi-value">%d</div></div>' % total_trades
        health_html += '<div class="kpi-tile"><div class="kpi-label">PnL 24h</div><div class="kpi-value" style="color:%s">$%+.2f</div></div>' % (pnl_color, total_pnl)
        health_html += '<div class="kpi-tile"><div class="kpi-label">Last Check</div><div class="kpi-value" style="font-size:0.9rem">%s</div></div>' % health_ts[:16]
        health_html += '</div>'

        health_html += '<table class="trades-table"><tr><th>Wallet</th><th>Status</th><th>Signals</th><th>Discovers</th><th>Skips</th><th>Trades</th><th>Skip Reasons</th></tr>'

        for w in health_data.get("wallets", []):
            name = w.get("name", "?")
            status = w.get("status", "?")
            signals = w.get("signals_24h", 0)
            discovers = w.get("discovers_24h", 0)
            skips = w.get("t1_skips_24h", 0)
            trades = w.get("trades_24h", 0)
            skip_reasons = w.get("skip_reasons", {})

            if status == "TRADED":
                color = "var(--green)"
            elif status == "SILENT":
                color = "var(--red)"
            elif "SIGNAL" in status:
                color = "#ffaa00"
            else:
                color = "#888"

            reasons_str = ", ".join("%s: %d" % (k, v) for k, v in skip_reasons.items()) if skip_reasons else "-"

            health_html += '<tr>'
            health_html += '<td>%s</td>' % name
            health_html += '<td style="color:%s;font-weight:700">%s</td>' % (color, status)
            health_html += '<td>%d</td>' % signals
            health_html += '<td>%d</td>' % discovers
            health_html += '<td>%d</td>' % skips
            health_html += '<td style="font-weight:700">%d</td>' % trades
            health_html += '<td class="muted" style="font-size:0.8rem">%s</td>' % reasons_str
            health_html += '</tr>'

        health_html += '</table>'

        issues = summary.get("issues", [])
        if issues:
            health_html += '<div style="margin-top:15px">'
            for issue in issues:
                health_html += '<div style="color:var(--red);margin:4px 0">%s</div>' % issue
            health_html += '</div>'

    health_html += '</div>'

    body = drift_html + health_html
    return page_wrap("/health", body, token, account=account)


def render_settings_page(token=""):
    prefix = f"/t/{token}" if token else ""
    stop_url = f"{prefix}/stop"
    transfer_url = f"{prefix}/transfer"

    # Get current cash balance
    pm = fetch_pm_data()
    cash = pm.get("cash", 0)

    # Bot status
    bot_running = False
    try:
        import subprocess
        result = subprocess.run(["systemctl", "is-active", "bottie"], capture_output=True, text=True, timeout=5)
        bot_running = result.stdout.strip() == "active"
    except Exception:
        pass

    status_color = "var(--green)" if bot_running else "var(--red)"
    status_text = "ACTIVE" if bot_running else "STOPPED"

    wallet_options = ""
    for name, addr in WITHDRAW_WALLETS.items():
        short = addr[:6] + "..." + addr[-4:]
        wallet_options += f'<option value="{addr}">{name} ({short})</option>'

    body = f"""
    <div class="section">
      <div class="section-title">Bot Control</div>
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
          <span style="font-size:0.9rem;font-weight:600">Status:</span>
          <span style="color:{status_color};font-weight:700;font-size:0.9rem">{status_text}</span>
        </div>
        <button class="stop-btn" style="padding:10px 24px;font-size:0.9rem"
          onclick="if(confirm('⚠️ STOP BOTTIE? Dit stopt alle trading.'))fetch('{stop_url}',{{method:'POST'}}).then(r=>r.text()).then(t=>{{alert(t);location.reload();}})">
          ⏹ STOP BOT
        </button>
      </div>
    </div>

    <div class="section">
      <div class="section-title">Withdraw USDC</div>
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px">
        <div style="margin-bottom:16px;font-size:0.85rem;color:var(--muted)">
          Beschikbaar: <span style="color:var(--green);font-weight:700">${cash:.2f}</span> USDC
        </div>

        <div style="margin-bottom:16px">
          <label style="font-size:0.8rem;font-weight:600;display:block;margin-bottom:6px">Bedrag (USDC)</label>
          <input id="withdraw-amount" type="number" min="1" max="{cash:.0f}" step="0.01" placeholder="0.00"
            style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 14px;color:var(--text);font-size:0.95rem;width:200px;font-family:monospace">
          <div style="margin-top:8px;display:flex;gap:8px">
            <button onclick="document.getElementById('withdraw-amount').value='{cash/4:.2f}'" style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:4px 10px;color:var(--muted);font-size:0.75rem;cursor:pointer">25%</button>
            <button onclick="document.getElementById('withdraw-amount').value='{cash/2:.2f}'" style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:4px 10px;color:var(--muted);font-size:0.75rem;cursor:pointer">50%</button>
            <button onclick="document.getElementById('withdraw-amount').value='{cash:.2f}'" style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:4px 10px;color:var(--muted);font-size:0.75rem;cursor:pointer">100%</button>
          </div>
        </div>

        <div style="margin-bottom:20px">
          <label style="font-size:0.8rem;font-weight:600;display:block;margin-bottom:6px">Naar wallet</label>
          <select id="withdraw-wallet"
            style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 14px;color:var(--text);font-size:0.9rem;width:320px">
            {wallet_options}
          </select>
        </div>

        <button id="transfer-btn" onclick="doTransfer()" style="background:var(--green);color:#fff;border:none;border-radius:6px;padding:10px 24px;font-size:0.9rem;font-weight:700;cursor:pointer">
          Verstuur USDC
        </button>
        <div id="transfer-result" style="margin-top:12px;font-size:0.85rem"></div>
      </div>
    </div>

    <script>
    function doTransfer() {{
      const amount = document.getElementById('withdraw-amount').value;
      const wallet = document.getElementById('withdraw-wallet').value;
      const btn = document.getElementById('transfer-btn');
      const resultDiv = document.getElementById('transfer-result');

      if (!amount || parseFloat(amount) <= 0) {{
        resultDiv.innerHTML = '<span style="color:var(--red)">Vul een bedrag in</span>';
        return;
      }}

      const walletName = document.getElementById('withdraw-wallet').selectedOptions[0].text;
      if (!confirm('Verstuur $' + amount + ' USDC naar ' + walletName + '?')) return;

      btn.disabled = true;
      btn.textContent = 'Bezig...';
      resultDiv.innerHTML = '<span style="color:var(--yellow)">Transactie wordt verstuurd...</span>';

      fetch('{transfer_url}', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{amount: parseFloat(amount), to: wallet}})
      }})
      .then(r => r.json())
      .then(data => {{
        if (data.ok) {{
          resultDiv.innerHTML = '<span style="color:var(--green)">✓ Verstuurd! TX: <a href="https://polygonscan.com/tx/' + data.tx_hash + '" target="_blank" style="color:var(--blue)">' + data.tx_hash.substring(0,16) + '...</a></span>';
        }} else {{
          resultDiv.innerHTML = '<span style="color:var(--red)">✗ ' + data.error + '</span>';
        }}
        btn.disabled = false;
        btn.textContent = 'Verstuur USDC';
      }})
      .catch(e => {{
        resultDiv.innerHTML = '<span style="color:var(--red)">✗ Fout: ' + e + '</span>';
        btn.disabled = false;
        btn.textContent = 'Verstuur USDC';
      }});
    }}
    </script>"""

    return page_wrap("/settings", body, token)


# ── SSOT Analysis Page ────────────────────────────────────────────────────────

def render_ssot_page(token="", account="cannae"):
    """Render the SSOT analysis page using scripts/ssot.py."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(CODE_DIR))
        from scripts.ssot import build_report
        trades_path = TRADES_FILE
        report = build_report(trades_path)
    except Exception as e:
        import traceback
        return page_wrap("/ssot", f'<div class="section"><pre>SSOT Error: {e}\n{traceback.format_exc()}</pre></div>', token, account=account)

    s = report["summary"]

    # Summary card
    pnl_color = "#3fb950" if s["total_pnl"] >= 0 else "#f85149"
    summary_html = f"""
    <div class="section">
      <div class="section-title">Summary — {s['total_games']} games</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:12px">
        <div class="card"><div class="card-label">Win Rate</div><div class="card-value">{s['wr']}%</div><div class="card-sub">[{s['wr_ci'][0]}-{s['wr_ci'][1]}%] CI</div></div>
        <div class="card"><div class="card-label">PnL</div><div class="card-value" style="color:{pnl_color}">${s['total_pnl']:+.2f}</div></div>
        <div class="card"><div class="card-label">ROI</div><div class="card-value">{s['roi']}%</div></div>
        <div class="card"><div class="card-label">Record</div><div class="card-value">{s['wins']}W / {s['losses']}L</div></div>
      </div>
    </div>"""

    # Combo table
    combo_rows = ""
    for combo, cs in report["by_combo"].items():
        pnl_c = "#3fb950" if cs["pnl"] >= 0 else "#f85149"
        combo_rows += f"""<tr>
          <td><strong>{combo}</strong></td><td>{cs['games']}</td>
          <td>{cs['wins']}W/{cs['losses']}L</td><td>{cs['wr']}%</td>
          <td style="color:#8b949e">[{cs['wr_ci'][0]}-{cs['wr_ci'][1]}%]</td>
          <td style="color:{pnl_c}">${cs['pnl']:+.2f}</td><td>{cs['roi']}%</td></tr>"""

    combo_html = f"""
    <div class="section">
      <div class="section-title">Per Game Combination</div>
      <table><thead><tr><th>Combo</th><th>Games</th><th>W/L</th><th>WR%</th><th>CI</th><th>PnL</th><th>ROI</th></tr></thead>
      <tbody>{combo_rows}</tbody></table>
    </div>"""

    # League table — only show leagues with >= 1 game, skip "other"
    league_rows = ""
    for league, ls in report["by_league"].items():
        if league == "other":
            continue
        pnl_c = "#3fb950" if ls["pnl"] >= 0 else "#f85149"
        league_rows += f"""<tr>
          <td><strong>{league}</strong></td><td>{ls['sport']}</td><td>{ls['games']}</td>
          <td>{ls['wins']}W/{ls['losses']}L</td><td>{ls['wr']}%</td>
          <td style="color:#8b949e">[{ls['wr_ci'][0]}-{ls['wr_ci'][1]}%]</td>
          <td style="color:{pnl_c}">${ls['pnl']:+.2f}</td><td>{ls['roi']}%</td></tr>"""

    league_html = f"""
    <div class="section">
      <div class="section-title">Per League</div>
      <table><thead><tr><th>League</th><th>Sport</th><th>Games</th><th>W/L</th><th>WR%</th><th>CI</th><th>PnL</th><th>ROI</th></tr></thead>
      <tbody>{league_rows}</tbody></table>
    </div>"""

    # CLV
    clv = report["clv"]
    if clv.get("available"):
        clv_html = f"""
    <div class="section">
      <div class="section-title">CLV Analysis</div>
      <p>Mean CLV: <strong>{clv['mean_clv']:+.4f}</strong> | +CLV: {clv['positive_clv']} trades (WR {clv['pos_wr']}%) | -CLV: {clv['negative_clv']} trades (WR {clv['neg_wr']}%)</p>
    </div>"""
    else:
        clv_html = f"""
    <div class="section">
      <div class="section-title">CLV Analysis</div>
      <p style="color:#8b949e">{clv.get('message', 'No data yet')}</p>
    </div>"""

    # Correlation
    corr = report["correlation"]
    corr_html = f"""
    <div class="section">
      <div class="section-title">Correlation Analysis</div>
      <p>Multi-leg games: <strong>{corr['multi_leg_games']}</strong></p>"""
    wn = corr.get("win_no_draw_yes", {})
    if wn.get("total", 0) > 0:
        corr_html += f"""<p>WIN_NO+DRAW_YES: {wn['total']} games — both win: {wn['both_win']}, both lose: {wn['both_lose']}, mixed: {wn['mixed']} (<strong>{wn['correlation']}</strong> correlation)</p>"""
    corr_html += "</div>"

    # Daily PnL chart (simple bar)
    daily_html = '<div class="section"><div class="section-title">Daily PnL</div><table><thead><tr><th>Date</th><th>Games</th><th>WR</th><th>PnL</th></tr></thead><tbody>'
    for d in report["daily"]:
        pnl_c = "#3fb950" if d["pnl"] >= 0 else "#f85149"
        daily_html += f'<tr><td>{d["date"]}</td><td>{d["games"]}</td><td>{d["wr"]}%</td><td style="color:{pnl_c}">${d["pnl"]:+.2f}</td></tr>'
    daily_html += "</tbody></table></div>"

    body = summary_html + combo_html + league_html + clv_html + corr_html + daily_html
    return page_wrap("/ssot", body, token, account=account)


def render_5m_page(token="", account="cannae"):
    """BTC 5M bot dashboard — mobile-first redesign."""
    import subprocess, re
    from collections import defaultdict

    trades_file = Path("/opt/bottie-test/data/fivemin_bot/trades.jsonl")

    trades = []
    if trades_file.exists():
        for line in open(trades_file):
            try:
                t = json.loads(line)
                if t.get("type") != "sell":
                    trades.append(t)
            except: pass

    crypto5m_data = fetch_pm_data(CRYPTO5M_FUNDER)
    cash = crypto5m_data.get("cash", 0)
    pos_value = crypto5m_data.get("positions_value", 0)
    total_value = cash + pos_value

    active_positions = []
    for p in crypto5m_data.get("positions", []):
        title = p.get("title", "")
        size = float(p.get("size", 0) or 0)
        if size > 0.1 and "Up or Down" in title:
            active_positions.append({
                "title": title,
                "outcome": p.get("outcome", ""),
                "size": size,
                "avg": float(p.get("avgPrice", 0) or 0),
                "current": float(p.get("curPrice", 0) or 0),
                "value": float(p.get("currentValue", 0) or 0),
            })

    now_ts = int(datetime.now(timezone.utc).timestamp())
    h24_ago = now_ts - 86400

    def parse_ts(s):
        try: return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except: return 0

    all_n = len(trades)
    all_wins = sum(1 for t in trades if t.get("result") == "WIN")
    all_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades)
    h24 = [t for t in trades if parse_ts(t.get("timestamp", "")) >= h24_ago]
    h24_n = len(h24)
    h24_wins = sum(1 for t in h24 if t.get("result") == "WIN")
    h24_pnl = sum(float(t.get("pnl", 0) or 0) for t in h24)

    fill_re = re.compile(r"(\S+)\s+.*FILL BTC (\w+) ([\d.]+)sh @([\d.]+)")
    before_60 = 0
    after_60 = 0
    tier_fills = defaultdict(lambda: {"before": 0, "after": 0})
    try:
        _now = time.time()
        if _journal_cache["data"] is not None and _now - _journal_cache["ts"] < 30:
            out = _journal_cache["data"]
        else:
            out = subprocess.check_output(
                ["journalctl", "-u", "fivemin-bot", "--since", "24 hours ago",
                 "--no-pager", "-o", "short-iso", "--grep", "FILL BTC"],
                timeout=15
            ).decode(errors="ignore")
            _journal_cache["data"] = out
            _journal_cache["ts"] = _now
        for line in out.split("\n"):
            m = fill_re.search(line)
            if m:
                ts_str, side, sh, price = m.groups()
                try: ts = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
                except: continue
                ws = (ts // 300) * 300
                offset = ts - ws
                price_f = float(price)
                tier = "1c" if abs(price_f - 0.01) < 0.001 else ("2c" if abs(price_f - 0.02) < 0.001 else "3c")
                if offset < 240:
                    before_60 += 1; tier_fills[tier]["before"] += 1
                else:
                    after_60 += 1; tier_fills[tier]["after"] += 1
    except Exception: pass
    total_fills = before_60 + after_60
    bet_per_side = 5.00
    saved_usd = after_60 * bet_per_side

    skim_pct_str = "0% (compound)"
    try:
        skim_txt = open("/opt/bottie-test/scripts/fivemin_profit_skim.py").read()
        m = re.search(r"SKIM_PCT\s*=\s*([\d.]+)", skim_txt)
        if m:
            p = float(m.group(1))
            skim_pct_str = f"{p*100:.0f}% skim" if p > 0 else "0% (compound)"
    except: pass

    recent = sorted(trades, key=lambda t: t.get("timestamp", ""), reverse=True)[:15]

    win_rate = (all_wins / all_n * 100) if all_n else 0
    pct_before = (before_60 / total_fills * 100) if total_fills else 0

    # Color logic
    def pnl_color(v): return "#22c55e" if v >= 0 else "#ef4444"
    def pnl_sign(v): return "+" if v >= 0 else ""

    h24_color = pnl_color(h24_pnl)
    all_color = pnl_color(all_pnl)

    # Build tier cards
    tier_cards = ""
    for t in ["1c", "2c", "3c"]:
        s = tier_fills[t]
        tot = s["before"] + s["after"]
        cancel_pct = (s["after"] / tot * 100) if tot else 0
        before_pct = 100 - cancel_pct
        tier_cards += f"""
        <div class="tier-row">
          <div class="tier-name">{t}</div>
          <div class="tier-bar">
            <div class="tier-bar-before" style="width:{before_pct}%"></div>
            <div class="tier-bar-after" style="width:{cancel_pct}%"></div>
          </div>
          <div class="tier-nums"><span class="good">{s['before']}</span> · <span class="bad">{s['after']}</span></div>
        </div>
        """

    # Active positions
    pos_html = ""
    if active_positions:
        for p in sorted(active_positions, key=lambda x: -x["value"])[:10]:
            title_short = p["title"].replace("Bitcoin Up or Down - ", "").replace(" ET", "")
            pnl = p["value"] - (p["avg"] * p["size"])
            pnl_c = pnl_color(pnl)
            pos_html += f"""
            <div class="pos-card">
              <div class="pos-title">{title_short}</div>
              <div class="pos-row">
                <span class="pill {'pill-up' if p['outcome'].lower() == 'up' else 'pill-down'}">{p['outcome']}</span>
                <span class="pos-shares">{p['size']:.0f}sh @ {p['avg']*100:.1f}¢</span>
                <span class="pos-value">${p['value']:.2f}</span>
              </div>
            </div>
            """
    else:
        pos_html = '<div class="empty">Geen open posities</div>'

    # Recent trades
    trades_html = ""
    for t in recent:
        ts = (t.get("timestamp", ""))[:16].replace("T", " ")
        title_short = (t.get("title") or "").replace("Bitcoin Up or Down - ", "").replace(" ET", "")[:40]
        fills = t.get("fills") or {}
        side = "/".join(k for k in fills.keys() if not k.startswith("_"))
        result = t.get("result", "?")
        is_win = result == "WIN"
        pnl = float(t.get("pnl", 0) or 0)
        trades_html += f"""
        <div class="trade-row">
          <div class="trade-left">
            <div class="trade-time">{ts}</div>
            <div class="trade-title">{title_short}</div>
          </div>
          <div class="trade-right">
            <div class="result-pill {'win' if is_win else 'loss'}">{result}</div>
            <div class="trade-pnl {'good' if pnl >= 0 else 'bad'}">{pnl_sign(pnl)}${pnl:.2f}</div>
          </div>
        </div>
        """

    body = f"""
<style>
  * {{ box-sizing: border-box; }}
  .main {{ padding: 0 !important; max-width: 640px; margin: 0 auto; background: #000; }}
  .hero {{
    background: linear-gradient(160deg, #0a0a0a 0%, #141621 100%);
    padding: 24px 20px 28px;
    border-bottom: 1px solid #1f2937;
  }}
  .hero-label {{ color: #6b7280; font-size: 12px; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 8px; }}
  .hero-value {{ color: #fff; font-size: 42px; font-weight: 700; line-height: 1; letter-spacing: -0.5px; }}
  .hero-sub {{ color: #9ca3af; font-size: 13px; margin-top: 8px; }}
  .hero-today {{ margin-top: 18px; padding-top: 14px; border-top: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: baseline; }}
  .hero-today-label {{ color: #9ca3af; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  .hero-today-value {{ font-size: 24px; font-weight: 700; color: {h24_color}; }}

  .kpi-strip {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    background: #1f2937;
    margin-bottom: 0;
  }}
  .kpi-item {{
    background: #0a0a0a;
    padding: 16px 12px;
    text-align: center;
  }}
  .kpi-item-label {{ color: #6b7280; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
  .kpi-item-value {{ color: #fff; font-size: 20px; font-weight: 600; }}
  .kpi-item-sub {{ color: #6b7280; font-size: 11px; margin-top: 3px; }}

  .section {{ padding: 24px 20px; border-top: 8px solid #000; background: #0a0a0a; }}
  .section h2 {{ color: #fff; font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 1.5px; margin: 0 0 16px; }}

  .fill-summary {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 16px;
  }}
  .fill-box {{ padding: 14px; border-radius: 10px; }}
  .fill-box-good {{ background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3); }}
  .fill-box-bad {{ background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); }}
  .fill-box-label {{ font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
  .fill-box-value {{ font-size: 24px; font-weight: 700; color: #fff; line-height: 1; }}
  .fill-box-sub {{ font-size: 11px; color: #6b7280; margin-top: 4px; }}
  .savings-banner {{
    background: linear-gradient(135deg, rgba(34,197,94,0.15), rgba(34,197,94,0.05));
    border: 1px solid rgba(34,197,94,0.4);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .savings-label {{ color: #9ca3af; font-size: 12px; }}
  .savings-value {{ color: #22c55e; font-size: 20px; font-weight: 700; }}

  .tier-row {{
    display: grid;
    grid-template-columns: 32px 1fr 90px;
    gap: 12px;
    align-items: center;
    padding: 12px 0;
    border-bottom: 1px solid #1f2937;
  }}
  .tier-row:last-child {{ border-bottom: none; }}
  .tier-name {{ color: #fff; font-weight: 600; font-size: 14px; }}
  .tier-bar {{ height: 8px; background: #1f2937; border-radius: 4px; overflow: hidden; display: flex; }}
  .tier-bar-before {{ background: #22c55e; height: 100%; }}
  .tier-bar-after {{ background: #ef4444; height: 100%; }}
  .tier-nums {{ color: #9ca3af; font-size: 13px; text-align: right; font-variant-numeric: tabular-nums; }}
  .good {{ color: #22c55e; font-weight: 600; }}
  .bad {{ color: #ef4444; font-weight: 600; }}

  .pos-card {{ padding: 14px; background: #111827; border-radius: 10px; margin-bottom: 8px; border: 1px solid #1f2937; }}
  .pos-title {{ color: #e5e7eb; font-size: 13px; font-weight: 500; margin-bottom: 8px; }}
  .pos-row {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
  .pill {{ padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; }}
  .pill-up {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
  .pill-down {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
  .pos-shares {{ color: #9ca3af; font-size: 12px; flex: 1; text-align: center; }}
  .pos-value {{ color: #fff; font-weight: 600; font-variant-numeric: tabular-nums; }}

  .trade-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 0;
    border-bottom: 1px solid #1f2937;
    gap: 12px;
  }}
  .trade-row:last-child {{ border-bottom: none; }}
  .trade-time {{ color: #6b7280; font-size: 11px; }}
  .trade-title {{ color: #e5e7eb; font-size: 13px; margin-top: 2px; }}
  .trade-right {{ text-align: right; flex-shrink: 0; }}
  .result-pill {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; }}
  .result-pill.win {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
  .result-pill.loss {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
  .trade-pnl {{ font-size: 14px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }}

  .empty {{ color: #6b7280; text-align: center; padding: 24px; font-size: 13px; }}
  @media (max-width: 420px) {{
    .hero-value {{ font-size: 36px; }}
    .kpi-item-value {{ font-size: 17px; }}
  }}
</style>

<div class="hero">
  <div class="hero-label">Bankroll · Crypto 5M</div>
  <div class="hero-value">${total_value:,.2f}</div>
  <div class="hero-sub">cash ${cash:,.0f} + open posities ${pos_value:,.0f}</div>
  <div class="hero-today">
    <div>
      <div class="hero-today-label">Vandaag (24h)</div>
      <div class="hero-today-value">{pnl_sign(h24_pnl)}${h24_pnl:,.2f}</div>
    </div>
    <div style="text-align:right">
      <div class="hero-today-label">All-time</div>
      <div class="hero-today-value" style="color:{all_color}">{pnl_sign(all_pnl)}${all_pnl:,.2f}</div>
    </div>
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-item">
    <div class="kpi-item-label">Trades 24h</div>
    <div class="kpi-item-value">{h24_n}</div>
    <div class="kpi-item-sub">{h24_wins} wins</div>
  </div>
  <div class="kpi-item">
    <div class="kpi-item-label">Win Rate</div>
    <div class="kpi-item-value">{win_rate:.1f}%</div>
    <div class="kpi-item-sub">{all_wins}/{all_n} all-time</div>
  </div>
  <div class="kpi-item">
    <div class="kpi-item-label">Skim</div>
    <div class="kpi-item-value" style="font-size:16px">{skim_pct_str}</div>
    <div class="kpi-item-sub">2×/dag</div>
  </div>
</div>

<div class="section">
  <h2>Fill Efficiency · 24h</h2>
  <div class="fill-summary">
    <div class="fill-box fill-box-good">
      <div class="fill-box-label">Productief</div>
      <div class="fill-box-value">{before_60}</div>
      <div class="fill-box-sub">voor T-60s · {pct_before:.0f}%</div>
    </div>
    <div class="fill-box fill-box-bad">
      <div class="fill-box-label">Gecancelled</div>
      <div class="fill-box-value">{after_60}</div>
      <div class="fill-box-sub">na T-60s · 0% WR</div>
    </div>
  </div>
  <div class="savings-banner">
    <div>
      <div class="savings-label">Besparing 24h door cancel-logica</div>
      <div style="color:#6b7280;font-size:11px;margin-top:2px">{after_60} × $5 niet uitgegeven</div>
    </div>
    <div class="savings-value">+${saved_usd:,.0f}</div>
  </div>
</div>

<div class="section">
  <h2>Per tier</h2>
  {tier_cards}
  <div style="color:#6b7280;font-size:11px;margin-top:10px;text-align:center">
    <span class="good">groen</span> = voor T-60s · <span class="bad">rood</span> = na T-60s (gecancelled)
  </div>
</div>

<div class="section">
  <h2>Open Posities</h2>
  {pos_html}
</div>

<div class="section">
  <h2>Recente Trades</h2>
  {trades_html if trades_html else '<div class="empty">Nog geen trades</div>'}
</div>
"""

    return page_wrap("/5m", body, token, account=account)

# ── HTTP Server ───────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        """Extract token from /t/<token>/... path. Returns (page_path, token) or None."""
        # Allow /t/<token>/ and /t/<token>/page paths
        if self.path.startswith(f"/t/{AUTH_TOKEN}"):
            rest = self.path[len(f"/t/{AUTH_TOKEN}"):]
            if not rest or rest == "/":
                return ("/", AUTH_TOKEN)
            return (rest, AUTH_TOKEN)
        # Also allow localhost without token (SSH tunnel)
        host = self.headers.get("Host", "")
        if host.startswith("localhost") or host.startswith("127.0.0.1"):
            return (self.path, "")
        return None

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self._check_auth()
        if auth is None:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        page, token = auth

        # Strip query string for routing; extract account param
        _parsed = urlparse(page)
        page = _parsed.path
        account = parse_qs(_parsed.query).get("account", ["cannae"])[0]
        if account not in ("cannae", "crypto5m"):
            account = "cannae"

        if page == "/settings":
            try:
                html = render_settings_page(token)
                self._send_html(html)
            except Exception as e:
                import traceback
                self._send_html(f"<pre>Error: {e}\n{traceback.format_exc()}</pre>", 500)
        elif page == "/ssot":
            try:
                html = render_ssot_page(token, account=account)
                self._send_html(html)
            except Exception as e:
                import traceback
                self._send_html(f"<pre>Error: {e}\n{traceback.format_exc()}</pre>", 500)
        elif page == "/games":
            try:
                trades     = load_trades()
                wallet_map = parse_config_wallets()
                html = render_games_page(trades, wallet_map, token=token, account=account)
                if token and f"/t/{token}" not in html:
                    html = html.replace('href="/', f'href="/t/{token}/')
                    html = html.replace("fetch('/", f"fetch('/t/{token}/")
                self._send_html(html)
            except Exception as e:
                import traceback
                self._send_html(f"<pre>Error: {e}\n{traceback.format_exc()}</pre>", 500)
        elif page == "/api/games":
            try:
                trades = load_trades()
                funder = CRYPTO5M_FUNDER if account == "crypto5m" else PM_FUNDER
                games = build_games_data(trades, funder)
                body = json.dumps(games).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
        elif page == "/5m":
            try:
                html = render_5m_page(token=token, account=account)
                if token and f"/t/{token}" not in html:
                    html = html.replace("href=\"/", f"href=\"/t/{token}/")
                    html = html.replace("fetch(\"", f"fetch(\"/t/{token}/")
                self._send_html(html)
            except Exception as e:
                import traceback
                self._send_html(f"<pre>Error: {e}\n{traceback.format_exc()}</pre>", 500)
        elif page == "/health":
            try:
                html = render_health_page(token=token, account=account)
                self._send_html(html)
            except Exception as e:
                import traceback
                self._send_html("<pre>Error: %s\n%s</pre>" % (e, traceback.format_exc()), 500)
        elif page == "/":
            # Always redirect to /5m (5M is the only active strategy)
            prefix = f"/t/{token}" if token else ""
            self.send_response(302)
            self.send_header("Location", f"{prefix}/5m")
            self.end_headers()
            return
        elif page in ("/", "/index.html", "/trades", "/wallets", "/edge", "/ops", "/strategy", "/intel"):
            try:
                trades     = load_trades()
                wallet_map = parse_config_wallets()
                if page == "/trades":
                    html = render_trades_page(trades, wallet_map, account=account)
                else:
                    html = render_overview(trades, wallet_map, account=account)
                # Inject token into page_wrap calls that don't have it yet
                if token and f"/t/{token}" not in html:
                    html = html.replace('href="/', f'href="/t/{token}/')
                    html = html.replace("fetch('/", f"fetch('/t/{token}/")
                self._send_html(html)
            except Exception as e:
                import traceback
                self._send_html(f"<pre>Error: {e}\n{traceback.format_exc()}</pre>", 500)
        elif page == "/api/trades":
            try:
                trades = load_trades()
                body = json.dumps(trades).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        auth = self._check_auth()
        if auth is None:
            self.send_response(403)
            self.end_headers()
            return

        page, token = auth

        if page == "/stop":
            import subprocess
            try:
                subprocess.run(["systemctl", "stop", "bottie"], check=True, timeout=10)
                # Send Telegram alert
                import urllib.request
                tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
                if tg_token and tg_chat:
                    msg = "⏹ BOTTIE STOPPED via dashboard noodstop"
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            f"https://api.telegram.org/bot{tg_token}/sendMessage",
                            data=f"chat_id={tg_chat}&text={msg}".encode(),
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                        ), timeout=5)
                    except: pass
                body = b"Bot gestopt. Herstart via SSH: systemctl start bottie"
            except Exception as e:
                body = f"Stop failed: {e}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif page == "/sell":
            import subprocess
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_len) if content_len else b"{}"
                data = json.loads(raw)
                token_id = data.get("token_id", "")
                shares = float(data.get("shares", 0))
                min_price = float(data.get("min_price", 0.01))

                if not token_id or shares <= 0:
                    raise ValueError("token_id and shares required")

                script = str(CODE_DIR / "scripts" / "sell_position.py")
                result = subprocess.run(
                    ["python3", script, token_id, str(shares), str(min_price)],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(BASE_DIR),
                )
                resp = json.loads(result.stdout) if result.stdout.strip() else {"ok": False, "error": result.stderr or "No output"}

                # Telegram notification on success
                if resp.get("ok"):
                    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
                    if tg_token and tg_chat:
                        msg = f"MANUAL SELL: {shares:.0f}sh @ {resp.get('price', 0):.0%} = ${resp.get('usdc', 0):.2f}"
                        try:
                            urllib.request.urlopen(urllib.request.Request(
                                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                                data=f"chat_id={tg_chat}&text={msg}".encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded"},
                            ), timeout=5)
                        except: pass

                body = json.dumps(resp).encode()
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif page == "/transfer":
            import subprocess
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_len) if content_len else b"{}"
                data = json.loads(raw)
                amount = float(data.get("amount", 0))
                to_addr = data.get("to", "")

                # Validate destination is in whitelist
                valid_addrs = set(WITHDRAW_WALLETS.values())
                if to_addr not in valid_addrs:
                    raise ValueError(f"Onbekend wallet adres: {to_addr}")
                if amount <= 0:
                    raise ValueError("Bedrag moet > 0 zijn")

                # Run transfer script
                script = str(CODE_DIR / "scripts" / "transfer_usdc.py")
                result = subprocess.run(
                    ["python3", script, to_addr, str(amount)],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(BASE_DIR),
                )
                resp = json.loads(result.stdout) if result.stdout.strip() else {"ok": False, "error": result.stderr or "No output"}

                # Send Telegram notification on success
                if resp.get("ok"):
                    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
                    wallet_name = next((n for n, a in WITHDRAW_WALLETS.items() if a == to_addr), to_addr[:10])
                    if tg_token and tg_chat:
                        msg = f"💸 USDC Transfer: ${amount:.2f} → {wallet_name}\nTX: https://polygonscan.com/tx/{resp.get('tx_hash', '')}"
                        try:
                            urllib.request.urlopen(urllib.request.Request(
                                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                                data=f"chat_id={tg_chat}&text={msg}".encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded"},
                            ), timeout=5)
                        except: pass

                body = json.dumps(resp).encode()
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress access logs


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    print(f"Bottie Dashboard → http://0.0.0.0:{port}")
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()
