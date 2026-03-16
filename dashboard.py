#!/usr/bin/env python3
"""Bottie Trading Dashboard — single-file, no external deps, port 8080."""

import json, re, glob, os
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BASE_DIR       = Path(__file__).parent
TRADES_FILE    = BASE_DIR / "data" / "trades.jsonl"
DAG_FILE       = BASE_DIR / "data" / "research_dag.jsonl"
SCOUT_FILE     = BASE_DIR / "data" / "scout_report.json"
PLAYBOOK_FILE  = BASE_DIR / "data" / "playbook.md"
CONFIG_FILE    = BASE_DIR / "config.yaml"
PM_CACHE_FILE  = BASE_DIR / "data" / "pm_cache.json"
INITIAL_BANKROLL = 377.0  # total deposited

# Polymarket Data API — source of truth
PM_DATA_API = "https://data-api.polymarket.com"
PM_FUNDER = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"


# ── PM API Data (source of truth) ──────────────────────────────────────────

import urllib.request, urllib.error, time

_pm_cache = {"data": None, "ts": 0}

def fetch_pm_data():
    """Fetch real data from Polymarket API. Cached for 60 seconds."""
    now = time.time()
    if _pm_cache["data"] and now - _pm_cache["ts"] < 60:
        return _pm_cache["data"]

    result = {"trades": [], "positions": [], "value": 0, "positions_value": 0, "cash": 0, "error": None}

    def pm_get(url):
        """Fetch PM API with proper headers to avoid 403."""
        req = urllib.request.Request(url, headers={
            "User-Agent": "Bottie-Dashboard/1.0",
            "Accept": "application/json",
        })
        return urllib.request.urlopen(req, timeout=15)

    try:
        # Paginate PM trades API (max 1000 per request, safety cap at 10K)
        all_trades = []
        offset = 0
        while offset < 10000:
            url = "%s/trades?user=%s&limit=1000&offset=%d" % (PM_DATA_API, PM_FUNDER, offset)
            batch = json.loads(pm_get(url).read())
            all_trades.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
        result["trades"] = all_trades
    except Exception as e:
        result["error"] = "trades: %s" % e

    try:
        url = "%s/positions?user=%s&limit=500&sizeThreshold=0" % (PM_DATA_API, PM_FUNDER)
        result["positions"] = json.loads(pm_get(url).read())
    except Exception as e:
        result["error"] = "positions: %s" % e

    try:
        url = "%s/value?user=%s" % (PM_DATA_API, PM_FUNDER)
        val = json.loads(pm_get(url).read())
        if isinstance(val, list) and val:
            result["positions_value"] = float(val[0].get("value", 0))
    except Exception:
        pass

    # Cash = on-chain USDC.e balance via Polygon RPC (same as bot uses)
    try:
        addr = PM_FUNDER.lower().replace("0x", "")
        data = "0x70a08231" + addr.rjust(64, "0")  # balanceOf(address)
        payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call",
                              "params": [{"to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "data": data}, "latest"], "id": 1})
        rpc_req = urllib.request.Request("https://polygon-bor-rpc.publicnode.com",
                                         data=payload.encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "Bottie-Dashboard/1.0"},
                                         method="POST")
        rpc_resp = json.loads(urllib.request.urlopen(rpc_req, timeout=10).read())
        hex_val = rpc_resp.get("result", "0x0").replace("0x", "")
        result["cash"] = int(hex_val, 16) / 1_000_000.0  # USDC has 6 decimals
    except Exception:
        result["cash"] = 0

    # Total portfolio = positions value + cash (matches PM UI exactly)
    result["value"] = result["positions_value"] + result["cash"]

    _pm_cache["data"] = result
    _pm_cache["ts"] = now
    return result


def sf(v):
    """Safe float conversion."""
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        try: return float(v)
        except: return 0.0
    return 0.0


def compute_pm_kpis():
    """Compute KPIs from Polymarket API data (source of truth)."""
    pm = fetch_pm_data()
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

    # Portfolio = positions value + cash (from Polygon RPC USDC balance)
    portfolio_value = pm["value"] if pm["value"] > 0 else position_value

    cash = pm.get("cash", 0)

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
        "deposited": INITIAL_BANKROLL,
        "rendement": portfolio_value - INITIAL_BANKROLL if portfolio_value > 0 else 0,
        "rendement_pct": (portfolio_value / INITIAL_BANKROLL - 1) * 100 if portfolio_value > 0 and INITIAL_BANKROLL > 0 else 0,
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
                    # Filter out manual trades and crypto Up/Down noise
                    if t.get("signal_source") == "manual":
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
            wallets[addr] = {"name": name, "weight": weight, "tier": tier}
        return wallets
    except Exception:
        return {}

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

def compute_kpis(trades):
    """Compute KPIs — uses PM API for portfolio/value, trades.jsonl for WR attribution."""
    pm = compute_pm_kpis()

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
    all_addrs = set(wallet_map.keys()) | set(by_wallet.keys())
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
            "avg_edge": sum(t.get("edge_pct") or 0 for t in arb_t) / len(arb_t) if arb_t else 0,
        }

    return {"copy": stats(copy_t), "arb": stats(arb_t)}

def compute_daily_pnl(trades):
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss", "take_profit", "sold")]
    by_day = {}
    for t in filled:
        day = (t.get("resolved_at") or t.get("timestamp") or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + (t.get("pnl") or 0)
    # Last 14 days
    today = datetime.now(timezone.utc).date()
    result = []
    for i in range(13, -1, -1):
        d = str(today - timedelta(days=i))
        result.append({"date": d, "pnl": by_day.get(d, 0)})
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
        tier = info.get("tier", "?")
        consensus = trade.get("consensus_count") or 1
        delay_s = (trade.get("signal_delay_ms") or 0) / 1000
        tier_color = {"T1": "#388bfd", "T2": "#3fb950", "T3": "#8b949e"}.get(tier, "#8b949e")
        html = f'<span class="badge" style="background:{tier_color};color:#fff">{tier}</span> '
        html += f'<strong>{name}</strong>'
        if consensus > 1:
            html += f' <span class="badge" style="background:#d29922;color:#000">+{consensus-1} wallets</span>'
        html += f' <span class="muted" style="font-size:0.75rem">{delay_s:.1f}s</span>'
        return html
    elif src.startswith("odds_arb:"):
        bookmaker = src.split(":", 1)[1]
        edge = trade.get("edge_pct") or 0
        return (f'<span class="badge" style="background:#f0883e;color:#000">ARB</span> '
                f'<strong>{bookmaker}</strong> '
                f'<span class="badge" style="background:#3fb950;color:#000">+{edge:.1f}%</span>')
    return f'<span class="muted">{src or "?"}</span>'

def render_kpi_row(kpis, wallet_map):
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

    tiles = [
        ("Portfolio (PM)", f'${portfolio:.0f}', "#388bfd",
         f'cash: ${cash:.0f} + posities: ${pos_val:.0f}'),
        ("Rendement", f'{"+" if rendement >= 0 else ""}${rendement:.0f} ({rendement_pct:+.1f}%)', rend_color,
         f'gestort: ${deposited:.0f}'),
        ("Open Posities", f'${pos_val:.0f}', unr_color,
         f'{kpis["open_count"]} bets | cash: ${cash:.0f}'),
        ("Win Rate", f"{wr:.1f}%" if kpis["resolved_count"] else "—", wr_color,
         f'{kpis["resolved_count"]} resolved (trades.jsonl)'),
    ]

    tiles_html = ""
    for label, value, color, subtitle in tiles:
        tiles_html += f"""
        <div class="kpi-tile" style="border-top:3px solid {color}">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{subtitle}</div>
        </div>"""

    return f"""
    <div class="kpi-row">{tiles_html}</div>{error_badge}
    <div class="goal-bar-wrap">
      <div class="goal-label">
        DOEL: ${deposited:.0f} → ${goal:.0f}
        <span class="muted" style="float:right">{progress:.1f}% &nbsp; ${portfolio:.0f}</span>
      </div>
      <div class="goal-bar"><div class="goal-fill" style="width:{progress:.1f}%"></div></div>
    </div>"""

def render_open_bets(trades, wallet_map):
    open_bets = [t for t in trades if t.get("filled") and t.get("result") is None and not t.get("dry_run")]
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

        if compact:
            rows += f"""
        <tr{dim}>
          <td class="muted">{i}</td>
          <td><strong>{w['name']}</strong></td>
          <td>{w['weight']:.2f}</td>
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
          <td><span class="badge" style="background:{'#388bfd' if w['tier']=='T1' else '#3fb950' if w['tier']=='T2' else '#8b949e'};color:{'#fff' if w['tier']!='T2' else '#000'}">{w['tier']}</span></td>
          <td><strong>{w['name']}</strong></td>
          <td>{w['weight']:.2f}</td>
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
        <th>#</th><th>Wallet</th><th>Wt</th><th>Record</th>
        <th>Win%</th><th>P&L</th><th>Totaal</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Tier</th><th>Wallet</th><th>Wt</th>
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

def render_pnl_chart(daily):
    max_abs = max((abs(d["pnl"]) for d in daily), default=1) or 1
    bars = ""
    for d in daily:
        pnl = d["pnl"]
        h = max(2, abs(pnl) / max_abs * 60)
        color = "#3fb950" if pnl >= 0 else "#f85149"
        label = d["date"][5:]  # MM-DD
        bars += f"""
        <div class="chart-bar-wrap" title="{d['date']}: {'+'if pnl>=0 else ''}${pnl:.2f}">
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
"""


def page_wrap(active_page, body_html):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pages = [
        ("Overview", "/"),
        ("Trades", "/trades"),
        ("Wallets", "/wallets"),
        ("Research", "/research"),
    ]
    nav = ""
    for label, href in pages:
        cls = ' class="active"' if href == active_page else ""
        nav += f'<a href="{href}"{cls}>{label}</a>'

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Bottie — {active_page}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="header">
  <h1>BOTTIE</h1>
  <div class="header-right">
    <span>{now_str}</span>
    <span id="countdown">30</span>s
  </div>
</div>
<div class="nav">{nav}</div>
<div class="main">{body_html}</div>
<script>
let t = 30;
const el = document.getElementById('countdown');
if (el) setInterval(() => {{ el.textContent = --t; if(t<=0) location.reload(); }}, 1000);
</script>
</body>
</html>"""


def render_overview(trades, wallet_map):
    kpis = compute_kpis(trades)
    wallet_stats = compute_wallet_stats(trades, wallet_map)
    sport_stats = compute_sport_stats(trades)
    daily_pnl = compute_daily_pnl(trades)

    body = render_kpi_row(kpis, wallet_map)
    body += f"""
    <div class="section">
      <div class="section-title">Dagelijkse P&L (14d)</div>
      {render_pnl_chart(daily_pnl)}
    </div>
    <div class="section">
      <div class="section-title">Trade Log (laatste 30)</div>
      {render_resolved_trades(trades, wallet_map, limit=30)}
    </div>
    <div class="two-col">
      <div class="section">
        <div class="section-title">Wallet Leaderboard</div>
        {render_wallet_table(wallet_stats, wallet_map, compact=True)}
      </div>
      <div class="section">
        <div class="section-title">Per Sport</div>
        {render_sport_grid(sport_stats)}
      </div>
    </div>"""
    return page_wrap("/", body)


def render_trades_page(trades, wallet_map):
    kpis = compute_kpis(trades)
    body = f"""
    <div class="section">
      <div class="section-title">Open Bets ({kpis['open_count']})</div>
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
    return page_wrap("/trades", body)


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

        cards += f"""
        <div class="wallet-detail-card">
          <div class="wallet-detail-header">
            <div>
              <span class="badge" style="background:{'#388bfd' if w['tier']=='T1' else '#3fb950' if w['tier']=='T2' else '#8b949e'};color:{'#fff' if w['tier']!='T2' else '#000'}">{w['tier']}</span>
              <strong style="font-size:1.05em;margin-left:6px">{w['name']}</strong>
              <span class="muted" style="margin-left:8px">wt {w['weight']:.2f}</span>
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


def render_wallets_page(trades, wallet_map):
    wallet_stats = compute_wallet_stats(trades, wallet_map)
    scout = load_scout_report()
    body = f"""
    <div class="section">
      <div class="section-title">Wallet Ranking</div>
      {render_wallet_table(wallet_stats, wallet_map)}
    </div>
    <div class="section">
      <div class="section-title">Per Wallet Detail</div>
      {render_wallet_detail(trades, wallet_map, wallet_stats)}
    </div>
    <div class="section">
      <div class="section-title">Wallet Scout Rapport</div>
      {render_scout_report(scout)}
    </div>"""
    return page_wrap("/wallets", body)


def render_research_page():
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
    return page_wrap("/research", body)


# ── HTTP Server ───────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html", "/trades", "/wallets", "/research"):
            try:
                trades     = load_trades()
                wallet_map = parse_config_wallets()
                if self.path == "/trades":
                    html = render_trades_page(trades, wallet_map)
                elif self.path == "/wallets":
                    html = render_wallets_page(trades, wallet_map)
                elif self.path == "/research":
                    html = render_research_page()
                else:
                    html = render_overview(trades, wallet_map)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                err = f"<pre>Error: {e}</pre>".encode()
                self.send_response(500)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(err)
        elif self.path == "/api/trades":
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

    def log_message(self, fmt, *args):
        pass  # suppress access logs


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    print(f"Bottie Dashboard → http://0.0.0.0:{port}")
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()
