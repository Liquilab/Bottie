#!/usr/bin/env python3
"""Patch dashboard.py to add live ESPN scores to game cards."""

import sys

with open("/opt/bottie/dashboard.py", "r") as f:
    content = f.read()

# 1. Add live scores function before build_games_data
score_func = r'''
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

'''

# Insert before build_games_data
old = 'def build_games_data(trades, funder=None):'
if old not in content:
    print("ERROR: could not find build_games_data")
    sys.exit(1)
content = content.replace(old, score_func + '\ndef build_games_data(trades, funder=None):')
print("1. Added score functions")

# 2. Add score CSS
score_css = """
.score-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.85rem; font-weight:600; margin-left:8px; font-family:monospace; }
.score-badge.live { background:rgba(255,50,50,0.15); color:#ff4444; animation: pulse 2s infinite; }
.score-badge.pre { background:rgba(100,100,100,0.15); color:#888; }
.score-badge.final { background:rgba(100,100,100,0.15); color:#aaa; }
.score-detail { font-size:0.7rem; font-weight:400; opacity:0.7; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.7} }
"""

css_anchor = '.game-card-header{display:flex'
if css_anchor not in content:
    # Try with spaces
    css_anchor = '.game-card-header {display:flex'
    if css_anchor not in content:
        css_anchor = '.game-card-header{'
        if css_anchor not in content:
            print("WARN: could not find css anchor, trying alternative")
            # Just prepend to first </style>
            content = content.replace('</style>', score_css + '\n</style>', 1)
            print("2. Added CSS (via </style>)")
        else:
            content = content.replace(css_anchor, score_css + '\n' + css_anchor, 1)
            print("2. Added CSS")
    else:
        content = content.replace(css_anchor, score_css + '\n' + css_anchor, 1)
        print("2. Added CSS")
else:
    content = content.replace(css_anchor, score_css + '\n' + css_anchor, 1)
    print("2. Added CSS")

# 3. Attach scores to game data in build_games_data
old_return = '    return list(games.values())\n\n\ndef render_game_cards'
if old_return in content:
    new_return = '''    # Attach live scores
    scores = fetch_live_scores()
    for g in games.values():
        g["score"] = match_score(g["slug"], scores)

    return list(games.values())


def render_game_cards'''
    content = content.replace(old_return, new_return)
    print("3. Added score attachment to build_games_data")
else:
    print("WARN: could not find return in build_games_data")

# 4. Add score badge to game card header
old_header = '''<span class="game-meta" style="margin-left:8px">{g["date"]}</span>'''
if old_header in content:
    new_header = old_header + '\n              {render_score_badge(g.get("score"))}'
    content = content.replace(old_header, new_header)
    print("4. Added score badge to game card header")
else:
    print("WARN: could not find game header template")

# 5. Auto-refresh games page every 15s
old_refresh = '{"" if active_page in ("/settings", "/games") else \'<meta http-equiv="refresh" content="30">\'}'
if old_refresh in content:
    new_refresh = old_refresh + '\n  {"" if active_page != "/games" else \'<meta http-equiv="refresh" content="15">\'}'
    content = content.replace(old_refresh, new_refresh)
    print("5. Added 15s auto-refresh for /games")
else:
    print("WARN: could not find refresh meta")

with open("/opt/bottie/dashboard.py", "w") as f:
    f.write(content)

print("\nDone! Restart dashboard to apply.")
