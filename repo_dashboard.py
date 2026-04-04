#!/usr/bin/env python3
"""
Bottie Dashboard — Cannae Monitor + Paper Trading Test
Single-file, no external deps, port 8080.

Pages:
  /         Overview  — KPIs + open bets + paper status
  /cannae   Cannae    — bias-vrije ROI, per sport, trade log
  /paper    Paper     — signals, edge accuracy, scanner log
  /ops      Ops       — bot health, USDC transfer
"""

import json, os, re, subprocess, time, urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    CET = ZoneInfo("Europe/Amsterdam")
except ImportError:
    CET = None

# ── Paths & constants ──────────────────────────────────────────────────────────
BASE_DIR          = Path.cwd()                            # /opt/bottie (workdir in service)
TRADES_FILE       = BASE_DIR / "data" / "trades.jsonl"
CONFIG_FILE       = BASE_DIR / "config.yaml"
PAPER_TRADES_FILE = Path("/opt/bottie-test/data/trades.jsonl")
SCANNER_LOG       = Path("/opt/bottie-test/data/scanner.log")

BOTTIE_ADDR       = "0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42"  # Jouw bot's PM account (Djembatuti)
CANNAE_ADDR       = "0x07921379f7b31ef93da634b688b2fe36897db778"   # Externe Cannae wallet (benchmark)
PM_DATA_API       = "https://data-api.polymarket.com"
LB_API            = "https://lb-api.polymarket.com"
AUTH_TOKEN        = os.environ.get("DASHBOARD_TOKEN", "8vNADas4jmnOk3IbpeBFrgDHkKHN9Epq")
INITIAL_CAPITAL   = float(os.environ.get("INITIAL_CAPITAL", "1400.0"))  # Jouw inleg
TARGET_CAPITAL    = 10000.0

WITHDRAW_WALLETS = {
    "Koen":     "0x87af7B1D1E76d218816313653a16183c9fa884a9",
    "Liesbeth": "0xa6B2c1c45048998729411eEf1e3001e59364D8B3",
}


# ── PM API fetch ───────────────────────────────────────────────────────────────
_pm_cache = {}  # {addr: {"data": ..., "ts": ...}}
_lb_cache = {}  # {addr: {"val": ..., "ts": ...}}

def fetch_lb_profit(addr):
    """All-time profit van lb-api. Bron van waarheid voor P&L. Cached 60s."""
    now = time.time()
    c = _lb_cache.get(addr, {"val": None, "ts": 0})
    if c["val"] is not None and now - c["ts"] < 60:
        return c["val"]
    try:
        req = urllib.request.Request(f"{LB_API}/profit?address={addr}",
              headers={"User-Agent": "Bottie/2.0", "Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        val = float(data[0]["amount"]) if isinstance(data, list) and data else None
        _lb_cache[addr] = {"val": val, "ts": now}
        return val
    except Exception:
        return c["val"]

def fetch_pm_data(addr=None):
    """PM trades + positions voor een adres. Cached 15s."""
    if addr is None:
        addr = BOTTIE_ADDR
    now = time.time()
    c = _pm_cache.get(addr, {"data": None, "ts": 0})
    if c["data"] and now - c["ts"] < 15:
        return c["data"]

    result = {"trades": [], "positions": [], "positions_value": 0, "cash": 0, "error": None}

    def pm_get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Bottie/2.0", "Accept": "application/json"})
        return urllib.request.urlopen(req, timeout=15)

    # Open positions (sizeThreshold=0.01 om resolved te skippen)
    try:
        all_pos, poffset = [], 0
        while poffset <= 2000:
            batch = json.loads(pm_get(f"{PM_DATA_API}/positions?user={addr}&limit=500&offset={poffset}&sizeThreshold=0.01").read())
            all_pos.extend(batch)
            if len(batch) < 500:
                break
            poffset += 500
        result["positions"] = all_pos
        result["positions_value"] = sum(float(p.get("currentValue") or 0) for p in all_pos)
    except Exception as e:
        result["error"] = f"positions: {e}"

    # Cash: USDC on Polygon via RPC
    try:
        addr_hex = addr.lower().replace("0x", "")
        payload = json.dumps({"jsonrpc":"2.0","method":"eth_call",
            "params":[{"to":"0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                       "data":"0x70a08231"+addr_hex.rjust(64,"0")},"latest"],"id":1})
        req = urllib.request.Request("https://rpc.ankr.com/polygon/d7e57b7d62eaba6b7c434153660caddfc0a9445537e9073bcc3823b4f8080bc8",
              data=payload.encode(), headers={"Content-Type":"application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=8).read())
        result["cash"] = int(resp.get("result","0x0"), 16) / 1e6
    except Exception:
        pass

    _pm_cache[addr] = {"data": result, "ts": now}
    return result


# ── Data loading ───────────────────────────────────────────────────────────────
def sf(v, d=0.0):
    try: return float(v)
    except: return d

def load_trades():
    """Cannae copy trades — dry_run=False, filters crypto up/down."""
    if not TRADES_FILE.exists():
        return []
    trades = []
    try:
        with open(TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    t = json.loads(line)
                    if t.get("dry_run"): continue
                    title = (t.get("market_title") or "").lower()
                    if any(x in title for x in ["up or down","bitcoin up","ethereum up","solana up","xrp up"]):
                        continue
                    trades.append(t)
                except Exception:
                    pass
    except Exception:
        pass
    return trades

def load_paper_trades():
    """Paper signals — dry_run=True, signal_source=paper_odds."""
    if not PAPER_TRADES_FILE.exists():
        return []
    trades = []
    try:
        with open(PAPER_TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    t = json.loads(line)
                    if t.get("dry_run") and t.get("signal_source") == "paper_odds":
                        trades.append(t)
                except Exception:
                    pass
    except Exception:
        pass
    return sorted(trades, key=lambda t: t.get("timestamp",""), reverse=True)


# ── KPI computation ────────────────────────────────────────────────────────────
def compute_bottie_portfolio():
    """
    Jouw eigen portfolio:
    - cash = USDC balans op Polygon (RPC, bron van waarheid)
    - positions = huidige open bets waarde (currentValue)
    - portfolio = cash + positions_value  (direct van PM, geen berekening)
    - pnl = portfolio - INITIAL_CAPITAL
    """
    lb_profit = fetch_lb_profit(BOTTIE_ADDR)
    pm = fetch_pm_data(BOTTIE_ADDR)
    pos_value = pm["positions_value"]
    cash = pm["cash"]
    open_count = len([p for p in pm["positions"] if sf(p.get("size",0)) > 0])

    portfolio = cash + pos_value
    pnl = portfolio - INITIAL_CAPITAL

    return {
        "portfolio": portfolio,
        "pnl": pnl,
        "lb_profit": lb_profit,
        "pos_value": pos_value,
        "cash": cash,
        "open_count": open_count,
        "error": pm.get("error"),
    }

def compute_cannae_kpis():
    """Cannae benchmark — zijn ROI op PM. Alleen voor vergelijking."""
    pm = fetch_pm_data(CANNAE_ADDR)
    buys  = [t for t in pm["trades"] if (t.get("side") or "").upper() == "BUY"]
    sells = [t for t in pm["trades"] if (t.get("side") or "").upper() == "SELL"]
    active = [p for p in pm["positions"] if sf(p.get("size",0)) > 0]

    total_bought = sum(sf(t.get("size",0)) * sf(t.get("price",0)) for t in buys)
    total_sold   = sum(sf(t.get("size",0)) * sf(t.get("price",0)) for t in sells)
    pos_value    = sum(sf(p.get("currentValue",0)) for p in active)

    roi = (total_sold + pos_value - total_bought) / total_bought * 100 if total_bought > 0 else 0
    lb_profit = fetch_lb_profit(CANNAE_ADDR)

    return {
        "total_bought": total_bought, "total_sold": total_sold,
        "pos_value": pos_value,
        "roi": roi, "lb_profit": lb_profit,
        "open_count": len(active),
        "buy_count": len(buys), "sell_count": len(sells),
        "error": pm.get("error"),
    }

def fetch_bot_live_stats():
    """
    Leest de meest recente STATUS:-regel uit de bot logs.
    Bot logt dit elke 5 minuten — altijd actueel, geen PM API nodig.
    Format: STATUS: Trades: N | W/L: W/L | Win rate: X% | PnL: $X | ... | daily_pnl=$X | open=N
    """
    try:
        r = subprocess.run(
            ["journalctl", "-u", "bottie", "--since", "10 min ago", "--no-pager"],
            capture_output=True, text=True, timeout=5
        )
        for line in reversed(r.stdout.splitlines()):
            if "STATUS:" not in line:
                continue
            trades = wins = losses = wr = pnl = daily_pnl = open_n = None
            m = re.search(r"Trades:\s*(\d+)", line)
            if m: trades = int(m.group(1))
            m = re.search(r"W/L:\s*(\d+)/(\d+)", line)
            if m: wins, losses = int(m.group(1)), int(m.group(2))
            m = re.search(r"Win rate:\s*([\d.]+)%", line)
            if m: wr = float(m.group(1))
            m = re.search(r"PnL:\s*\$([\d.+-]+)", line)
            if m: pnl = float(m.group(1))
            m = re.search(r"daily_pnl=\$([\d.+-]+)", line)
            if m: daily_pnl = float(m.group(1))
            m = re.search(r"open=(\d+)", line)
            if m: open_n = int(m.group(1))
            if wr is not None:
                resolved = (wins or 0) + (losses or 0)
                return {
                    "resolved": resolved, "wins": wins or 0, "losses": losses or 0,
                    "wr": wr, "total_pnl": pnl or 0, "open": open_n or 0,
                    "daily_pnl": daily_pnl or 0, "daily_count": 0,
                    "source": "live",
                }
    except Exception:
        pass
    return None


def compute_local_kpis(trades):
    """WR + PnL van trades.jsonl — fallback als bot logs niet beschikbaar."""
    resolved = [t for t in trades if t.get("filled") and
                t.get("result") in ("win","loss","take_profit","sold")]
    wins = [t for t in resolved if t.get("result") in ("win","take_profit")]
    open_t = [t for t in trades if t.get("filled") and t.get("result") is None]
    total_pnl = sum(t.get("pnl") or 0 for t in resolved)

    today = datetime.now(timezone.utc).date()
    daily = [t for t in resolved if (t.get("resolved_at") or "")[:10] == str(today)]
    daily_pnl = sum(t.get("pnl") or 0 for t in daily)

    return {
        "resolved": len(resolved), "wins": len(wins),
        "losses": len(resolved) - len(wins),
        "wr": len(wins)/len(resolved)*100 if resolved else 0,
        "total_pnl": total_pnl, "open": len(open_t),
        "daily_pnl": daily_pnl, "daily_count": len(daily),
        "source": "file",
    }

def compute_paper_stats(paper):
    """Stats voor paper trading experiment."""
    resolved = [t for t in paper if t.get("result") in ("win","loss","refund")]
    open_t   = [t for t in paper if t.get("result") is None]
    wins     = [t for t in resolved if t.get("result") == "win"]
    invested = sum(t.get("size_usdc") or 0 for t in resolved)
    total_pnl = sum(t.get("pnl") or 0 for t in resolved)

    # Edge accuracy: bm_prob > pm_price correct?
    ea_correct, ea_total = 0, 0
    for t in resolved:
        bm = t.get("_bm_prob")
        if bm is None: continue
        ea_total += 1
        pm_price = t.get("price", 0)
        if (bm > pm_price and t.get("result") == "win") or \
           (bm <= pm_price and t.get("result") == "loss"):
            ea_correct += 1

    # Per sport
    by_sport = {}
    for t in paper:
        s = (t.get("sport") or "unknown").replace("soccer_","").replace("basketball_","")
        by_sport.setdefault(s, []).append(t)
    sport_stats = []
    for sport, grp in sorted(by_sport.items()):
        gr = [t for t in grp if t.get("result") in ("win","loss")]
        gw = [t for t in gr if t.get("result") == "win"]
        g_pnl = sum(t.get("pnl") or 0 for t in gr)
        g_inv = sum(t.get("size_usdc") or 0 for t in gr)
        sport_stats.append({
            "sport": sport, "total": len(grp),
            "open": len([t for t in grp if t.get("result") is None]),
            "resolved": len(gr), "wins": len(gw),
            "wr": len(gw)/len(gr)*100 if gr else 0,
            "pnl": g_pnl, "roi": g_pnl/g_inv*100 if g_inv > 0 else 0,
        })
    return {
        "total": len(paper), "open": len(open_t),
        "resolved": len(resolved), "wins": len(wins),
        "losses": len(resolved)-len(wins),
        "wr": len(wins)/len(resolved)*100 if resolved else 0,
        "total_pnl": total_pnl,
        "roi": total_pnl/invested*100 if invested > 0 else 0,
        "avg_edge": sum(t.get("edge_pct") or 0 for t in paper)/len(paper) if paper else 0,
        "edge_accuracy": ea_correct/ea_total*100 if ea_total else 0,
        "edge_total": ea_total,
        "sport_stats": sport_stats,
    }

def compute_sport_stats(trades):
    by_sport = {}
    for t in [t for t in trades if t.get("filled")]:
        by_sport.setdefault(t.get("sport") or "unknown", []).append(t)
    stats = []
    for sport, grp in sorted(by_sport.items()):
        res = [t for t in grp if t.get("result") in ("win","loss","take_profit","sold")]
        wins = [t for t in res if t.get("result") in ("win","take_profit")]
        pnl = sum(t.get("pnl") or 0 for t in res)
        stats.append({"sport":sport,"total":len(grp),"resolved":len(res),
                      "wins":len(wins),"wr":len(wins)/len(res)*100 if res else 0,"pnl":pnl})
    return sorted(stats, key=lambda x: abs(x["pnl"]), reverse=True)

def compute_daily_pnl(trades):
    by_day = {}
    for t in [t for t in trades if t.get("filled") and
              t.get("result") in ("win","loss","take_profit","sold")]:
        day = (t.get("resolved_at") or t.get("timestamp") or "")[:10]
        if not day: continue
        by_day.setdefault(day, {"pnl":0,"wins":0,"losses":0})
        by_day[day]["pnl"] += t.get("pnl") or 0
        if t.get("result") in ("win","take_profit"):
            by_day[day]["wins"] += 1
        else:
            by_day[day]["losses"] += 1
    return [{"day":d,**v} for d,v in sorted(by_day.items())[-14:]]


# ── Formatters ─────────────────────────────────────────────────────────────────
def fp(v):
    if v is None: return "-"
    c = "var(--green)" if v >= 0 else "var(--red)"
    s = "+" if v >= 0 else ""
    return f'<span style="color:{c}">{s}${v:.2f}</span>'

def fpct(v):
    if v is None: return "-"
    c = "var(--green)" if v >= 0 else "var(--red)"
    s = "+" if v >= 0 else ""
    return f'<span style="color:{c}">{s}{v:.1f}%</span>'

def fage(ts_str):
    if not ts_str: return ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z","+00:00"))
        d = datetime.now(timezone.utc) - ts
        h = int(d.total_seconds()//3600)
        m = int((d.total_seconds()%3600)//60)
        if h >= 48: return f"{h//24}d"
        if h >= 1:  return f"{h}h{m:02d}m"
        return f"{m}m"
    except: return ""

def sport_badge(sport):
    c = {"epl":"#3fb950","bundesliga":"#d29922","la_liga":"#f0883e","serie_a":"#388bfd",
         "ligue_1":"#bc8cff","champs_league":"#ffd700","nba":"#ff6b35","nhl":"#00bfff",
         "nfl":"#3fb950","mlb":"#f85149","champions_league":"#ffd700"}.get(
         (sport or "").replace("soccer_","").replace("basketball_",""), "#666")
    s = (sport or "").replace("soccer_","").replace("basketball_","").replace("_"," ")
    return f'<span style="background:{c}22;color:{c};border:1px solid {c}44;border-radius:3px;padding:1px 6px;font-size:0.7rem;font-weight:600">{s.upper()[:14]}</span>'

def badge(text, cls):
    colors = {"win":"#3fb950","loss":"#f85149","open":"#388bfd","refund":"#7d8590","take_profit":"#3fb950"}
    c = colors.get(cls, "#888")
    return f'<span style="background:{c}22;color:{c};border:1px solid {c}44;border-radius:3px;padding:1px 7px;font-size:0.72rem;font-weight:600">{text}</span>'


# ── CSS ────────────────────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;font-size:14px;line-height:1.5}
a{color:inherit;text-decoration:none}
:root{--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#388bfd;--purple:#bc8cff;
      --border:#21262d;--surface:#161b22;--muted:#7d8590;--bg:#0d1117}
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;
        display:flex;align-items:center;justify-content:space-between;gap:12px}
.header h1{font-size:0.95rem;font-weight:700;letter-spacing:.05em}
.header-right{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:0.78rem;flex-wrap:wrap}
.nav{display:flex;background:var(--surface);border-bottom:1px solid var(--border);overflow-x:auto}
.nav a{padding:10px 20px;color:var(--muted);font-size:0.82rem;font-weight:600;
       border-bottom:2px solid transparent;white-space:nowrap}
.nav a:hover{color:#e6edf3;background:rgba(255,255,255,0.03)}
.nav a.active{color:var(--blue);border-bottom-color:var(--blue)}
.main{padding:20px;max-width:1400px}
.section{margin-bottom:26px}
.stitle{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;
        color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.krow{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.ktile{background:var(--surface);border:1px solid var(--border);border-radius:6px;
       padding:14px 16px;min-width:130px;flex:1}
.klabel{font-size:.68rem;color:var(--muted);font-weight:600;text-transform:uppercase;
        letter-spacing:.08em;margin-bottom:3px}
.kval{font-size:1.35rem;font-weight:700;line-height:1.2}
.ksub{font-size:.7rem;color:var(--muted);margin-top:3px}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{text-align:left;padding:7px 10px;color:var(--muted);font-weight:600;font-size:.7rem;
   text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--surface)}
td{padding:6px 10px;border-bottom:1px solid #161b22}
tr:hover td{background:rgba(255,255,255,0.02)}
.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}
.blue{color:var(--blue)}.muted{color:var(--muted)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
.banner{background:#1c2128;border:1px solid #388bfd44;border-left:3px solid var(--blue);
        border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:.8rem}
.warn-banner{background:#1c1a10;border:1px solid #d2992244;border-left:3px solid var(--yellow);
             border-radius:6px;padding:8px 14px;margin-bottom:14px;font-size:.78rem;color:#d29922}
.empty{color:var(--muted);font-style:italic;padding:12px 0;font-size:.82rem}
.chart-row{display:flex;align-items:center;gap:8px;padding:2px 0}
.chart-bar{height:16px;border-radius:2px;min-width:2px}
code{background:#1c2128;border-radius:3px;padding:1px 5px;font-size:.8rem}
@media(max-width:768px){.ktile{min-width:calc(50% - 5px)}.main{padding:12px}}
"""


# ── Render components ──────────────────────────────────────────────────────────
def ktile(label, val, color="#e6edf3", sub="", top_border=""):
    b = f"border-top:3px solid {top_border};" if top_border else ""
    s = f'<div class="ksub">{sub}</div>' if sub else ""
    return f'<div class="ktile" style="{b}"><div class="klabel">{label}</div><div class="kval" style="color:{color}">{val}</div>{s}</div>'

def render_daily_chart(buckets):
    if not buckets: return '<div class="empty">Geen data</div>'
    mx = max(abs(b["pnl"]) for b in buckets) or 1
    html = ""
    for b in buckets[-14:]:
        pnl = b["pnl"]
        w = max(2, int(abs(pnl)/mx*180))
        c = "var(--green)" if pnl >= 0 else "var(--red)"
        html += f'<div class="chart-row"><span style="width:48px;font-size:.7rem;color:var(--muted);text-align:right">{b["day"][5:]}</span><div class="chart-bar" style="width:{w}px;background:{c}"></div><span style="font-size:.74rem;color:{c}">{"+" if pnl>=0 else ""}${pnl:.2f}</span><span style="font-size:.7rem;color:var(--muted);margin-left:4px">{b["wins"]}W/{b["losses"]}L</span></div>'
    return html

def render_open_bets(trades):
    rows = [t for t in trades if t.get("filled") and t.get("result") is None and not t.get("dry_run")]
    if not rows: return '<div class="empty">Geen open bets</div>'
    html = ""
    for t in sorted(rows, key=lambda x: x.get("timestamp",""), reverse=True)[:60]:
        out = t.get("outcome","?")
        price = t.get("price",0)
        size = t.get("size_usdc",0)
        html += f"<tr><td>{sport_badge(t.get('sport',''))}</td><td>{(t.get('market_title') or '?')[:52]}</td><td>{badge(out,'open')}</td><td>{price:.3f}</td><td>${size:.2f}</td><td class='muted'>{fage(t.get('timestamp',''))}</td><td class='muted'>{t.get('signal_source','')[:12]}</td></tr>"
    return f'<div class="tw"><table><thead><tr><th>Sport</th><th>Markt</th><th>Side</th><th>Prijs</th><th>Size</th><th>Leeftijd</th><th>Bron</th></tr></thead><tbody>{html}</tbody></table></div>'

def render_resolved_trades(trades, limit=60):
    rows = [t for t in trades if t.get("filled") and
            t.get("result") in ("win","loss","refund","take_profit","sold")]
    if not rows: return '<div class="empty">Geen resolved trades</div>'
    rows = sorted(rows, key=lambda t: t.get("resolved_at") or t.get("timestamp",""), reverse=True)[:limit]
    html = ""
    for t in rows:
        res = t.get("result","?")
        cls = "win" if res in ("win","take_profit") else "loss" if res == "loss" else "refund"
        pnl = t.get("pnl")
        html += f"<tr><td>{sport_badge(t.get('sport',''))}</td><td title='{t.get('market_title','')}'>{(t.get('market_title') or '?')[:50]}</td><td>{badge(t.get('outcome','?'),'open')}</td><td>{t.get('price',0):.3f}</td><td>{badge(res,cls)}</td><td>{fp(pnl) if pnl is not None else ''}</td><td class='muted'>{(t.get('resolved_at') or '')[:10]}</td></tr>"
    return f'<div class="tw"><table><thead><tr><th>Sport</th><th>Markt</th><th>Side</th><th>Prijs</th><th>Resultaat</th><th>PnL</th><th>Datum</th></tr></thead><tbody>{html}</tbody></table></div>'

def render_paper_trades(paper, open_only=False, resolved_only=False):
    trades = paper
    if open_only:   trades = [t for t in paper if t.get("result") is None]
    if resolved_only: trades = [t for t in paper if t.get("result") in ("win","loss","refund")]
    if not trades: return '<div class="empty">Geen paper trades</div>'
    html = ""
    for t in trades[:100]:
        res = t.get("result")
        res_badge = badge(res, "win" if res=="win" else "loss" if res=="loss" else "refund") if res else badge("open","open")
        bm = t.get("_bm_prob")
        edge = t.get("edge_pct", 0)
        ec = "var(--green)" if edge >= 10 else "var(--yellow)" if edge >= 3 else "var(--red)"
        pnl = t.get("pnl")
        bm_str = f"{bm:.1%}" if bm is not None else "—"
        pm_str = f"{t.get('price',0):.1%}"
        ts = (t.get("timestamp","") or "")[:16].replace("T"," ")
        size = t.get("size_usdc", 5)
        # Build signal card for mobile-friendly display
        html += f"""<tr>
<td>{sport_badge(t.get('sport',''))}</td>
<td><div style='font-weight:600;font-size:.8rem'>{(t.get('market_title') or '?')[:52]}</div>
<div style='font-size:.7rem;color:var(--muted)'>{ts} UTC · ${size:.2f} flat</div></td>
<td>{badge(t.get('outcome','?'),'open')}</td>
<td><div>{pm_str}</div><div style='font-size:.7rem;color:var(--muted)'>PM prijs</div></td>
<td><div>{bm_str}</div><div style='font-size:.7rem;color:var(--muted)'>BM fair</div></td>
<td style='color:{ec};font-weight:700'>{edge:.1f}pp</td>
<td>{res_badge}</td>
<td>{fp(pnl) if pnl is not None else '<span class="muted">—</span>'}</td>
</tr>"""
    return f'<div class="tw"><table><thead><tr><th>Sport</th><th>Markt</th><th>Side</th><th>PM</th><th>BM fair</th><th>Edge</th><th>Status</th><th>PnL</th></tr></thead><tbody>{html}</tbody></table></div>'

def render_scanner_log():
    if not SCANNER_LOG.exists(): return '<div class="empty">Geen scanner log</div>'
    try: lines = SCANNER_LOG.read_text().splitlines()[-600:]
    except: return '<div class="empty">Onleesbaar</div>'
    runs, cur = [], None
    for line in lines:
        if "PAPER SIGNAL SCANNER — start" in line:
            cur = {"ts": line[:19], "signals": [], "totaal": 0}
        elif "PAPER SIGNAL SCANNER — klaar" in line and cur:
            runs.append(cur); cur = None
        elif cur and "SIGNAL" in line:
            cur["signals"].append(line[27:].strip()[:120] if len(line) > 27 else line)
        elif cur:
            m = re.search(r"Totaal nieuwe signalen:\s*(\d+)", line)
            if m: cur["totaal"] = int(m.group(1))
    if not runs: return '<div class="empty">Nog geen scanner runs</div>'
    html = ""
    for run in reversed(runs[-10:]):
        sigs = "".join(f'<div style="color:var(--green);font-size:.74rem;padding:1px 0">→ {s}</div>' for s in run["signals"])
        if not sigs: sigs = '<div class="muted" style="font-size:.74rem">Geen signalen</div>'
        nc = "var(--green)" if run["totaal"] > 0 else "var(--muted)"
        html += f'<div style="background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:8px 12px;margin-bottom:6px"><div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="font-size:.8rem;font-weight:600">{run["ts"]}</span><span style="color:{nc};font-size:.8rem">{run["totaal"]} signalen</span></div>{sigs}</div>'
    return html

def render_live_board(trades):
    """Games die Cannae gepolled heeft — uit bot logs + schedule cache."""
    SCHEDULE = BASE_DIR / "data" / "schedule_cache.json"

    # Parse log lines: $  1234 | N legs (types) | HH:MM UTC | sizing | slug
    cannae_games = {}
    try:
        r = subprocess.run(["journalctl","-u","bottie","--since","90 min ago","--no-pager"],
                           capture_output=True, text=True, timeout=8)
        for line in r.stdout.splitlines():
            if "legs (" in line and "|" in line and "$" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 5:
                    slug = parts[-1].strip()
                    try: amt = float(parts[0].split("$")[-1].strip().replace(",",""))
                    except: amt = 0
                    cannae_games[slug] = {"amt": amt, "legs": parts[1], "sizing": parts[3]}
    except: pass

    if not cannae_games:
        return '<div class="empty">Geen Cannae poll data in laatste 90 min.</div>'

    # Load schedule
    sched_by_slug = {}
    try:
        games = json.load(open(SCHEDULE))
        for g in games:
            s = g.get("event_slug","")
            if s: sched_by_slug[s] = g
    except: pass

    # Filled slugs
    filled = {t["event_slug"] for t in trades if t.get("filled") and not t.get("dry_run") and t.get("event_slug")}

    now = datetime.now(timezone.utc)
    rows_data = []
    for slug, info in cannae_games.items():
        g = sched_by_slug.get(slug)
        start = None
        if g and g.get("start_time"):
            try: start = datetime.fromisoformat(g["start_time"].replace("Z","+00:00"))
            except: pass
        diff_min = (start - now).total_seconds()/60 if start else None
        if diff_min is not None and diff_min < -180: continue  # wedstrijd lang geleden
        rows_data.append((slug, g, start, diff_min, info))

    rows_data.sort(key=lambda x: x[2] or datetime(2099,1,1,tzinfo=timezone.utc))

    html = ""
    for slug, g, start, diff_min, info in rows_data[:30]:
        title = (g.get("title","") if g else "") or slug
        league = slug.split("-")[0].upper() if slug else "?"
        amt = info["amt"]
        sizing = info["sizing"]
        is_filled = slug in filled

        if diff_min is None:
            time_str = "??:??"
            status = '<span class="muted">geen tijd</span>'
        elif diff_min < -60:
            time_str = start.astimezone(CET).strftime("%H:%M") if CET else start.strftime("%H:%M")
            status = '<span style="color:var(--muted)">afgelopen</span>'
        elif diff_min < 0:
            time_str = start.astimezone(CET).strftime("%H:%M") if CET else start.strftime("%H:%M")
            status = f'<span style="color:var(--green);font-weight:600">LIVE {abs(diff_min):.0f}m</span>'
        elif diff_min < 15:
            time_str = start.astimezone(CET).strftime("%H:%M") if CET else start.strftime("%H:%M")
            status = f'<span style="color:var(--red);font-weight:600">T-{diff_min:.0f}m!</span>'
        elif diff_min < 60:
            time_str = start.astimezone(CET).strftime("%H:%M") if CET else start.strftime("%H:%M")
            status = f'<span style="color:var(--yellow)">{diff_min:.0f}m</span>'
        else:
            time_str = start.astimezone(CET).strftime("%H:%M") if CET else start.strftime("%H:%M")
            status = f'<span class="muted">{diff_min/60:.1f}h</span>'

        fill_badge = ' ' + badge("FILLED","win") if is_filled else ""
        opacity = "opacity:.45;" if is_filled else ""
        html += f"<tr style='{opacity}'><td style='font-weight:600'>{time_str}</td><td>{sport_badge(league.lower())}</td><td>{title[:52]}{fill_badge}</td><td style='color:var(--blue)'>${amt:,.0f}</td><td style='font-size:.72rem;color:var(--muted)'>{sizing[:40]}</td><td>{status}</td></tr>"

    return f'<div class="tw"><table><thead><tr><th>CET</th><th>League</th><th>Game</th><th>Cannae $</th><th>Onze types</th><th>Status</th></tr></thead><tbody>{html}</tbody></table></div>'


def render_bot_status():
    html = ""
    for svc, label in [("bottie","Cannae"), ("bottie-test","Paper Bot")]:
        try:
            r = subprocess.run(["systemctl","is-active",svc], capture_output=True, text=True, timeout=3)
            active = r.stdout.strip() == "active"
        except: active = False
        c = "var(--green)" if active else "var(--red)"
        html += f'<span><span class="dot" style="background:{c}"></span>{label}</span>'
    return html

def render_events(trades):
    open_t = [t for t in trades if t.get("filled") and t.get("result") is None and t.get("event_slug")]
    events = {}
    for t in open_t:
        events.setdefault(t.get("event_slug","?"), []).append(t)
    if not events: return '<div class="empty">Geen open events</div>'
    cards = ""
    for slug, legs in sorted(events.items(), key=lambda x: -len(x[1]))[:20]:
        title = re.sub(r'\(.*?\)','', legs[0].get("market_title","") or slug).strip()[:44]
        leg_html = " ".join(f'<span style="font-size:.7rem;background:#161b22;border-radius:3px;padding:1px 5px">{l.get("outcome","?")} {l.get("price",0):.2f} ${l.get("size_usdc",0):.1f}</span>' for l in legs)
        cards += f'<div style="background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:10px;min-width:240px;flex:1"><div style="font-size:.7rem;color:var(--muted);margin-bottom:3px">{sport_badge(legs[0].get("sport",""))}</div><div style="font-weight:600;font-size:.83rem;margin-bottom:5px">{title}</div><div>{leg_html}</div></div>'
    return f'<div style="display:flex;flex-wrap:wrap;gap:8px">{cards}</div>'


# ── Pages ─────────────────────────────────────────────────────────────────────
def page_wrap(active, body, token="", bankroll=None, pnl=None):
    now_str = datetime.now(CET if CET else timezone.utc).strftime("%Y-%m-%d %H:%M %Z")
    prefix = f"/t/{token}" if token else ""
    nav = "".join(
        f'<a href="{prefix}{href}" class="{"active" if href==active else ""}">{lbl}</a>'
        for lbl, href in [("Overview","/"),("Cannae","/cannae"),("Paper Test","/paper"),("Ops","/ops")]
    )
    ar = '' if active == "/ops" else '<meta http-equiv="refresh" content="30">'
    cjs = '' if active == "/ops" else 'let _t=30,_e=document.getElementById("cd");if(_e)setInterval(()=>{_e.textContent=--_t;if(_t<=0)location.reload()},1000);'
    ch = '' if active == "/ops" else '<span id="cd" class="muted">30</span><span class="muted">s</span>'
    bankroll_html = ""
    if bankroll is not None:
        brc = "var(--green)" if (pnl or 0) >= 0 else "var(--red)"
        pnl_s = (f'<span style="color:{brc};margin-left:6px;font-size:.8rem">({"+" if (pnl or 0)>=0 else ""}{pnl:+.2f})</span>' if pnl is not None else "")
        bankroll_html = f'<span style="font-weight:700">${bankroll:,.2f}{pnl_s}</span>'
    return f"""<!DOCTYPE html><html lang="nl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">{ar}
<title>Bottie</title><style>{CSS}</style></head><body>
<div class="header"><h1>BOTTIE</h1><div class="header-right">{render_bot_status()}{bankroll_html}{ch}<span>{now_str}</span></div></div>
<div class="nav">{nav}</div>
<div class="main">{body}</div>
<script>{cjs}</script></body></html>"""


def page_overview(trades, token=""):
    bp   = compute_bottie_portfolio()
    lk   = fetch_bot_live_stats() or compute_local_kpis(trades)
    p    = load_paper_trades()
    ps   = compute_paper_stats(p)
    daily = compute_daily_pnl(trades)

    bankroll = bp["portfolio"]
    pct_to_goal = bankroll / TARGET_CAPITAL * 100
    bar_w = min(100, int(pct_to_goal))
    pnl = bp["pnl"]
    rc = "var(--green)" if pnl >= 0 else "var(--red)"
    cash_str = f'cash: ${bp["cash"]:,.2f}' if bp["cash"] else "cash: —"
    open_str = f' + ${bp["pos_value"]:.2f} open' if bp["pos_value"] > 0 else ""

    progress = f'''<div style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:14px 16px;margin-bottom:18px">
<div style="display:flex;justify-content:space-between;margin-bottom:6px">
  <span style="font-weight:700;font-size:1.1rem">${bankroll:,.2f}</span>
  <span style="color:var(--muted);font-size:.8rem">doel ${TARGET_CAPITAL:,.0f} · {pct_to_goal:.1f}%</span>
</div>
<div style="background:#21262d;border-radius:3px;height:8px;overflow:hidden">
  <div style="background:var(--green);width:{bar_w}%;height:100%;border-radius:3px"></div>
</div>
<div style="display:flex;justify-content:space-between;margin-top:5px;font-size:.72rem;color:var(--muted)">
  <span>inleg ${INITIAL_CAPITAL:,.0f}</span>
  <span style="color:{rc}">{"+" if pnl>=0 else ""}${pnl:,.2f} ({cash_str}{open_str})</span>
</div>
</div>'''

    body = f"""
{progress}
<div class="krow">
{ktile("Win Rate", f"{lk['wr']:.1f}%", "var(--green)" if lk['wr']>=55 else "var(--yellow)", f"{lk['wins']}W / {lk['losses']}L · {lk['resolved']} resolved" + (" · live" if lk.get('source')=='live' else ""))}
{ktile("Totaal P&L", fp(lk['total_pnl']), "var(--green)" if lk['total_pnl']>=0 else "var(--red)", f"vandaag: {fp(lk['daily_pnl'])}" + (" · live" if lk.get('source')=='live' else ""))}
{ktile("Open Bets", str(lk["open"]), "var(--blue)", "live" if lk.get('source')=='live' else "wacht op resolutie")}
{ktile("Paper Signalen", str(ps['total']), "var(--purple)", f"{ps['open']} open · {ps['resolved']} resolved")}
</div>
<div class="section"><div class="stitle">Cannae Games — Live Board</div>{render_live_board(trades)}</div>
<div class="section"><div class="stitle">Open Bets ({lk["open"]})</div>{render_open_bets(trades)}</div>
<div class="section"><div class="stitle">Open Paper Trades ({ps["open"]})</div>
<div class="banner">📊 Paper Trading Test — Paasweekend 2026. Bookmaker vs PM mispricing. Geen echt geld.</div>
{render_paper_trades(p, open_only=True)}</div>
<div class="section"><div class="stitle">Dagelijkse P&L (14d)</div>{render_daily_chart(daily)}</div>
<div class="section"><div class="stitle">Events met open legs</div>{render_events(trades)}</div>"""
    return page_wrap("/", body, token, bankroll=bankroll, pnl=pnl)


def page_cannae(trades, token=""):
    ck = compute_cannae_kpis()
    lk = compute_local_kpis(trades)
    ss = compute_sport_stats(trades)
    daily = compute_daily_pnl(trades)

    rc = "var(--green)" if ck["roi"] >= 0 else "var(--red)"
    sport_html = ""
    for s in ss[:12]:
        wc = "var(--green)" if s["wr"]>=55 else "var(--yellow)" if s["wr"]>=45 else "var(--red)"
        pc = "var(--green)" if s["pnl"]>=0 else "var(--red)"
        sport_html += f'<div class="ktile" style="flex:0 0 150px">{sport_badge(s["sport"])}<div style="margin-top:5px;font-size:1rem;font-weight:700;color:{wc}">{s["wr"]:.0f}% WR</div><div style="font-size:.74rem;color:{pc}">{"+" if s["pnl"]>=0 else ""}${s["pnl"]:.2f}</div><div class="muted" style="font-size:.7rem">{s["resolved"]} resolved</div></div>'

    bp = compute_bottie_portfolio()
    lk = compute_local_kpis(trades)
    bankroll = bp["portfolio"]
    body = f"""
{"" if not ck["error"] else f'<div class="warn-banner">⚠ Cannae API: {ck["error"]}</div>'}
<div class="warn-banner" style="font-size:.74rem">Cannae = externe trader (benchmark). Jouw portfolio staat bovenaan.</div>
<div class="krow">
{ktile("Jouw Bankroll", f"${bankroll:,.2f}", "#e6edf3", f"inleg ${INITIAL_CAPITAL:,.0f} + ${bp['pnl']:,.2f} P&L")}
{ktile("Win Rate", f"{lk['wr']:.1f}%", "var(--green)" if lk['wr']>=55 else "var(--yellow)", f"{lk['wins']}W / {lk['losses']}L")}
{ktile("Vandaag", fp(lk["daily_pnl"]), "var(--green)" if lk["daily_pnl"]>=0 else "var(--red)", f"{lk['daily_count']} trades")}
{ktile("Cannae ROI", fpct(ck["roi"]), rc, f"benchmark (extern)")}
{ktile("Cannae lb P&L", f'${ck["lb_profit"]:,.0f}' if ck["lb_profit"] else "—", "var(--muted)", "zijn totale winst")}
{ktile("Cannae open", str(ck['open_count']), "var(--blue)", f"${ck['pos_value']:.0f} waarde")}
</div>
<div class="section"><div class="stitle">Per Sport (jouw bot)</div><div class="krow" style="flex-wrap:wrap">{sport_html}</div></div>
<div class="section"><div class="stitle">Dagelijkse P&L (14d)</div>{render_daily_chart(daily)}</div>
<div class="section"><div class="stitle">Open Bets</div>{render_open_bets(trades)}</div>
<div class="section"><div class="stitle">Laatste 100 Resolved</div>{render_resolved_trades(trades, 100)}</div>"""
    return page_wrap("/cannae", body, token, bankroll=bankroll, pnl=bp["pnl"])


def page_paper(token=""):
    p  = load_paper_trades()
    ps = compute_paper_stats(p)

    wc = "var(--green)" if ps["wr"]>=55 else "var(--yellow)" if ps["wr"]>=45 else "var(--muted)"
    rc = "var(--green)" if ps["roi"]>=0 else "var(--red)"
    ec = "var(--green)" if ps["edge_accuracy"]>=60 else "var(--yellow)"

    sport_html = ""
    for s in ps["sport_stats"]:
        swc = "var(--green)" if s["wr"]>=55 else "var(--yellow)" if s["wr"]>=45 else "var(--muted)"
        spc = "var(--green)" if s["pnl"]>=0 else "var(--red)"
        rl = f"{s['wins']}W/{s['resolved']-s['wins']}L" if s["resolved"] else f"{s['open']} open"
        sport_html += f'<div class="ktile" style="flex:0 0 150px">{sport_badge(s["sport"])}<div style="margin-top:5px;font-size:1rem;font-weight:700;color:{swc}">{"%.0f%%" % s["wr"] if s["resolved"] else "—"}</div><div style="font-size:.74rem;color:{spc}">{"+" if s["pnl"]>=0 else ""}${abs(s["pnl"]):.2f} · {rl}</div><div class="muted" style="font-size:.7rem">{s["total"]} signalen</div></div>'

    body = f"""
<div class="banner">📊 <strong>Paper Trading Experiment</strong> — Paasweekend 2026<br>
Bookmaker fair odds vs Polymarket prijs. Edge ≥ 3pp | PM prijs 0.20–0.50 | Min 4 bookmakers | $5 flat.<br>
Voetbal (EPL/BL/UCL/LaLiga/SerieA/Ligue1) + NBA (als beschikbaar). Scanner elke 4u. Dry-run.</div>
<div class="krow">
{ktile("Signalen", str(ps["total"]), "var(--blue)", "paper_signal_scanner.py")}
{ktile("Open", str(ps["open"]), "var(--blue)", "wacht op resolutie")}
{ktile("Resolved", str(ps["resolved"]), "#e6edf3", f"{ps['wins']}W / {ps['losses']}L")}
{ktile("Win Rate", f"{ps['wr']:.1f}%" if ps["resolved"] else "—", wc, "na resolutie")}
{ktile("ROI", fpct(ps["roi"]) if ps["resolved"] else "—", rc, "$5 flat per signaal")}
{ktile("Gem. Edge", f"{ps['avg_edge']:.1f}pp", "var(--yellow)", "bookmaker − PM prijs")}
{ktile("Edge Accuracy", f"{ps['edge_accuracy']:.0f}%" if ps["edge_total"] else "—", ec, f"n={ps['edge_total']}")}
</div>
{"" if not ps["sport_stats"] else '<div class="section"><div class="stitle">Per Sport</div><div class="krow" style="flex-wrap:wrap">' + sport_html + '</div></div>'}
<div class="section"><div class="stitle">Open Signalen ({ps["open"]})</div>{render_paper_trades(p, open_only=True)}</div>
<div class="section"><div class="stitle">Resolved ({ps["resolved"]})</div>{render_paper_trades(p, resolved_only=True)}</div>
<div class="section"><div class="stitle">Scanner Runs (laatste 10)</div>{render_scanner_log()}</div>"""
    return page_wrap("/paper", body, token)


def page_ops(trades, token=""):
    ck = compute_cannae_kpis()
    cash = ck.get("cash", 0)
    prefix = f"/t/{token}" if token else ""
    opts = "".join(f'<option value="{addr}">{name} ({addr[:6]}...{addr[-4:]})</option>'
                   for name, addr in WITHDRAW_WALLETS.items())
    body = f"""
<div class="section"><div class="stitle">Bot Status</div>
<div style="font-size:.85rem;display:flex;flex-direction:column;gap:5px">{render_bot_status()}</div></div>
<div class="section"><div class="stitle">Paper Trading Config</div>
<div style="font-size:.82rem;color:var(--muted);line-height:2">
Scanner: <code>/opt/bottie-test/scripts/paper_signal_scanner.py</code><br>
Cron: elke 4u (00:00 04:00 08:00 12:00 16:00 20:00 UTC)<br>
Odds API: <code>0cb690f97add451f3282da4e481f0730</code><br>
Min edge: 3pp | PM prijs: 0.20–0.50 | Min bookmakers: 4 | Flat: $5<br>
Log: <code>/opt/bottie-test/data/scanner.log</code><br>
Trades: <code>/opt/bottie-test/data/trades.jsonl</code>
</div></div>
<div class="section"><div class="stitle">USDC Opnemen</div>
<div style="margin-bottom:8px;font-size:.82rem">Cash: <strong>${cash:.2f}</strong></div>
<div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
<div><div style="font-size:.7rem;color:var(--muted);margin-bottom:3px">Bedrag</div>
<input id="wa" type="number" min="1" max="{cash:.0f}" step="0.01" placeholder="0.00"
 style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:5px 9px;color:#e6edf3;width:120px"></div>
<div><div style="font-size:.7rem;color:var(--muted);margin-bottom:3px">Naar</div>
<select id="ww" style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:5px 9px;color:#e6edf3">{opts}</select></div>
<button onclick="doT()" style="background:var(--blue);color:#fff;border:none;border-radius:4px;padding:6px 14px;font-size:.82rem;font-weight:600;cursor:pointer">Versturen</button>
<button onclick="document.getElementById('wa').value='{cash:.2f}'" style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:6px 10px;color:var(--muted);font-size:.78rem;cursor:pointer">Max</button>
</div>
<div id="tr" style="margin-top:8px;font-size:.8rem"></div></div>
<script>
async function doT(){{
  const a=document.getElementById('wa').value,w=document.getElementById('ww').value,n=document.getElementById('ww').selectedOptions[0].text;
  if(!a||a<=0){{alert('Voer bedrag in');return;}}
  if(!confirm('Stuur $'+a+' USDC naar '+n+'?'))return;
  document.getElementById('tr').innerHTML='<span style="color:var(--yellow)">Bezig...</span>';
  try{{const r=await fetch('{prefix}/transfer',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{amount:parseFloat(a),to:w}})}});
  const d=await r.json();
  document.getElementById('tr').innerHTML=d.ok?'<span style="color:var(--green)">✓ Verstuurd! TX: '+(d.tx_hash||'').slice(0,14)+'</span>':'<span style="color:var(--red)">✗ '+d.error+'</span>';
  }}catch(e){{document.getElementById('tr').innerHTML='<span style="color:var(--red)">✗ '+e+'</span>';}}
}}
</script>"""
    return page_wrap("/ops", body, token)


# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        token, path = self._auth(self.path)
        if token is None: return
        if path == "/favicon.ico": self._ok(b"", "image/x-icon"); return
        try:
            trades = load_trades()
            if path in ("/", "/index.html"): html = page_overview(trades, token)
            elif path == "/cannae":           html = page_cannae(trades, token)
            elif path == "/paper":            html = page_paper(token)
            elif path == "/ops":              html = page_ops(trades, token)
            elif path == "/api/trades":
                body = json.dumps({"cannae": trades[-50:], "paper": load_paper_trades()}, default=str).encode()
                self._ok(body, "application/json"); return
            else: self._send(404, b"Not found", "text/plain"); return
            self._ok(html.encode(), "text/html")
        except Exception as e:
            import traceback
            self._ok(f"<pre>{e}\n{traceback.format_exc()}</pre>".encode(), "text/html")

    def do_POST(self):
        token, path = self._auth(self.path)
        if token is None: return
        if path != "/transfer": self._send(404, b"", "text/plain"); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n)) if n else {}
            amt = float(data.get("amount", 0))
            to  = data.get("to", "")
            if to not in set(WITHDRAW_WALLETS.values()): raise ValueError(f"Onbekend adres")
            if amt <= 0: raise ValueError("Bedrag > 0")
            script = str(Path(__file__).parent / "scripts" / "transfer_usdc.py")
            env = os.environ.copy()
            ef = Path(__file__).parent / ".env"
            if ef.exists():
                for line in ef.read_text().splitlines():
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1); env[k.strip()] = v.strip()
            r = subprocess.run(["python3", script, to, str(round(amt,2))],
                capture_output=True, text=True, timeout=60,
                cwd=str(Path(__file__).parent), env=env)
            resp = json.loads(r.stdout) if r.stdout.strip() else {"ok": False, "error": r.stderr or "no output"}
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        self._ok(json.dumps(resp).encode(), "application/json")

    def _auth(self, raw_path):
        m = re.match(r"^/t/([^/]+)(/.*)$", raw_path)
        if m:
            t, p = m.group(1), m.group(2) or "/"
            if t != AUTH_TOKEN: self._send(403, b"Forbidden", "text/plain"); return None, None
            return t, p
        if raw_path not in ("/favicon.ico",):
            self._send(302, b"", "text/plain",
                       extra=[("Location", f"/t/{AUTH_TOKEN}{raw_path}")])
        return None, None

    def _ok(self, body, ct): self._send(200, body, ct)

    def _send(self, code, body, ct, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        for k, v in (extra or []): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)

    def log_message(self, *a): pass


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    print(f"Bottie Dashboard → http://0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
