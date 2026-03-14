#!/usr/bin/env python3
"""Bottie Trading Dashboard — single-file, no external deps, port 8080."""

import json, re, glob, os
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BASE_DIR       = Path(__file__).parent
TRADES_FILE    = BASE_DIR / "data" / "trades.jsonl"
HYPOTHESES_DIR = BASE_DIR / "research" / "hypotheses"
CONFIG_FILE    = BASE_DIR / "config.yaml"
INITIAL_BANKROLL = 250.0  # $200 start + $50 deposit


# ── Data Loading ────────────────────────────────────────────────────────────

def load_trades():
    if not TRADES_FILE.exists():
        return []
    trades = []
    with open(TRADES_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass
    return trades

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
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    open_  = [t for t in filled if t.get("result") is None]
    resolved = [t for t in filled if t.get("result") in ("win", "loss")]
    wins   = [t for t in resolved if t.get("result") == "win"]

    total_pnl = sum(t.get("pnl") or 0 for t in resolved)
    win_rate  = len(wins) / len(resolved) * 100 if resolved else 0

    today = datetime.now(timezone.utc).date()
    daily_resolved = [t for t in resolved if t.get("resolved_at") and t["resolved_at"][:10] == str(today)]
    daily_pnl = sum((t.get("pnl") or 0) for t in daily_resolved)
    daily_wins = sum(1 for t in daily_resolved if t.get("result") == "win")
    daily_losses = sum(1 for t in daily_resolved if t.get("result") == "loss")

    # Open bet value (total $ in open positions)
    open_value = sum(t.get("size_usdc") or 0 for t in open_)

    return {
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "open_count": len(open_),
        "open_value": open_value,
        "resolved_count": len(resolved),
        "wins_count": len(wins),
        "losses_count": len(resolved) - len(wins),
        "daily_pnl": daily_pnl,
        "daily_wins": daily_wins,
        "daily_losses": daily_losses,
        "total_trades": len(filled),
        "dry_run": not filled and any(t.get("dry_run") for t in trades),
    }

def compute_wallet_stats(trades, wallet_map):
    filled = [t for t in trades if t.get("filled") and t.get("signal_source") == "copy" and not t.get("dry_run")]
    by_wallet = {}
    for t in filled:
        addr = (t.get("copy_wallet") or "").lower()
        if addr not in by_wallet:
            by_wallet[addr] = []
        by_wallet[addr].append(t)

    stats = []
    # Include all configured wallets
    all_addrs = set(wallet_map.keys()) | set(by_wallet.keys())
    for addr in all_addrs:
        group = by_wallet.get(addr, [])
        resolved = [t for t in group if t.get("result") in ("win", "loss")]
        wins = [t for t in resolved if t.get("result") == "win"]
        pnl = sum(t.get("pnl") or 0 for t in resolved)
        wr = len(wins) / len(resolved) * 100 if resolved else None
        avg_delay = sum(t.get("signal_delay_ms") or 0 for t in group) / len(group) / 1000 if group else 0
        info = wallet_map.get(addr, {})
        stats.append({
            "addr": addr,
            "name": info.get("name", addr[:10] + "..."),
            "tier": info.get("tier", "?"),
            "weight": info.get("weight", 0),
            "trades": len(group),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": wr,
            "pnl": pnl,
            "avg_delay_s": avg_delay,
        })
    stats.sort(key=lambda x: x["pnl"], reverse=True)
    return stats

def compute_sport_stats(trades):
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run")]
    by_sport = {}
    for t in filled:
        sport = t.get("sport") or "unknown"
        by_sport.setdefault(sport, []).append(t)

    stats = []
    for sport, group in by_sport.items():
        resolved = [t for t in group if t.get("result") in ("win", "loss")]
        wins = [t for t in resolved if t.get("result") == "win"]
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
        resolved = [t for t in group if t.get("result") in ("win", "loss")]
        wins = [t for t in resolved if t.get("result") == "win"]
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
    filled = [t for t in trades if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss")]
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
    if r == "win":   return '<span class="green">✓ WIN</span>'
    if r == "loss":  return '<span class="red">✗ LOSS</span>'
    return '<span class="yellow">⏳ open</span>'

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
    pnl = kpis["total_pnl"]
    pnl_color = "#3fb950" if pnl >= 0 else "#f85149"
    wr = kpis["win_rate"]
    wr_color = "#3fb950" if wr >= 55 else "#f85149" if wr < 45 else "#d29922"
    dpnl = kpis["daily_pnl"]
    dpnl_color = "#3fb950" if dpnl >= 0 else "#f85149"

    goal = 10000.0
    # Rough portfolio estimate: invested + PnL + open value
    estimated_portfolio = INITIAL_BANKROLL + pnl + kpis["open_value"]
    progress = min(100, max(0, estimated_portfolio / goal * 100))

    daily_detail = f'{kpis["daily_wins"]}W {kpis["daily_losses"]}L' if kpis["daily_wins"] + kpis["daily_losses"] > 0 else ""

    tiles = [
        ("Open Posities", f'${kpis["open_value"]:.0f}', "#388bfd",
         f'{kpis["open_count"]} bets'),
        ("Resolved P&L", f'{"+" if pnl >= 0 else ""}${pnl:.2f}', pnl_color,
         f'{kpis["wins_count"]}W / {kpis["losses_count"]}L'),
        ("Win Rate", f"{wr:.1f}%" if kpis["resolved_count"] else "—", wr_color,
         f'{kpis["resolved_count"]} resolved'),
        ("Vandaag", f'{"+" if dpnl >= 0 else ""}${dpnl:.2f}', dpnl_color,
         daily_detail),
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
    <div class="kpi-row">{tiles_html}</div>
    <div class="goal-bar-wrap">
      <div class="goal-label">
        DOEL: ${INITIAL_BANKROLL:.0f} → ${goal:.0f}
        <span class="muted" style="float:right">{progress:.1f}% &nbsp; ~${estimated_portfolio:.0f}</span>
      </div>
      <div class="goal-bar"><div class="goal-fill" style="width:{progress:.1f}%"></div></div>
    </div>"""

def render_open_bets(trades, wallet_map):
    open_bets = [t for t in trades if t.get("filled") and t.get("result") is None and not t.get("dry_run")]
    if not open_bets:
        return '<div class="empty">Geen open bets.</div>'
    open_bets.sort(key=lambda t: t.get("timestamp") or "", reverse=True)

    rows = ""
    for t in open_bets:
        age = fmt_age(t.get("timestamp"))
        conf = t.get("confidence") or 0
        price = t.get("price") or 0
        edge = (conf - price) / price * 100 if price > 0 and conf > price else 0
        rows += f"""
        <tr>
          <td><span class="badge sport">{t.get('sport','?')[:8]}</span></td>
          <td class="market-title">{t.get('market_title','?')}</td>
          <td><span class="badge {'green' if t.get('side')=='BUY' else 'red'}">{t.get('side','?')}</span> {t.get('outcome','')}</td>
          <td>{price:.0%}</td>
          <td style="color:#bc8cff">{conf:.0%}</td>
          <td>{edge:+.1f}%</td>
          <td>${t.get('size_usdc',0):.2f}</td>
          <td>{render_why(t, wallet_map)}</td>
          <td class="muted">{age}</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Sport</th><th>Market</th><th>Side / Outcome</th>
        <th>Entry</th><th>Conf</th><th>Edge</th><th>Size</th>
        <th>Waarom</th><th>Leeftijd</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""

def render_wallet_table(stats, wallet_map):
    if not stats:
        return '<div class="empty">Geen wallet data.</div>'
    rows = ""
    for i, w in enumerate(stats, 1):
        wr_html = fmt_pct(w["win_rate"])
        pnl_html = fmt_pnl(w["pnl"])
        dim = ' style="opacity:0.5"' if w["trades"] == 0 else ""
        rows += f"""
        <tr{dim}>
          <td class="muted">{i}</td>
          <td><span class="badge" style="background:{'#388bfd' if w['tier']=='T1' else '#3fb950' if w['tier']=='T2' else '#8b949e'};color:{'#fff' if w['tier']!='T2' else '#000'}">{w['tier']}</span></td>
          <td><strong>{w['name']}</strong></td>
          <td>{w['weight']:.2f}</td>
          <td>{w['trades']}</td>
          <td>{w['resolved']}</td>
          <td>{wr_html}</td>
          <td>{pnl_html}</td>
          <td class="muted">{w['avg_delay_s']:.1f}s</td>
        </tr>"""

    return f"""
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>#</th><th>Tier</th><th>Wallet</th><th>Weight</th>
        <th>Gekopieerd</th><th>Resolved</th><th>Win%</th><th>P&L</th><th>Delay</th>
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

def render_resolved_trades(trades, wallet_map):
    """Recently resolved trades — shows outcomes and PnL."""
    resolved = [t for t in trades if t.get("filled") and not t.get("dry_run") and t.get("result") in ("win", "loss")]
    if not resolved:
        return '<div class="empty">Nog geen resolved trades.</div>'
    resolved.sort(key=lambda t: t.get("resolved_at") or t.get("timestamp") or "", reverse=True)

    rows = ""
    for t in resolved[:50]:
        result = t.get("result", "")
        pnl = t.get("pnl") or 0
        result_html = f'<span class="green">WIN</span>' if result == "win" else f'<span class="red">LOSS</span>'
        pnl_html = fmt_pnl(pnl)
        addr = (t.get("copy_wallet") or "").lower()
        info = wallet_map.get(addr, {})
        wallet_name = info.get("name", addr[:10] + "..." if addr else "?")
        resolved_at = t.get("resolved_at") or ""
        age = fmt_age(resolved_at) if resolved_at else fmt_age(t.get("timestamp"))
        price = t.get("price") or 0
        rows += f"""
        <tr>
          <td class="muted">{age}</td>
          <td>{result_html}</td>
          <td class="market-title">{t.get('market_title','?')}</td>
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
        <th>Wanneer</th><th>Uitkomst</th><th>Market</th><th>Side</th>
        <th>Entry</th><th>Inzet</th><th>Wallet</th><th>P&L</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


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

/* Header */
.header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: 1px; }
.header-right { display: flex; gap: 16px; align-items: center; color: var(--muted); font-size: 0.8rem; }
#countdown { color: var(--yellow); font-family: monospace; }

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


def render_page(trades, wallet_map):
    kpis       = compute_kpis(trades)
    wallet_stats = compute_wallet_stats(trades, wallet_map)
    sport_stats  = compute_sport_stats(trades)
    daily_pnl    = compute_daily_pnl(trades)
    now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Bottie Dashboard</title>
  <style>{CSS}</style>
</head>
<body>
<div class="header">
  <h1>🤖 BOTTIE DASHBOARD</h1>
  <div class="header-right">
    <span>Bijgewerkt: {now_str}</span>
    <span>Refresh in <span id="countdown">30</span>s</span>
  </div>
</div>

<div class="main">

  <!-- KPIs + Goal -->
  {render_kpi_row(kpis, wallet_map)}

  <!-- Resolved trades -->
  <div class="section">
    <div class="section-title">Resolved Trades (laatste 50)</div>
    {render_resolved_trades(trades, wallet_map)}
  </div>

  <!-- Open bets -->
  <div class="section">
    <div class="section-title">Open Bets ({kpis['open_count']})</div>
    {render_open_bets(trades, wallet_map)}
  </div>

  <!-- Two-column: Wallets + Sports -->
  <div class="two-col">
    <div class="section">
      <div class="section-title">Wallet Leaderboard</div>
      {render_wallet_table(wallet_stats, wallet_map)}
    </div>
    <div class="section">
      <div class="section-title">Per Sport</div>
      {render_sport_grid(sport_stats)}
    </div>
  </div>

  <!-- PnL chart -->
  <div class="section">
    <div class="section-title">Dagelijkse P&L (14d)</div>
    {render_pnl_chart(daily_pnl)}
  </div>

  <!-- All trades -->
  <div class="section">
    <div class="section-title">Alle Trades (laatste 200)</div>
    {render_all_trades(trades, wallet_map)}
  </div>

</div>

<script>
let t = 30;
const el = document.getElementById('countdown');
if (el) setInterval(() => {{ el.textContent = --t + 's'; if(t<=0) location.reload(); }}, 1000);
</script>
</body>
</html>"""


# ── HTTP Server ───────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                trades     = load_trades()
                wallet_map = parse_config_wallets()
                html = render_page(trades, wallet_map)
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
