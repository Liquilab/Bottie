#!/usr/bin/env python3
"""Patch dashboard.py to add /health page showing drift + healthcheck data."""

with open("/opt/bottie/dashboard.py", "r") as f:
    content = f.read()

# 1. Add "Health" to nav
content = content.replace(
    '("Settings", "/settings"),',
    '("Health", "/health"),\n        ("Settings", "/settings"),'
)

# 2. Add render_health_page function before render_settings_page
health_page = '''
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

'''

content = content.replace(
    'def render_settings_page(',
    health_page + '\ndef render_settings_page('
)

# 3. Add route handler for /health
# Find the route handler section
content = content.replace(
    'elif path == "/settings":',
    'elif path == "/health":\n                return render_health_page(token=token, account=account)\n            elif path == "/settings":'
)

# 4. Auto-refresh health page every 60s
content = content.replace(
    '{"" if active_page in ("/settings", "/games",',
    '{"" if active_page in ("/settings", "/games", "/health",'
)

with open("/opt/bottie/dashboard.py", "w") as f:
    f.write(content)

print("Dashboard patched with /health page")
