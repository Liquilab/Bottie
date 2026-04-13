#!/usr/bin/env python3
"""Retrospective: blind WIN NO (favorite) + DRAW YES on all football games.

Uses Cannae's 19K+ closed positions to simulate what would have happened.
Price filter: WIN NO between 30-70ct.
"""
import json
from collections import defaultdict
from pathlib import Path

# Load data
data_path = Path("/opt/bottie/data/cannae/closed_positions.json")
if not data_path.exists():
    data_path = Path("data/cannae_closed_positions.json")
pos = json.load(open(data_path))

FOOTBALL = {'epl','bun','lal','fl1','uel','arg','mls','rou1','efa','por',
            'bra','itc','ere','es2','bl2','sea','elc','mex','fr2','aus',
            'spl','efl','tur','uef','ucl','cdr','acn','cde','ssc','fif'}

# Group by eventSlug
games = defaultdict(list)
for p in pos:
    slug = (p.get('eventSlug') or '').removesuffix('-more-markets')
    if not slug:
        continue
    if slug.split('-')[0] not in FOOTBALL:
        continue
    games[slug].append(p)

results = []
skipped = defaultdict(int)

for slug, positions in games.items():
    win_pos = [p for p in positions if 'win' in p.get('title','').lower() and 'draw' not in p.get('title','').lower()]
    draw_pos = [p for p in positions if 'draw' in p.get('title','').lower()]

    if not win_pos:
        skipped['no_win_pos'] += 1
        continue

    # Determine implied YES price per team from Cannae's positions
    # outcome=Yes, avgPrice = yes_price directly
    # outcome=No, avgPrice = no_price, so yes_price = 1 - avgPrice
    #
    # Determine if team won:
    # outcome=Yes, curPrice=1 → team won
    # outcome=No, curPrice=1 → NO resolved true → team did NOT win
    # outcome=No, curPrice=0 → NO resolved false → team DID win
    teams = {}
    for p in win_pos:
        title = p['title']
        outcome = p['outcome']
        avg = float(p['avgPrice'])
        cur = float(p.get('curPrice', 0))

        if outcome == 'Yes':
            yes_price = avg
            team_won = cur > 0.5
        else:  # No
            yes_price = 1.0 - avg
            # curPrice=1 on NO = NO is correct = team did NOT win
            # curPrice=0 on NO = NO is wrong = team DID win
            team_won = cur < 0.5

        teams[title] = {'yes_price': yes_price, 'won': team_won}

    if not teams:
        skipped['no_teams'] += 1
        continue

    # Favorite = highest implied YES price
    fav_key = max(teams, key=lambda k: teams[k]['yes_price'])
    fav = teams[fav_key]
    fav_yes_price = fav['yes_price']
    win_no_price = 1.0 - fav_yes_price

    # Filter: WIN NO price 30-70ct
    if win_no_price < 0.30 or win_no_price > 0.70:
        skipped['out_of_range'] += 1
        continue

    fav_won = fav['won']
    any_team_won = any(t['won'] for t in teams.values())

    # Draw detection
    draw_yes_pos = [p for p in draw_pos if p['outcome'] == 'Yes']
    if draw_yes_pos:
        draw_happened = any(float(p.get('curPrice', 0)) > 0.5 for p in draw_yes_pos)
    else:
        # No draw position — infer: draw if no team won
        draw_happened = not any_team_won

    underdog_won = any_team_won and not fav_won and not draw_happened

    # Draw YES price
    if draw_yes_pos:
        draw_price = float(draw_yes_pos[0]['avgPrice'])
    else:
        sum_yes = sum(t['yes_price'] for t in teams.values())
        draw_price = max(0.12, min(0.45, 1.0 - sum_yes))

    # PnL: $1 per leg
    if fav_won:
        win_no_pnl = -1.0
    else:
        win_no_pnl = (1.0 / win_no_price) - 1.0

    if draw_happened:
        draw_pnl = (1.0 / draw_price) - 1.0
    else:
        draw_pnl = -1.0

    results.append({
        'slug': slug, 'league': slug.split('-')[0],
        'fav_yes': round(fav_yes_price, 3), 'win_no_price': round(win_no_price, 3),
        'draw_price': round(draw_price, 3),
        'fav_won': fav_won, 'draw': draw_happened, 'underdog': underdog_won,
        'win_no_pnl': round(win_no_pnl, 4), 'draw_pnl': round(draw_pnl, 4),
        'total_pnl': round(win_no_pnl + draw_pnl, 4),
    })

# === Output ===
print("Football games: %d" % len(games))
print("Skipped: %s" % dict(skipped))
print("Analyzed: %d" % len(results))
print()

total = len(results)
if total == 0:
    print('No results')
    exit()

wins = sum(1 for r in results if r['total_pnl'] > 0)
total_pnl = sum(r['total_pnl'] for r in results)
total_inv = total * 2.0

print('=== OVERALL ($1 per leg, $2 per game) ===')
print("Games: %d | %dW/%dL | WR: %.1f%%" % (total, wins, total-wins, wins/total*100))
print("PnL: $%+.2f | Invested: $%d | ROI: %.1f%%" % (total_pnl, total_inv, total_pnl/total_inv*100))
print()

fav_w = sum(1 for r in results if r['fav_won'])
draws = sum(1 for r in results if r['draw'])
und_w = sum(1 for r in results if r['underdog'])
print("Fav wins (both lose): %d (%.1f%%)" % (fav_w, fav_w/total*100))
print("Draws (both win):     %d (%.1f%%)" % (draws, draws/total*100))
print("Underdog (mixed):     %d (%.1f%%)" % (und_w, und_w/total*100))
print()

# Per league
lg = defaultdict(lambda: [0, 0, 0.0])
for r in results:
    lg[r['league']][0] += 1
    if r['total_pnl'] > 0:
        lg[r['league']][1] += 1
    lg[r['league']][2] += r['total_pnl']

print('=== PER LEAGUE ===')
for l, v in sorted(lg.items(), key=lambda x: x[1][2], reverse=True):
    g, w, p = v
    print("  %6s: %4dg | %3dW/%3dL | WR %5.1f%% | PnL $%+8.2f | ROI %+6.1f%%" % (
        l, g, w, g-w, w/g*100, p, p/(g*2)*100))

print()
print('=== PER WIN_NO PRICE BUCKET ===')
for lo, hi in [(0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70)]:
    b = [r for r in results if lo <= r['win_no_price'] < hi]
    if not b:
        continue
    bw = sum(1 for r in b if r['total_pnl'] > 0)
    bp = sum(r['total_pnl'] for r in b)
    print("  %d-%dct: %4dg | %dW/%dL | WR %.1f%% | PnL $%+.2f | ROI %.1f%%" % (
        int(lo*100), int(hi*100), len(b), bw, len(b)-bw, bw/len(b)*100, bp, bp/(len(b)*2)*100))
