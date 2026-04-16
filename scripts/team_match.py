#!/usr/bin/env python3
"""Shared team/event matching — SSOT for all scanners.

Matches bookmaker events to Polymarket markets using:
1. Date: commence_time within ±3 hours of PM start_time
2. Both teams: via alias table + normalization
3. Filters: no futures/outrights, no -more-markets, only win markets
"""
from datetime import datetime, timezone, timedelta

# ── Team Aliases ──────────────────────────────────────────────────────────
# Canonical name (Odds API) → list of PM variants
TEAM_ALIASES = {
    # EPL
    "manchester united": ["man utd", "man united", "manchester utd", "manchester united fc"],
    "manchester city": ["man city", "manchester city fc"],
    "tottenham hotspur": ["tottenham", "spurs", "tottenham hotspur fc"],
    "wolverhampton wanderers": ["wolves", "wolverhampton", "wolverhampton wanderers fc"],
    "newcastle united": ["newcastle", "newcastle united fc"],
    "nottingham forest": ["nottm forest", "nottingham", "nottingham forest fc"],
    "west ham united": ["west ham", "west ham united fc"],
    "brighton and hove albion": ["brighton", "brighton and hove albion fc"],
    "leicester city": ["leicester", "leicester city fc"],
    "crystal palace": ["crystal palace fc"],
    "afc bournemouth": ["bournemouth", "afc bournemouth fc"],
    "ipswich town": ["ipswich", "ipswich town fc"],
    "arsenal": ["arsenal fc"],
    "liverpool": ["liverpool fc"],
    "chelsea": ["chelsea fc"],
    "everton": ["everton fc"],
    "fulham": ["fulham fc"],
    "aston villa": ["aston villa fc"],
    "brentford": ["brentford fc"],
    "southampton": ["southampton fc"],
    "sunderland": ["sunderland afc"],
    "leeds united": ["leeds", "leeds united fc"],
    # Championship
    "sheffield united": ["sheffield utd", "sheffield united fc"],
    "norwich city": ["norwich", "norwich city fc"],
    "burnley": ["burnley fc"],
    "middlesbrough": ["middlesbrough fc"],
    "coventry city": ["coventry", "coventry city fc"],
    "west bromwich albion": ["west brom", "west bromwich"],
    "derby county": ["derby", "derby county fc"],
    "portsmouth": ["portsmouth fc"],
    "wrexham": ["wrexham afc"],
    "birmingham city": ["birmingham", "birmingham city fc"],
    # Bundesliga
    "borussia dortmund": ["dortmund", "bvb", "bv borussia 09 dortmund"],
    "bayern munich": ["bayern", "fc bayern", "bayern munchen", "fc bayern munchen", "fc bayern münchen"],
    "bayer leverkusen": ["leverkusen", "bayer 04", "bayer 04 leverkusen"],
    "rb leipzig": ["leipzig", "rasenballsport leipzig"],
    "eintracht frankfurt": ["frankfurt", "e. frankfurt", "eintracht frankfurt fc"],
    "borussia monchengladbach": ["gladbach", "monchengladbach", "borussia mönchengladbach", "borussia mgladbach"],
    "vfb stuttgart": ["stuttgart", "vfb stuttgart fc"],
    "sc freiburg": ["freiburg", "sc freiburg fc"],
    "vfl wolfsburg": ["wolfsburg", "vfl wolfsburg fc"],
    "1. fc union berlin": ["union berlin"],
    "fc augsburg": ["augsburg"],
    "1. fc heidenheim 1846": ["heidenheim", "1 fc heidenheim 1846"],
    "tsg hoffenheim": ["hoffenheim", "tsg 1899 hoffenheim"],
    "fc st. pauli": ["st. pauli", "st pauli", "fc st pauli"],
    "holstein kiel": ["kiel"],
    "sv werder bremen": ["werder bremen", "bremen"],
    "vfl bochum": ["bochum", "vfl bochum 1848"],
    "1. fsv mainz 05": ["mainz", "mainz 05", "1 fsv mainz 05"],
    "1. fc köln": ["köln", "koln", "cologne", "1 fc köln"],
    # La Liga
    "atletico madrid": ["atletico", "atl. madrid", "atl madrid", "atletico de madrid", "club atlético de madrid"],
    "real madrid": ["real madrid cf"],
    "fc barcelona": ["barcelona", "barca"],
    "real sociedad": ["real sociedad fc"],
    "athletic bilbao": ["ath bilbao", "athletic club", "athletic club bilbao"],
    "real betis": ["betis", "real betis balompié"],
    "villarreal cf": ["villarreal", "villarreal"],
    "rcd mallorca": ["mallorca"],
    "celta vigo": ["celta", "rc celta de vigo"],
    "rayo vallecano": ["rayo", "rayo vallecano de madrid"],
    "deportivo alaves": ["alaves"],
    "cd leganes": ["leganes"],
    "rcd espanyol": ["espanyol"],
    "real valladolid": ["valladolid", "real valladolid cf"],
    "ud las palmas": ["las palmas"],
    "getafe cf": ["getafe"],
    "ca osasuna": ["osasuna"],
    "girona fc": ["girona"],
    "sevilla fc": ["sevilla"],
    "valencia cf": ["valencia"],
    "levante ud": ["levante"],
    # Serie A
    "ac milan": ["milan", "ac milan fc"],
    "inter milan": ["internazionale", "fc internazionale milano"],
    "juventus": ["juve", "juventus fc"],
    "as roma": ["roma"],
    "ss lazio": ["lazio"],
    "atalanta bc": ["atalanta", "atalanta bergamo"],
    "ssc napoli": ["napoli"],
    "acf fiorentina": ["fiorentina"],
    "torino fc": ["torino"],
    "us lecce": ["lecce"],
    "cagliari calcio": ["cagliari"],
    "hellas verona": ["verona", "hellas verona fc"],
    "udinese calcio": ["udinese"],
    "genoa cfc": ["genoa"],
    "empoli fc": ["empoli"],
    "parma calcio": ["parma", "parma calcio 1913"],
    "como 1907": ["como"],
    "venezia fc": ["venezia"],
    "ac monza": ["monza"],
    "bologna fc": ["bologna", "bologna fc 1909"],
    # Ligue 1
    "paris saint-germain": ["psg", "paris sg", "paris saint-germain fc", "paris saint germain"],
    "olympique de marseille": ["marseille", "om"],
    "olympique lyonnais": ["lyon", "ol", "olympique lyon"],
    "as monaco": ["monaco", "as monaco fc"],
    "losc lille": ["lille", "lille osc"],
    "ogc nice": ["nice"],
    "rc lens": ["lens", "racing club de lens"],
    "stade rennais": ["rennes"],
    "rc strasbourg alsace": ["strasbourg"],
    "stade de reims": ["reims"],
    "fc nantes": ["nantes"],
    "montpellier hsc": ["montpellier"],
    "toulouse fc": ["toulouse"],
    "angers sco": ["angers"],
    "le havre ac": ["le havre"],
    "as saint-etienne": ["saint-etienne", "st etienne"],
    "aj auxerre": ["auxerre"],
    "fc lorient": ["lorient"],
    "fc metz": ["metz"],
    "paris fc": ["paris fc"],  # NOT PSG!
    # Eredivisie
    "psv eindhoven": ["psv"],
    "ajax": ["ajax amsterdam", "afc ajax"],
    "feyenoord": ["feyenoord rotterdam"],
    "az alkmaar": ["az"],
    "fc twente": ["twente"],
    "fc utrecht": ["utrecht"],
    "sc heerenveen": ["heerenveen"],
    "nec nijmegen": ["nec"],
    # Portugal
    "benfica": ["sl benfica"],
    "sporting cp": ["sporting lisbon", "sporting"],
    "fc porto": ["porto"],
    "sc braga": ["braga"],
    # Turkey
    "galatasaray": ["galatasaray sk"],
    "fenerbahce": ["fenerbahçe", "fenerbahce sk", "fenerbahçe sk"],
    "besiktas": ["beşiktaş", "besiktas jk", "beşiktaş jk"],
    "trabzonspor": ["trabzonspor"],
    # UCL
    "club brugge": ["brugge", "club bruges"],
    # NHL
    "colorado avalanche": ["avalanche"],
    "edmonton oilers": ["oilers"],
    "vegas golden knights": ["golden knights"],
    "winnipeg jets": ["jets"],
    "carolina hurricanes": ["hurricanes"],
    "philadelphia flyers": ["flyers"],
    "florida panthers": ["panthers"],
    "new york rangers": ["rangers"],
    "toronto maple leafs": ["maple leafs"],
    "boston bruins": ["bruins"],
    "new jersey devils": ["devils"],
    "new york islanders": ["islanders"],
    "washington capitals": ["capitals"],
    "columbus blue jackets": ["blue jackets"],
    "montreal canadiens": ["canadiens"],
    "dallas stars": ["stars"],
    "minnesota wild": ["wild"],
    "detroit red wings": ["red wings"],
    "pittsburgh penguins": ["penguins"],
    "tampa bay lightning": ["lightning"],
    "ottawa senators": ["senators"],
    "seattle kraken": ["kraken"],
    "los angeles kings": ["kings"],
    "calgary flames": ["flames"],
    "st louis blues": ["blues"],
    "buffalo sabres": ["sabres"],
    "chicago blackhawks": ["blackhawks"],
    "san jose sharks": ["sharks"],
    "anaheim ducks": ["ducks"],
    "nashville predators": ["predators"],
    # MLB
    "new york yankees": ["yankees"],
    "los angeles dodgers": ["dodgers"],
    "houston astros": ["astros"],
    "atlanta braves": ["braves"],
    "philadelphia phillies": ["phillies"],
    "baltimore orioles": ["orioles"],
    "new york mets": ["mets"],
    "san diego padres": ["padres"],
    "milwaukee brewers": ["brewers"],
    "minnesota twins": ["twins"],
    "boston red sox": ["red sox"],
    "chicago cubs": ["cubs"],
    "cleveland guardians": ["guardians"],
    "texas rangers": ["rangers"],
    "seattle mariners": ["mariners"],
    "san francisco giants": ["giants"],
    "detroit tigers": ["tigers"],
    "kansas city royals": ["royals"],
    "arizona diamondbacks": ["diamondbacks", "d-backs"],
    "colorado rockies": ["rockies"],
    "pittsburgh pirates": ["pirates"],
    "miami marlins": ["marlins"],
    "chicago white sox": ["white sox"],
    "cincinnati reds": ["reds"],
    "toronto blue jays": ["blue jays"],
    "oakland athletics": ["athletics", "a's"],
    "los angeles angels": ["angels"],
    "washington nationals": ["nationals"],
    "tampa bay rays": ["rays"],
    "st louis cardinals": ["cardinals"],
}

# ── Build reverse lookup for O(1) matching ────────────────────────────────
_ALIAS_LOOKUP: dict[str, str] = {}  # normalized name → canonical name


def _build_lookup():
    global _ALIAS_LOOKUP
    if _ALIAS_LOOKUP:
        return
    for canonical, aliases in TEAM_ALIASES.items():
        cn = normalize(canonical)
        _ALIAS_LOOKUP[cn] = canonical
        for alias in aliases:
            an = normalize(alias)
            _ALIAS_LOOKUP[an] = canonical


def normalize(name: str) -> str:
    """Normalize team name: lowercase, strip FC/CF suffixes, remove accents-ish."""
    n = name.lower().strip()
    for suffix in [" fc", " cf", " sc", " bc", " ssc", " ac", " sk", " fk"]:
        if n.endswith(suffix):
            n = n[:-len(suffix)].strip()
    for prefix in ["fc ", "cf ", "sc ", "ac ", "as ", "ss ", "sk ", "fk "]:
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
    n = n.replace(".", "").replace("'", "").replace("-", " ")
    # Strip common accents
    for a, b in [("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("ñ", "n"), ("ü", "u"), ("ö", "o"), ("ç", "c"), ("ã", "a"),
                 ("ê", "e"), ("ô", "o"), ("ø", "o"), ("å", "a"), ("ä", "a")]:
        n = n.replace(a, b)
    return n


def canonical_name(name: str) -> str | None:
    """Get canonical team name via alias lookup. Returns None if unknown."""
    _build_lookup()
    n = normalize(name)
    if n in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[n]
    # Substring fallback: check if any alias is contained in the name
    for alias_norm, canon in _ALIAS_LOOKUP.items():
        if len(alias_norm) >= 5 and alias_norm in n:
            return canon
    return None


def teams_match(name_a: str, name_b: str) -> bool:
    """Check if two team names refer to the same team."""
    ca = canonical_name(name_a)
    cb = canonical_name(name_b)
    if ca and cb:
        return ca == cb
    # Fallback: direct normalized comparison
    na = normalize(name_a)
    nb = normalize(name_b)
    return na == nb or (len(na) >= 5 and na in nb) or (len(nb) >= 5 and nb in na)


def match_event(
    odds_home: str,
    odds_away: str,
    odds_commence: str,
    pm_teams: list[str],
    pm_start_time: str,
    pm_slug: str,
) -> bool:
    """Match an Odds API event to a PM game on 3 axes: date + both teams + filters."""

    # Filter 1: no futures/outrights
    slug_lower = pm_slug.lower()
    if any(kw in slug_lower for kw in ["winner", "season", "trophy", "champion", "golden-boot", "mvp", "award"]):
        return False

    # Filter 2: commence_time within ±3 hours
    try:
        odds_dt = datetime.fromisoformat(odds_commence.replace("Z", "+00:00"))
        pm_dt = datetime.fromisoformat(pm_start_time.replace("Z", "+00:00"))
        if abs((odds_dt - pm_dt).total_seconds()) > 3 * 3600:
            return False
    except (ValueError, TypeError):
        return False  # can't parse → no match

    # Filter 3: BOTH teams must match
    home_matched = any(teams_match(odds_home, pt) for pt in pm_teams)
    away_matched = any(teams_match(odds_away, pt) for pt in pm_teams)
    return home_matched and away_matched


def extract_team_from_question(question: str) -> str:
    """Extract team name from PM question like 'Will FC Barcelona win on 2026-04-14?'"""
    q = question.strip()
    if q.lower().startswith("will "):
        q = q[5:]
    # Remove " win on YYYY-MM-DD?" or " win?"
    for pattern in [" win on ", " win?"]:
        idx = q.lower().find(pattern)
        if idx >= 0:
            return q[:idx].strip()
    return q


# ── Self-tests ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Verify critical non-matches
    assert not teams_match("Inter Milan", "Inter Miami"), "Inter Milan ≠ Inter Miami"
    assert not teams_match("Real Madrid", "Real Sociedad"), "Real Madrid ≠ Real Sociedad"
    assert not teams_match("Paris Saint-Germain", "Paris FC"), "PSG ≠ Paris FC"
    assert not teams_match("Rangers", "New York Rangers"), "Rangers (football) vs Rangers (NHL)"

    # Verify critical matches
    assert teams_match("FC Barcelona", "Barcelona"), "Barca"
    assert teams_match("Atlético Madrid", "Club Atlético de Madrid"), "Atletico"
    assert teams_match("Bayern Munich", "FC Bayern München"), "Bayern"
    assert teams_match("Paris Saint-Germain", "Paris Saint-Germain FC"), "PSG"
    assert teams_match("Liverpool", "Liverpool FC"), "Liverpool"
    assert teams_match("Colorado Avalanche", "Avalanche"), "Avalanche"
    assert teams_match("Los Angeles Dodgers", "Dodgers"), "Dodgers"
    assert teams_match("Borussia Dortmund", "BV Borussia 09 Dortmund"), "BVB"
    assert teams_match("Olympique Lyonnais", "Lyon"), "Lyon"

    # Verify event matching
    assert match_event(
        "Atlético Madrid", "Barcelona",
        "2026-04-14T19:00:00Z",
        ["club atlético de madrid", "fc barcelona"],
        "2026-04-14T19:00:00Z",
        "ucl-atm1-fcb1-2026-04-14",
    ), "UCL ATM-FCB should match"

    assert not match_event(
        "Atlético Madrid", "Barcelona",
        "2026-04-14T19:00:00Z",
        ["club atlético de madrid", "fc barcelona"],
        "2026-04-14T19:00:00Z",
        "la-liga-winner",
    ), "Season winner should NOT match"

    assert not match_event(
        "Liverpool", "Paris Saint Germain",
        "2026-04-14T19:00:00Z",
        ["paris fc", "fc metz"],
        "2026-04-19T19:00:00Z",
        "fl1-met-pfc-2026-04-19",
    ), "Paris FC ≠ PSG, and date mismatch"

    print("All tests passed!")
