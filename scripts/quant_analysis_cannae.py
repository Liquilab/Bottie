#!/usr/bin/env python3
"""
Senior quant analysis of Cannae wallet for copy-trade optimization.
All metrics capital-weighted (naar rato) by default.
"""
import json, re, math
from collections import defaultdict, Counter
from datetime import datetime, timezone

BANKROLL = 900

with open("data/wallet_0x7ea5_raw.json") as f:
    records = json.load(f)

def sf(v):
    if v is None: return None
    try: return float(v)
    except: return None

def r2(x):
    return round(x, 2) if x is not None else None

# ═══════════════════════════════════════════════════════
# DERIVATIONS
# ═══════════════════════════════════════════════════════
for r in records:
    r['avgPrice'] = sf(r.get('avgPrice'))
    r['totalBought'] = sf(r.get('totalBought'))
    r['realizedPnl'] = sf(r.get('realizedPnl'))
    r['curPrice'] = sf(r.get('curPrice'))
    r['initialValue'] = sf(r.get('initialValue'))
    r['currentValue'] = sf(r.get('currentValue'))
    rdm = r.get('redeemable')
    if isinstance(rdm, str): rdm = rdm.lower() == 'true'
    r['redeemable'] = bool(rdm) if rdm is not None else False

    src = r['source_endpoint']
    cp = r['curPrice']
    if src == 'closed_positions': r['status'] = 'closed'
    elif src == 'positions' and r['redeemable']: r['status'] = 'redeemable'
    elif src == 'positions' and (cp == 0 or cp == 1): r['status'] = 'closed'
    elif src == 'positions' and cp is not None and 0 < cp < 1 and not r['redeemable']: r['status'] = 'open'
    else: r['status'] = 'other_status'

    if r['status'] != 'closed': r['won'] = None
    elif r['realizedPnl'] is None: r['won'] = None
    elif cp == 1: r['won'] = 1
    elif cp == 0: r['won'] = 0
    elif r['realizedPnl'] > 0: r['won'] = 1
    elif r['realizedPnl'] < 0: r['won'] = 0
    elif r['realizedPnl'] == 0: r['won'] = None
    else: r['won'] = None

    r['usdc_in'] = r['totalBought'] if src == 'closed_positions' else r['initialValue']

    title = r.get('title', '') or ''
    if 'Spread:' in title: r['market_type'] = 'spread'
    elif 'O/U' in title: r['market_type'] = 'over_under'
    elif 'Both Teams to Score' in title: r['market_type'] = 'btts'
    elif re.search(r'Will .* end in a draw', title, re.I): r['market_type'] = 'draw'
    elif re.search(r'Will .* win', title, re.I): r['market_type'] = 'moneyline'
    else: r['market_type'] = 'other'

    slug = (r.get('slug', '') or '').lower()
    sport_map = [('nba-','NBA'),('nhl-','NHL'),('nfl-','NFL'),('mlb-','MLB'),('ucl-','UCL'),('uel-','UEL'),('epl-','EPL'),('ligue1-','Ligue1'),('bundesliga-','Bundesliga'),('seriea-','SerieA'),('laliga-','LaLiga'),('brasileirao-','Brasileirao'),('superlig-','Superlig'),('eredivisie-','Eredivisie'),('primeira-','PrimeiraLiga')]
    r['sport'] = 'other'
    for prefix, label in sport_map:
        if prefix in slug: r['sport'] = label; break

    es = r.get('eventSlug')
    r['game_id'] = str(es).strip() if es and str(es).strip() else r.get('conditionId', 'unknown')

    # outcome side
    outcome = (r.get('outcome', '') or '').strip()
    r['outcome'] = outcome
    r['oppositeOutcome'] = (r.get('oppositeOutcome', '') or '').strip()

    # price bucket
    ap = r['avgPrice']
    if ap is None or ap < 0 or ap >= 1.0:
        r['price_bucket'] = 'invalid' if (ap is None or ap < 0 or ap > 1.0) else '>95ct'
    elif ap < 0.10: r['price_bucket'] = '<10ct'
    elif ap < 0.20: r['price_bucket'] = '10-20ct'
    elif ap < 0.30: r['price_bucket'] = '20-30ct'
    elif ap < 0.45: r['price_bucket'] = '30-45ct'
    elif ap < 0.58: r['price_bucket'] = '45-58ct'
    elif ap < 0.83: r['price_bucket'] = '58-83ct'
    elif ap < 0.95: r['price_bucket'] = '83-95ct'
    else: r['price_bucket'] = '>95ct'

# ═══════════════════════════════════════════════════════
# GAME GROUPING + HEDGE DETECTION
# ═══════════════════════════════════════════════════════
games = defaultdict(list)
for r in records:
    games[r['game_id']].append(r)

for gid, legs in games.items():
    # Hedge detection
    is_hedged = False
    for i, l1 in enumerate(legs):
        for l2 in legs[i+1:]:
            o1, o2 = l1['outcome'], l2['outcome']
            oo1, oo2 = l1['oppositeOutcome'], l2['oppositeOutcome']
            if (o1 and o1 == oo2) or (o2 and o2 == oo1):
                is_hedged = True
            elif (o1, o2) in [('Yes','No'),('No','Yes'),('Over','Under'),('Under','Over')]:
                is_hedged = True
    for leg in legs:
        leg['is_hedged_game'] = is_hedged
        leg['game_legs'] = len(legs)
        leg['game_market_types'] = list(set(l['market_type'] for l in legs))

# ═══════════════════════════════════════════════════════
# ANALYSIS HELPERS
# ═══════════════════════════════════════════════════════
closed = [r for r in records if r['status'] == 'closed']
graded = [r for r in closed if r['won'] in (0, 1) and r['usdc_in'] and r['usdc_in'] > 0]

def wwr(recs, min_n=10):
    """Stake-weighted win rate"""
    e = [r for r in recs if r['won'] in (0,1) and r['usdc_in'] and r['usdc_in'] > 0]
    if len(e) < min_n: return None, len(e), 0, 0
    total = sum(r['usdc_in'] for r in e)
    win_u = sum(r['usdc_in'] for r in e if r['won'] == 1)
    return r2(win_u/total*100), len(e), r2(total), r2(win_u)

def wroi(recs, min_n=10):
    """Weighted ROI = sum(pnl) / sum(usdc_in)"""
    e = [r for r in recs if r['status'] == 'closed' and r['realizedPnl'] is not None and r['usdc_in'] and r['usdc_in'] > 0]
    if len(e) < min_n: return None, len(e), 0, 0
    usdc = sum(r['usdc_in'] for r in e)
    pnl = sum(r['realizedPnl'] for r in e)
    return r2(pnl/usdc*100), len(e), r2(usdc), r2(pnl)

def uwr(recs, min_n=10):
    """Unweighted win rate (context only)"""
    e = [r for r in recs if r['won'] in (0,1)]
    if len(e) < min_n: return None, len(e)
    return r2(sum(1 for r in e if r['won']==1)/len(e)*100), len(e)

P = []
def p(s=""): P.append(s)

# ═══════════════════════════════════════════════════════
# 1) PORTFOLIO BASELINE
# ═══════════════════════════════════════════════════════
p("=" * 70)
p("1) PORTFOLIO BASELINE (kapitaal-gewogen)")
p("=" * 70)

roi_all, n_all, usdc_all, pnl_all = wroi(closed)
wwr_all, wn_all, wusdc_all, wwin_all = wwr(graded)
uwr_all, un_all = uwr(graded)

p(f"Weighted ROI:         {roi_all}%")
p(f"Stake-weighted WR:    {wwr_all}% (n={wn_all})")
p(f"Unweighted WR:        {uwr_all}% (n={un_all})")
p(f"Total USDC deployed:  ${usdc_all:,.2f}")
p(f"Total realized PnL:   ${pnl_all:,.2f}")

# ═══════════════════════════════════════════════════════
# 2) STRATEGIE-DECOMPOSITIE
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("2) STRATEGIE-DECOMPOSITIE (alle metrics naar rato)")
p("=" * 70)

# 2a: moneyline vs non-moneyline
p("\n--- 2a: Moneyline vs Non-Moneyline ---")
ml = [r for r in closed if r['market_type'] == 'moneyline']
non_ml = [r for r in closed if r['market_type'] != 'moneyline']
for label, recs in [("Moneyline", ml), ("Non-moneyline", non_ml)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    p(f"  {label:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# 2b: moneyline YES vs NO
p("\n--- 2b: Moneyline YES vs NO ---")
ml_yes = [r for r in ml if r['outcome'] == 'Yes']
ml_no = [r for r in ml if r['outcome'] == 'No']
for label, recs in [("ML Yes", ml_yes), ("ML No", ml_no)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    usdc_share = r2(usdc / sf(wroi(ml)[2]) * 100) if wroi(ml)[2] else 0
    p(f"  {label:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}  share={usdc_share}%")

# 2c: draw YES vs NO
p("\n--- 2c: Draw YES vs NO ---")
dr = [r for r in closed if r['market_type'] == 'draw']
dr_yes = [r for r in dr if r['outcome'] == 'Yes']
dr_no = [r for r in dr if r['outcome'] == 'No']
for label, recs in [("Draw Yes", dr_yes), ("Draw No", dr_no)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    p(f"  {label:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# 2d: spread / O/U / btts / other
p("\n--- 2d: Overige market types ---")
for mt in ['spread', 'over_under', 'btts', 'other']:
    recs = [r for r in closed if r['market_type'] == mt]
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    p(f"  {mt:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# 2e: per sport
p("\n--- 2e: Per Sport ---")
sports_order = ['EPL','UCL','UEL','LaLiga','Ligue1','Bundesliga','SerieA','Brasileirao','Superlig','Eredivisie','PrimeiraLiga','NBA','NHL','NFL','other']
for sp in sports_order:
    recs = [r for r in closed if r['sport'] == sp]
    if len(recs) < 5: continue
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    usdc_share = r2(usdc / sf(wroi(closed)[2]) * 100) if wroi(closed)[2] else 0
    p(f"  {sp:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}  share={usdc_share}%")

# 2f: per price bucket
p("\n--- 2f: Per Price Bucket ---")
for pb in ['<10ct','10-20ct','20-30ct','30-45ct','45-58ct','58-83ct','83-95ct','>95ct']:
    recs = [r for r in closed if r['price_bucket'] == pb]
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    # edge: wWR - mean(avgPrice) weighted
    e_recs = [r for r in recs if r['won'] in (0,1) and r['usdc_in'] and r['usdc_in'] > 0]
    if e_recs:
        w_avg = sum(r['avgPrice'] * r['usdc_in'] for r in e_recs) / sum(r['usdc_in'] for r in e_recs)
        w_edge = r2(sf(ww) - w_avg * 100) if ww else None
    else:
        w_edge = None
    p(f"  {pb:15s}  wROI={roi}%  wWR={ww}%  w_edge={w_edge}pp  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# ═══════════════════════════════════════════════════════
# 3) HEDGING IMPACT
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("3) HEDGING IMPACT")
p("=" * 70)

hedged_legs = [r for r in closed if r['is_hedged_game']]
unhedged_legs = [r for r in closed if not r['is_hedged_game']]

hedged_usdc = sum(r['usdc_in'] or 0 for r in hedged_legs)
unhedged_usdc = sum(r['usdc_in'] or 0 for r in unhedged_legs)
total_usdc = hedged_usdc + unhedged_usdc

hedged_games = set(r['game_id'] for r in hedged_legs)
unhedged_games = set(r['game_id'] for r in unhedged_legs)
all_closed_games = hedged_games | unhedged_games

p(f"Hedged games:    {len(hedged_games)} ({r2(len(hedged_games)/len(all_closed_games)*100)}%)")
p(f"Unhedged games:  {len(unhedged_games)} ({r2(len(unhedged_games)/len(all_closed_games)*100)}%)")
p(f"Hedged legs:     {len(hedged_legs)} ({r2(len(hedged_legs)/len(closed)*100)}%)")
p(f"Unhedged legs:   {len(unhedged_legs)} ({r2(len(unhedged_legs)/len(closed)*100)}%)")
p(f"Hedged USDC:     ${hedged_usdc:>14,.2f} ({r2(hedged_usdc/total_usdc*100)}%)")
p(f"Unhedged USDC:   ${unhedged_usdc:>14,.2f} ({r2(unhedged_usdc/total_usdc*100)}%)")

for label, recs in [("Hedged legs", hedged_legs), ("Unhedged legs", unhedged_legs)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    p(f"  {label:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  PnL=${pnl:>12,.2f}")

# Game-level hedged vs unhedged
p("\n--- Game-level ---")
for label, game_set in [("Hedged games", hedged_games), ("Unhedged games", unhedged_games)]:
    g_win_usdc = 0
    g_loss_usdc = 0
    g_pnl = 0
    g_usdc = 0
    g_count = 0
    for gid in game_set:
        legs = games[gid]
        if not all(r['status'] == 'closed' for r in legs): continue
        if any(r['won'] is None for r in legs): continue
        gu = sum(r['usdc_in'] or 0 for r in legs)
        gp = sum(r['realizedPnl'] or 0 for r in legs)
        g_usdc += gu
        g_pnl += gp
        g_count += 1
        if all(r['won'] == 1 for r in legs):
            g_win_usdc += gu
        else:
            g_loss_usdc += gu
    g_total = g_win_usdc + g_loss_usdc
    gwwr = r2(g_win_usdc / g_total * 100) if g_total > 0 else None
    groi = r2(g_pnl / g_usdc * 100) if g_usdc > 0 else None
    p(f"  {label:20s}  game_wWR={gwwr}%  game_wROI={groi}%  n_games={g_count}  USDC=${g_usdc:>14,.2f}  PnL=${g_pnl:>12,.2f}")

# ═══════════════════════════════════════════════════════
# 4) CONTEXT TESTS
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("4) CONTEXT TESTS")
p("=" * 70)

# 4a: ML NO vs ML YES in overlapping games
p("\n--- 4a: ML YES vs ML NO in games that have BOTH ---")
ml_both_games = set()
for gid, legs in games.items():
    ml_legs = [l for l in legs if l['market_type'] == 'moneyline' and l['status'] == 'closed']
    outcomes = set(l['outcome'] for l in ml_legs)
    if 'Yes' in outcomes and 'No' in outcomes:
        ml_both_games.add(gid)

ml_yes_overlap = [r for r in closed if r['market_type'] == 'moneyline' and r['outcome'] == 'Yes' and r['game_id'] in ml_both_games]
ml_no_overlap = [r for r in closed if r['market_type'] == 'moneyline' and r['outcome'] == 'No' and r['game_id'] in ml_both_games]

for label, recs in [("ML Yes (overlap)", ml_yes_overlap), ("ML No (overlap)", ml_no_overlap)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    p(f"  {label:25s}  wROI={roi}%  wWR={ww}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# 4b: ML NO vs Draw YES in same games
p("\n--- 4b: ML NO vs Draw YES in same games ---")
ml_draw_games = set()
for gid, legs in games.items():
    has_ml_no = any(l['market_type'] == 'moneyline' and l['outcome'] == 'No' for l in legs)
    has_draw_yes = any(l['market_type'] == 'draw' and l['outcome'] == 'Yes' for l in legs)
    if has_ml_no and has_draw_yes:
        ml_draw_games.add(gid)

ml_no_ctx = [r for r in closed if r['market_type'] == 'moneyline' and r['outcome'] == 'No' and r['game_id'] in ml_draw_games]
dr_yes_ctx = [r for r in closed if r['market_type'] == 'draw' and r['outcome'] == 'Yes' and r['game_id'] in ml_draw_games]

for label, recs in [("ML No (ctx)", ml_no_ctx), ("Draw Yes (ctx)", dr_yes_ctx)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    p(f"  {label:25s}  wROI={roi}%  wWR={ww}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# ═══════════════════════════════════════════════════════
# 5) ROBUUSTHEID
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("5) ROBUUSTHEID")
p("=" * 70)

# Single-leg games only
single_leg = [r for r in closed if r['game_legs'] == 1]
p("\n--- Single-leg games only ---")
roi, n, usdc, pnl = wroi(single_leg)
ww, wn, _, _ = wwr([r for r in single_leg if r['won'] in (0,1)])
uw, un = uwr(single_leg)
p(f"  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# Unhedged legs only
p("\n--- Unhedged legs only ---")
roi, n, usdc, pnl = wroi(unhedged_legs)
ww, wn, _, _ = wwr([r for r in unhedged_legs if r['won'] in (0,1)])
uw, un = uwr(unhedged_legs)
p(f"  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# Unhedged per market type
p("\n--- Unhedged per market type ---")
for mt in ['moneyline', 'draw', 'over_under', 'btts', 'spread', 'other']:
    recs = [r for r in unhedged_legs if r['market_type'] == mt]
    roi, n, usdc, pnl = wroi(recs, min_n=5)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)], min_n=5)
    uw, un = uwr(recs, min_n=5)
    flag = "  ⚠️ n<30" if n < 30 else ""
    p(f"  {mt:20s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}{flag}")

# Non-moneyline non-draw (pure directional bets)
p("\n--- Pure directional (excl moneyline + draw) ---")
pure_dir = [r for r in closed if r['market_type'] not in ('moneyline', 'draw')]
roi, n, usdc, pnl = wroi(pure_dir)
ww, wn, _, _ = wwr([r for r in pure_dir if r['won'] in (0,1)])
uw, un = uwr(pure_dir)
p(f"  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# Football only directional
p("\n--- Football-only directional (excl moneyline + draw, excl NBA/NHL) ---")
football_dir = [r for r in pure_dir if r['sport'] not in ('NBA', 'NHL', 'NFL')]
roi, n, usdc, pnl = wroi(football_dir)
ww, wn, _, _ = wwr([r for r in football_dir if r['won'] in (0,1)])
uw, un = uwr(football_dir)
p(f"  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# NBA only
p("\n--- NBA only (all market types) ---")
nba = [r for r in closed if r['sport'] == 'NBA']
roi, n, usdc, pnl = wroi(nba)
ww, wn, _, _ = wwr([r for r in nba if r['won'] in (0,1)])
uw, un = uwr(nba)
p(f"  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# Football moneyline+draw (the hedge engine)
p("\n--- Football moneyline+draw only ---")
fb_ml_dr = [r for r in closed if r['market_type'] in ('moneyline', 'draw') and r['sport'] not in ('NBA', 'NHL', 'NFL')]
roi, n, usdc, pnl = wroi(fb_ml_dr)
ww, wn, _, _ = wwr([r for r in fb_ml_dr if r['won'] in (0,1)])
p(f"  wROI={roi}%  wWR={ww}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# ═══════════════════════════════════════════════════════
# 6) SPREIDING / RISICO
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("6) SPREIDING / RISICO")
p("=" * 70)

# Concentration: top games by USDC
game_usdc = {}
for gid, legs in games.items():
    cl_legs = [r for r in legs if r['status'] == 'closed']
    if cl_legs:
        game_usdc[gid] = sum(r['usdc_in'] or 0 for r in cl_legs)

sorted_games = sorted(game_usdc.items(), key=lambda x: x[1], reverse=True)
total_game_usdc = sum(game_usdc.values())

top5 = sum(v for _, v in sorted_games[:5])
top10 = sum(v for _, v in sorted_games[:10])
top20 = sum(v for _, v in sorted_games[:20])

p(f"Top-5 games:   ${top5:>14,.2f} ({r2(top5/total_game_usdc*100)}% of total)")
p(f"Top-10 games:  ${top10:>14,.2f} ({r2(top10/total_game_usdc*100)}% of total)")
p(f"Top-20 games:  ${top20:>14,.2f} ({r2(top20/total_game_usdc*100)}% of total)")
p(f"Total games:   {len(game_usdc)}")

# Sport concentration
p("\n--- Sport concentration (closed USDC) ---")
sport_usdc = defaultdict(float)
for r in closed:
    if r['usdc_in']: sport_usdc[r['sport']] += r['usdc_in']
for sp, usdc in sorted(sport_usdc.items(), key=lambda x: x[1], reverse=True):
    p(f"  {sp:20s}  ${usdc:>14,.2f}  ({r2(usdc/total_game_usdc*100)}%)")

# Market type concentration
p("\n--- Market type concentration (closed USDC) ---")
mt_usdc = defaultdict(float)
for r in closed:
    if r['usdc_in']: mt_usdc[r['market_type']] += r['usdc_in']
for mt, usdc in sorted(mt_usdc.items(), key=lambda x: x[1], reverse=True):
    p(f"  {mt:20s}  ${usdc:>14,.2f}  ({r2(usdc/total_game_usdc*100)}%)")

# Median games per week
from datetime import timedelta
week_games = defaultdict(set)
for r in closed:
    ed = r.get('endDate')
    if ed:
        try:
            ed_clean = str(ed).replace('Z', '+00:00')
            dt = datetime.fromisoformat(ed_clean)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            wk = dt.strftime('%G-W%V')
            week_games[wk].add(r['game_id'])
        except: pass

games_per_week = [len(gs) for gs in week_games.values()]
if games_per_week:
    p(f"\nGames per week: min={min(games_per_week)}, median={sorted(games_per_week)[len(games_per_week)//2]}, max={max(games_per_week)}")

# ═══════════════════════════════════════════════════════
# 7) RECENT PERFORMANCE (last 4 weeks vs overall)
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("7) RECENT vs OVERALL (edge decay check)")
p("=" * 70)

# Use endDate for time bucketing
recent_cutoff = datetime(2026, 2, 19, tzinfo=timezone.utc)  # last 4 weeks
early = []
recent = []
for r in closed:
    ed = r.get('endDate')
    if not ed: continue
    try:
        ed_clean = str(ed).replace('Z', '+00:00')
        dt = datetime.fromisoformat(ed_clean)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        if dt >= recent_cutoff:
            recent.append(r)
        else:
            early.append(r)
    except: pass

for label, recs in [("Early (before 2/19)", early), ("Recent (2/19+)", recent)]:
    roi, n, usdc, pnl = wroi(recs)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)])
    uw, un = uwr(recs)
    p(f"  {label:25s}  wROI={roi}%  wWR={ww}%  uWR={uw}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}")

# Recent per market type
p("\n--- Recent (4 wk) per market type ---")
for mt in ['moneyline', 'draw', 'over_under', 'btts', 'spread', 'other']:
    recs = [r for r in recent if r['market_type'] == mt]
    roi, n, usdc, pnl = wroi(recs, min_n=5)
    ww, wn, _, _ = wwr([r for r in recs if r['won'] in (0,1)], min_n=5)
    flag = "  ⚠️ n<30" if n < 30 else ""
    p(f"  {mt:20s}  wROI={roi}%  wWR={ww}%  n={n}  USDC=${usdc:>14,.2f}  PnL=${pnl:>12,.2f}{flag}")

# ═══════════════════════════════════════════════════════
# 8) CANNAE SIZING ANALYSIS
# ═══════════════════════════════════════════════════════
p()
p("=" * 70)
p("8) CANNAE SIZING PATTERNS (voor proportioneel kopiëren)")
p("=" * 70)

# What % of bankroll does Cannae put per game?
# Estimate Cannae's bankroll from total open + recent weekly deployment
# Recent weeks: ~$5M/week deployed, assume 2-week cycle, bankroll ~$2-3M
# More precise: use concurrent exposure

# Per-game sizing as % of weekly USDC
week_usdc = defaultdict(float)
week_game_sizes = defaultdict(list)
for gid, legs in games.items():
    cl_legs = [r for r in legs if r['status'] == 'closed']
    if not cl_legs: continue
    gu = sum(r['usdc_in'] or 0 for r in cl_legs)
    ed = cl_legs[0].get('endDate')
    if not ed: continue
    try:
        ed_clean = str(ed).replace('Z', '+00:00')
        dt = datetime.fromisoformat(ed_clean)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        wk = dt.strftime('%G-W%V')
        week_usdc[wk] += gu
        week_game_sizes[wk].append(gu)
    except: pass

game_pcts = []
for wk, sizes in week_game_sizes.items():
    wk_total = week_usdc[wk]
    if wk_total > 0:
        for s in sizes:
            game_pcts.append(s / wk_total * 100)

if game_pcts:
    game_pcts.sort()
    p(f"Game as % of weekly USDC:")
    p(f"  p50={r2(game_pcts[len(game_pcts)//2])}%  p75={r2(game_pcts[int(len(game_pcts)*0.75)])}%  p90={r2(game_pcts[int(len(game_pcts)*0.9)])}%  p95={r2(game_pcts[int(len(game_pcts)*0.95)])}%  max={r2(max(game_pcts))}%")

# Market type allocation within multi-leg games
p("\n--- Intra-game allocation (multi-leg, closed) ---")
mt_allocs = defaultdict(list)
for gid, legs in games.items():
    cl_legs = [r for r in legs if r['status'] == 'closed']
    if len(cl_legs) < 2: continue
    types = set(r['market_type'] for r in cl_legs)
    if len(types) < 2: continue
    gu = sum(r['usdc_in'] or 0 for r in cl_legs)
    if gu == 0: continue
    for r in cl_legs:
        pct = (r['usdc_in'] or 0) / gu * 100
        mt_allocs[r['market_type']].append(pct)

for mt in ['moneyline', 'draw', 'over_under', 'btts', 'spread', 'other']:
    vals = mt_allocs.get(mt, [])
    if vals:
        vals.sort()
        mn = r2(sum(vals)/len(vals))
        md = r2(vals[len(vals)//2])
        p(f"  {mt:20s}  mean={mn}%  median={md}%  n={len(vals)}")

# ═══════════════════════════════════════════════════════
# OUTPUT
# ═══════════════════════════════════════════════════════
report = "\n".join(P)
print(report)

with open("report_quant_cannae.md", "w") as f:
    f.write(report)
