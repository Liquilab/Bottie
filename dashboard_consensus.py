#!/usr/bin/env python3
"""Whale Consensus Dashboard — clean build for consensus-only strategy."""

import json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from collections import defaultdict
try:
    from zoneinfo import ZoneInfo
    CET = ZoneInfo("Europe/Amsterdam")
except ImportError:
    CET = None

DATA_DIR = Path.cwd() / "data"
CONSENSUS_TRADES = DATA_DIR / "consensus_trades.jsonl"
LEGACY_TRADES    = DATA_DIR / "trades.jsonl"
BOARD_FILE       = DATA_DIR / "consensus_board.json"

AUTH_TOKEN = os.environ.get("DASHBOARD_TOKEN", "8vNADas4jmnOk3IbpeBFrgDHkKHN9Epq")
PM_DATA_API = "https://data-api.polymarket.com"
PM_FUNDER = "0x89dcA91b49AfB7bEfb953a7658a1B83fC7Ab8F42"
INITIAL_BANKROLL = 1400.0

# ── PM API ──────────────────────────────────────────────────────────────
_pm_cache = {"data": None, "ts": 0}

def pm_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Dashboard/1", "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def fetch_pm():
    now = time.time()
    if _pm_cache["data"] and now - _pm_cache["ts"] < 20:
        return _pm_cache["data"]
    r = {"positions": [], "value": 0, "cash": 0}
    try:
        r["positions"] = pm_get("%s/positions?user=%s&limit=500&sizeThreshold=0" % (PM_DATA_API, PM_FUNDER))
    except: pass
    try:
        val = pm_get("%s/value?user=%s" % (PM_DATA_API, PM_FUNDER))
        if val: r["value"] = float(val[0].get("value", 0))
    except: pass
    # Cash: on-chain USDC.e balance
    try:
        addr = PM_FUNDER.lower().replace("0x", "")
        data = "0x70a08231" + addr.rjust(64, "0")
        payload = json.dumps({"jsonrpc":"2.0","method":"eth_call","params":[{"to":"0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174","data":data},"latest"],"id":1})
        rpc_req = urllib.request.Request("https://1rpc.io/matic", data=payload.encode(),
            headers={"Content-Type":"application/json","User-Agent":"B/1"}, method="POST")
        rpc_resp = json.loads(urllib.request.urlopen(rpc_req, timeout=10).read())
        hex_val = rpc_resp.get("result","0x0").replace("0x","")
        r["cash"] = int(hex_val, 16) / 1e6
    except:
        r["cash"] = 0
    r["total"] = r["cash"] + r["value"]
    _pm_cache["data"] = r
    _pm_cache["ts"] = now
    return r

# ── Data Loading ────────────────────────────────────────────────────────
def load_consensus_trades():
    trades = []
    if CONSENSUS_TRADES.exists():
        for line in CONSENSUS_TRADES.read_text().splitlines():
            if line.strip():
                try: trades.append(json.loads(line))
                except: pass
    return trades

def load_legacy_trades():
    trades = []
    if LEGACY_TRADES.exists():
        for line in LEGACY_TRADES.read_text().splitlines():
            if line.strip():
                try:
                    t = json.loads(line)
                    if t.get("result") in ("win", "loss"):
                        trades.append(t)
                except: pass
    return trades

def load_board():
    """Load consensus board + supplement with schedule cache for 24h view."""
    board = []
    if BOARD_FILE.exists():
        try: board = json.loads(BOARD_FILE.read_text())
        except: pass

    # Supplement: add games from Rust bot schedule cache that are within 24h
    board_slugs = set(g.get("slug","") for g in board)
    schedule_file = DATA_DIR / "schedule_cache.json"
    if schedule_file.exists():
        try:
            sched = json.loads(schedule_file.read_text())
            now = datetime.now(timezone.utc)
            us_sports = {"nba","nhl","mlb","nfl","cbb","ncaa","mls"}
            for g in sched:
                slug = g.get("event_slug","")
                start = g.get("start_time","")
                sport = slug.split("-")[0]
                if sport in us_sports: continue
                # Skip duplicates, -more-markets, etc
                base = slug.split("-more-markets")[0].split("-halftime")[0].split("-exact-score")[0].split("-player-props")[0].split("-total-corners")[0]
                if base in board_slugs: continue
                try:
                    kt = datetime.fromisoformat(start.replace("Z","+00:00"))
                    until = (kt - now).total_seconds() / 3600
                    if until < 0 or until > 24: continue
                except: continue
                board_slugs.add(base)
                board.append({
                    "slug": base,
                    "sport": sport,
                    "title": g.get("title", base),
                    "kickoff": start,
                    "status": "PENDING",
                    "consensus_pct": 0,
                    "n_traders": 0,
                    "buy_question": "",
                    "buy_side": "",
                    "bought": False,
                })
        except: pass

    board.sort(key=lambda g: g.get("kickoff",""))
    return board

def sf(v):
    try: return float(v)
    except: return 0.0

# ── Formatters ──────────────────────────────────────────────────────────
def fmt_pnl(v):
    if v is None: return '<span class="m">-</span>'
    s = "+" if v >= 0 else ""
    c = "g" if v >= 0 else "r"
    return '<span class="%s">%s$%.2f</span>' % (c, s, v)

def fmt_pct(v):
    if v is None: return '-'
    c = "g" if v >= 55 else "r" if v < 45 else "y"
    return '<span class="%s">%.1f%%</span>' % (c, v)

# ── CSS ─────────────────────────────────────────────────────────────────
CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0d1117; color:#c9d1d9; font-family:-apple-system,system-ui,sans-serif; font-size:14px; }
.wrap { max-width:1100px; margin:0 auto; padding:16px; }
nav { background:#161b22; border-bottom:1px solid #30363d; padding:10px 20px; display:flex; gap:16px; align-items:center; }
nav a { color:#58a6ff; text-decoration:none; font-size:13px; }
nav a:hover { text-decoration:underline; }
nav .brand { color:#f0f6fc; font-weight:700; font-size:15px; margin-right:auto; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:16px 0; }
.card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; }
.card .label { color:#8b949e; font-size:11px; text-transform:uppercase; margin-bottom:4px; }
.card .value { font-size:22px; font-weight:700; }
table { width:100%; border-collapse:collapse; margin:12px 0; }
th { text-align:left; color:#8b949e; font-size:11px; text-transform:uppercase; padding:8px; border-bottom:1px solid #30363d; }
td { padding:8px; border-bottom:1px solid #21262d; font-size:13px; }
tr:hover { background:#161b22; }
h2 { color:#f0f6fc; font-size:16px; margin:20px 0 8px; }
.g { color:#3fb950; } .r { color:#f85149; } .y { color:#d29922; } .m { color:#484f58; }
.pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
.pill-buy { background:#1a3a2a; color:#3fb950; }
.pill-skip { background:#3d2a1a; color:#d29922; }
.pill-pending { background:#1a2233; color:#58a6ff; }
.pill-done { background:#1a3a2a; color:#3fb950; border:1px solid #3fb950; }
.btn { background:#21262d; color:#c9d1d9; border:1px solid #30363d; padding:4px 12px; border-radius:6px; cursor:pointer; font-size:12px; }
.btn:hover { background:#30363d; }
.section { margin:20px 0; }
"""

# ── Page Wrapper ────────────────────────────────────────────────────────
def page_wrap(title, body, token=""):
    pfx = "/t/%s" % token if token else ""
    return """<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s — Whale Consensus</title>
<style>%s</style>
</head><body>
<nav>
  <span class="brand">Whale Consensus</span>
  <a href="%s/">Overview</a>
  <a href="%s/board">Flight Board</a>
  <a href="%s/trades">Trades</a>
  <a href="%s/games">Positions</a>
  <a href="%s/pnl">PnL</a>
</nav>
<div class="wrap">%s</div>
</body></html>""" % (title, CSS, pfx, pfx, pfx, pfx, pfx, body)

# ── Overview ────────────────────────────────────────────────────────────
def render_overview(token=""):
    pm = fetch_pm()
    trades = load_consensus_trades()
    legacy = load_legacy_trades()

    # Today's consensus trades
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in trades if (t.get("timestamp","") or "")[:10] == today]

    # All resolved (from legacy + consensus with result)
    all_resolved = [t for t in legacy if t.get("result") in ("win","loss")]
    cons_resolved = [t for t in trades if t.get("result") in ("win","loss")]

    # PnL — use LB API as source of truth for legacy
    legacy_pnl = None
    try:
        lb_req = urllib.request.Request(
            "https://lb-api.polymarket.com/profit?address=%s" % PM_FUNDER,
            headers={"User-Agent": "Dashboard/1", "Accept": "application/json"})
        lb_data = json.loads(urllib.request.urlopen(lb_req, timeout=10).read())
        if lb_data:
            legacy_pnl = float(lb_data[0].get("amount", 0))
    except: pass
    if legacy_pnl is None:
        legacy_pnl = sum(sf(t.get("pnl",0)) for t in all_resolved)
    cons_pnl = sum(sf(t.get("pnl",0)) for t in cons_resolved)

    # WR
    cons_w = sum(1 for t in cons_resolved if t["result"] == "win")
    cons_wr = 100 * cons_w / len(cons_resolved) if cons_resolved else 0

    # Avg consensus %
    avg_cons = sum(sf(t.get("consensus_pct",0)) for t in trades) / len(trades) if trades else 0

    # Open positions
    active = [p for p in pm["positions"] if sf(p.get("size",0)) > 0.01]
    open_val = sum(sf(p.get("currentValue",0)) for p in active)
    open_cost = sum(sf(p.get("initialValue",0)) for p in active)

    cards = """
    <div class="cards">
      <div class="card"><div class="label">Portfolio</div><div class="value">$%.0f</div></div>
      <div class="card"><div class="label">Cash</div><div class="value">$%.0f</div></div>
      <div class="card"><div class="label">Positions</div><div class="value">$%.0f</div></div>
      <div class="card"><div class="label">Unrealized</div><div class="value">%s</div></div>
      <div class="card"><div class="label">Legacy PnL</div><div class="value">%s</div></div>
      <div class="card"><div class="label">Consensus PnL</div><div class="value">%s</div></div>
      <div class="card"><div class="label">Consensus WR</div><div class="value">%s</div></div>
      <div class="card"><div class="label">Avg Consensus</div><div class="value">%.0f%%</div></div>
    </div>
    """ % (pm["total"], pm["cash"], open_val,
           fmt_pnl(open_val - open_cost), fmt_pnl(legacy_pnl), fmt_pnl(cons_pnl),
           fmt_pct(cons_wr), avg_cons)

    # Today's trades
    rows = ""
    for t in sorted(today_trades, key=lambda x: x.get("timestamp",""), reverse=True):
        ts = (t.get("timestamp","") or "")[11:16]
        slug = t.get("event_slug","")
        sport = slug.split("-")[0] if slug else ""
        title = t.get("market_title","")[:40]
        price = sf(t.get("price",0))
        size = sf(t.get("size_usdc",0))
        cpct = sf(t.get("consensus_pct",0))
        rows += "<tr><td>%s</td><td>%s</td><td>%s No</td><td>%.0f¢</td><td>$%.0f</td><td>%.0f%%</td></tr>\n" % (
            ts, sport, title, price*100, size, cpct)

    today_html = ""
    if rows:
        today_html = """<h2>Trades vandaag (%d)</h2>
        <table><tr><th>Tijd</th><th>Sport</th><th>Market</th><th>Prijs</th><th>Size</th><th>Consensus</th></tr>
        %s</table>""" % (len(today_trades), rows)
    else:
        today_html = "<h2>Trades vandaag</h2><p class='m'>Geen trades vandaag</p>"

    # Flight board inline
    board = load_board()
    now_utc = datetime.now(timezone.utc)
    board_rows = ""
    for g in board:
        kickoff = g.get("kickoff","")
        try:
            kt = datetime.fromisoformat(kickoff.replace("Z","+00:00"))
            until = (kt - now_utc).total_seconds() / 60
            time_str = kt.astimezone(CET).strftime("%H:%M") if CET else kickoff[11:16]
        except:
            until = 999
            time_str = "?"
        if until < 0: continue
        slug = g.get("slug","")
        sport = slug.split("-")[0]
        title = g.get("title","")[:40]
        status = g.get("status","PENDING")
        cpct = g.get("consensus_pct",0)
        bought = g.get("bought", False)
        if bought:
            pill = '<span class="pill pill-done">BOUGHT %.0f%%</span>' % cpct
        elif status == "BUY":
            pill = '<span class="pill pill-buy">BUY %.0f%%</span>' % cpct
        elif status == "SKIP":
            pill = '<span class="pill pill-skip">SKIP %.0f%%</span>' % cpct
        else:
            pill = '<span class="pill pill-pending">PENDING</span>'
        until_str = "%.0fm" % until if until > 0 else '<span class="m">LIVE</span>'
        board_rows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n" % (
            time_str, until_str, sport, title, pill)

    if board_rows:
        board_html = """<h2>Flight Board</h2>
        <table><tr><th>Kickoff</th><th>T-</th><th>Sport</th><th>Game</th><th>Status</th></tr>
        %s</table>""" % board_rows
    else:
        board_html = "<h2>Flight Board</h2><p class='m'>Geen upcoming games</p>"

    # Open positions inline — match consensus % from trades
    cons_by_title = {}
    for t in trades:
        mt = (t.get("market_title","") or "").lower()
        cons_by_title[mt] = sf(t.get("consensus_pct", 0))

    pos_rows = ""
    for p in sorted(active, key=lambda x: sf(x.get("currentValue",0)), reverse=True):
        ptitle = (p.get("title","") or "")[:35]
        outcome = p.get("outcome","")
        size = sf(p.get("size",0))
        avg_p = sf(p.get("avgPrice",0))
        cur_p = sf(p.get("curPrice",0))
        cost = sf(p.get("initialValue",0))
        value = sf(p.get("currentValue",0))
        upnl = value - cost
        # Match consensus %
        full_title = (p.get("title","") or "").lower()
        cpct = cons_by_title.get(full_title, 0)
        cpct_html = "%.0f%%" % cpct if cpct > 0 else '<span class="m">-</span>'
        pos_rows += "<tr><td>%s</td><td>%s</td><td>%.0f</td><td>%.0f¢→%.0f¢</td><td>$%.0f</td><td>%s</td><td>%s</td></tr>\n" % (
            ptitle, outcome, size, avg_p*100, cur_p*100, cost, fmt_pnl(upnl), cpct_html)

    if pos_rows:
        pos_html = """<h2>Open Positions (%d)</h2>
        <table><tr><th>Market</th><th>Side</th><th>Shares</th><th>Price</th><th>Cost</th><th>P&L</th><th>Cons%%</th></tr>
        %s</table>""" % (len(active), pos_rows)
    else:
        pos_html = "<h2>Open Positions</h2><p class='m'>Geen open posities</p>"

    return page_wrap("Overview", cards + today_html + board_html + pos_html, token)

# ── Flight Board ────────────────────────────────────────────────────────
def render_board(token=""):
    board = load_board()
    now = datetime.now(timezone.utc)

    rows = ""
    for g in board:
        kickoff = g.get("kickoff","")
        try:
            kt = datetime.fromisoformat(kickoff.replace("Z","+00:00"))
            until = (kt - now).total_seconds() / 60
            time_str = kt.astimezone(CET).strftime("%H:%M") if CET else kickoff[11:16]
        except:
            until = 999
            time_str = "?"

        if until < -120:
            continue

        slug = g.get("slug","")
        sport = slug.split("-")[0]
        title = g.get("title","")[:45]
        status = g.get("status","PENDING")
        cpct = g.get("consensus_pct",0)
        bought = g.get("bought", False)

        if bought:
            pill = '<span class="pill pill-done">BOUGHT %.0f%%</span>' % cpct
        elif status == "BUY":
            pill = '<span class="pill pill-buy">BUY %.0f%%</span>' % cpct
        elif status == "SKIP":
            pill = '<span class="pill pill-skip">SKIP %.0f%%</span>' % cpct
        else:
            pill = '<span class="pill pill-pending">PENDING</span>'

        if until > 0:
            until_str = "%.0fm" % until
        else:
            until_str = '<span class="m">LIVE</span>'

        question = g.get("buy_question","")[:35]
        side = g.get("buy_side","")

        rows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s %s</td></tr>\n" % (
            time_str, until_str, sport, title, pill, question, side)

    body = """<h2>Flight Board — vandaag</h2>
    <table><tr><th>Kickoff</th><th>T-</th><th>Sport</th><th>Game</th><th>Status</th><th>Consensus Leg</th></tr>
    %s</table>""" % (rows if rows else "<tr><td colspan='6' class='m'>Geen games geladen</td></tr>")

    return page_wrap("Flight Board", body, token)

# ── Trades ──────────────────────────────────────────────────────────────
def render_trades(token=""):
    trades = load_consensus_trades()

    rows = ""
    for t in sorted(trades, key=lambda x: x.get("timestamp",""), reverse=True)[:100]:
        ts = (t.get("timestamp","") or "")[:16].replace("T"," ")
        slug = t.get("event_slug","")
        sport = slug.split("-")[0]
        title = t.get("market_title","")[:35]
        price = sf(t.get("price",0))
        size = sf(t.get("size_usdc",0))
        cpct = sf(t.get("consensus_pct",0))
        result = t.get("result","")
        pnl = t.get("pnl")

        if result == "win":
            res_html = '<span class="g">WIN</span>'
        elif result == "loss":
            res_html = '<span class="r">LOSS</span>'
        else:
            res_html = '<span class="y">OPEN</span>'

        pnl_html = fmt_pnl(sf(pnl)) if pnl is not None else '<span class="m">-</span>'

        rows += "<tr><td>%s</td><td>%s</td><td>%s No</td><td>%.0f¢</td><td>$%.0f</td><td>%.0f%%</td><td>%s</td><td>%s</td></tr>\n" % (
            ts, sport, title, price*100, size, cpct, res_html, pnl_html)

    body = """<h2>Consensus Trades (%d)</h2>
    <table><tr><th>Tijd</th><th>Sport</th><th>Market</th><th>Prijs</th><th>Size</th><th>Cons%%</th><th>Result</th><th>PnL</th></tr>
    %s</table>""" % (len(trades), rows)

    return page_wrap("Trades", body, token)

# ── Games (open positions) ──────────────────────────────────────────────
def render_games(token=""):
    pm = fetch_pm()
    active = [p for p in pm["positions"] if sf(p.get("size",0)) > 0.01]
    trades = load_consensus_trades()

    cons_by_title = {}
    for t in trades:
        mt = (t.get("market_title","") or "").lower()
        cons_by_title[mt] = sf(t.get("consensus_pct", 0))

    rows = ""
    for p in sorted(active, key=lambda x: sf(x.get("currentValue",0)), reverse=True):
        title = (p.get("title","") or "")[:40]
        outcome = p.get("outcome","")
        size = sf(p.get("size",0))
        avg_price = sf(p.get("avgPrice",0))
        cur_price = sf(p.get("curPrice",0))
        cost = sf(p.get("initialValue",0))
        value = sf(p.get("currentValue",0))
        upnl = value - cost
        full_title = (p.get("title","") or "").lower()
        cpct = cons_by_title.get(full_title, 0)
        cpct_html = "%.0f%%" % cpct if cpct > 0 else '<span class="m">-</span>'

        rows += "<tr><td>%s</td><td>%s</td><td>%.0f</td><td>%.0f¢</td><td>%.0f¢</td><td>$%.0f</td><td>$%.0f</td><td>%s</td><td>%s</td></tr>\n" % (
            title, outcome, size, avg_price*100, cur_price*100, cost, value, fmt_pnl(upnl), cpct_html)

    body = """<h2>Open Positions (%d)</h2>
    <table><tr><th>Market</th><th>Side</th><th>Shares</th><th>Avg</th><th>Cur</th><th>Cost</th><th>Value</th><th>P&L</th><th>Cons%%</th></tr>
    %s</table>""" % (len(active), rows)

    return page_wrap("Positions", body, token)

# ── PnL Breakdown ──────────────────────────────────────────────────────
def render_pnl(token=""):
    trades = load_consensus_trades()
    legacy = load_legacy_trades()

    # Per-league breakdown (consensus trades)
    by_league = defaultdict(lambda: {"w":0,"l":0,"pnl":0,"inv":0,"cons":[]})
    for t in trades:
        if t.get("result") not in ("win","loss"): continue
        slug = t.get("event_slug","")
        sport = slug.split("-")[0]
        d = by_league[sport]
        d["w" if t["result"]=="win" else "l"] += 1
        d["pnl"] += sf(t.get("pnl",0))
        d["inv"] += sf(t.get("size_usdc",0))
        d["cons"].append(sf(t.get("consensus_pct",0)))

    league_rows = ""
    for sport, d in sorted(by_league.items(), key=lambda x: x[1]["pnl"], reverse=True):
        n = d["w"]+d["l"]
        wr = 100*d["w"]/n if n else 0
        roi = 100*d["pnl"]/d["inv"] if d["inv"] else 0
        avg_c = sum(d["cons"])/len(d["cons"]) if d["cons"] else 0
        league_rows += "<tr><td>%s</td><td>%d</td><td>%d/%d</td><td>%s</td><td>%s</td><td>%.0f%%</td><td>%.0f%%</td></tr>\n" % (
            sport, n, d["w"], d["l"], fmt_pct(wr), fmt_pnl(d["pnl"]), roi, avg_c)

    league_html = """<h2>Per League (Consensus)</h2>
    <table><tr><th>League</th><th>N</th><th>W/L</th><th>WR</th><th>PnL</th><th>ROI</th><th>Avg Cons</th></tr>
    %s</table>""" % (league_rows if league_rows else "<tr><td colspan='7' class='m'>Geen resolved trades</td></tr>")

    # Per-day breakdown
    by_day = defaultdict(lambda: {"w":0,"l":0,"pnl":0})
    for t in trades:
        if t.get("result") not in ("win","loss"): continue
        day = (t.get("timestamp","") or "")[:10]
        d = by_day[day]
        d["w" if t["result"]=="win" else "l"] += 1
        d["pnl"] += sf(t.get("pnl",0))

    day_rows = ""
    cum = 0
    for day in sorted(by_day.keys()):
        d = by_day[day]
        n = d["w"]+d["l"]
        cum += d["pnl"]
        wr = 100*d["w"]/n if n else 0
        day_rows += "<tr><td>%s</td><td>%d</td><td>%d/%d</td><td>%s</td><td>%s</td><td>%s</td></tr>\n" % (
            day, n, d["w"], d["l"], fmt_pct(wr), fmt_pnl(d["pnl"]), fmt_pnl(cum))

    day_html = """<h2>Per Dag</h2>
    <table><tr><th>Dag</th><th>N</th><th>W/L</th><th>WR</th><th>PnL</th><th>Cum PnL</th></tr>
    %s</table>""" % (day_rows if day_rows else "<tr><td colspan='6' class='m'>Geen resolved trades</td></tr>")

    # Legacy summary
    legacy_pnl = sum(sf(t.get("pnl",0)) for t in legacy)
    legacy_w = sum(1 for t in legacy if t.get("result") == "win")
    legacy_l = sum(1 for t in legacy if t.get("result") == "loss")
    legacy_html = """<h2>Legacy (Cannae) — gesloten</h2>
    <p>%d trades | %dW/%dL | PnL %s</p>""" % (len(legacy), legacy_w, legacy_l, fmt_pnl(legacy_pnl))

    return page_wrap("PnL", league_html + day_html + legacy_html, token)

# ── HTTP Server ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _check_auth(self):
        path = self.path
        if path.startswith("/t/"):
            parts = path.split("/", 3)
            if len(parts) >= 3:
                token = parts[2]
                if token == AUTH_TOKEN:
                    page = "/" + parts[3] if len(parts) > 3 else "/"
                    return page, token
        return None

    def _send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self._check_auth()
        if not auth:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        page, token = auth
        page = urlparse(page).path

        try:
            if page in ("/", "/index.html"):
                self._send_html(render_overview(token))
            elif page == "/board":
                self._send_html(render_board(token))
            elif page == "/trades":
                self._send_html(render_trades(token))
            elif page == "/games":
                self._send_html(render_games(token))
            elif page == "/pnl":
                self._send_html(render_pnl(token))
            else:
                self._send_html(render_overview(token))
        except Exception as e:
            import traceback
            self._send_html("<pre>Error: %s\n%s</pre>" % (e, traceback.format_exc()), 500)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print("Dashboard running on http://0.0.0.0:%d" % port)
    server.serve_forever()
