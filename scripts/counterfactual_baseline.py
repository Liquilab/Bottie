#!/usr/bin/env python3
"""
Counterfactual Baseline Analysis: Cannae Football Betting
Determines how much of Cannae's ROI is structural (Win NO + Draw YES pattern)
vs his selection/timing/sizing skill.
"""
import json
from collections import defaultdict

FOOTBALL_PREFIXES = [
    'epl','bun','sea','lal','ucl','uel','aus','ere','bra','fl1','fr2',
    'bl2','elc','tur','spl','arg','por','rou1','mex','cde','efa','es2',
    'efl','acn','itc','ssc','cdr','uef'
]

def get_league(slug):
    for pre in sorted(FOOTBALL_PREFIXES, key=len, reverse=True):
        if slug.startswith(pre + '-'):
            return pre
    return None

def classify_leg(pos):
    """Classify a position as 'win_no', 'draw_yes', 'draw_no', 'win_yes', 'other'"""
    title_lower = pos['title'].lower()
    outcome = pos['outcome']
    is_draw = 'draw' in title_lower or 'end in a draw' in title_lower

    if is_draw and outcome == 'Yes':
        return 'draw_yes'
    elif is_draw and outcome == 'No':
        return 'draw_no'
    elif not is_draw and 'win' in title_lower and outcome == 'No':
        return 'win_no'
    elif not is_draw and 'win' in title_lower and outcome == 'Yes':
        return 'win_yes'
    else:
        return 'other'

def calc_flat_pnl(pos, flat_amount=100):
    """Calculate P&L if we had bet a flat amount instead of actual sizing."""
    avg_price = pos['avgPrice']
    won = pos['curPrice'] == 1
    if won:
        pnl = flat_amount * (1.0 / avg_price - 1.0)
    else:
        pnl = -flat_amount
    return pnl, flat_amount

def main():
    with open('/opt/bottie/data/cannae/closed_positions.json') as f:
        data = json.load(f)

    # Filter football
    football = []
    for p in data:
        league = get_league(p['slug'])
        if league:
            p['_league'] = league
            p['_leg_type'] = classify_leg(p)
            football.append(p)

    print(f"Total football positions: {len(football)}")

    # Count leg types
    from collections import Counter
    type_counts = Counter(p['_leg_type'] for p in football)
    print(f"Leg types: {dict(type_counts)}")
    print()

    # ========== GLOBAL ANALYSIS ==========
    # Group by event
    events = defaultdict(list)
    for p in football:
        events[p['eventSlug']].append(p)

    print(f"Total unique events: {len(events)}")
    print()

    # ========== PER-LEAGUE ANALYSIS ==========
    # Structure: league -> metrics
    league_data = defaultdict(lambda: {
        'actual_pnl': 0, 'actual_invested': 0,
        'flat_pnl': 0, 'flat_invested': 0,
        'win_no_flat_pnl': 0, 'win_no_flat_invested': 0,
        'draw_yes_flat_pnl': 0, 'draw_yes_flat_invested': 0,
        'n_positions': 0, 'n_events': 0,
        'n_win_no': 0, 'n_draw_yes': 0,
        'win_no_wins': 0, 'draw_yes_wins': 0,
    })

    # Track events per league
    league_events = defaultdict(set)

    for p in football:
        league = p['_league']
        leg_type = p['_leg_type']
        ld = league_data[league]

        # Actual
        ld['actual_pnl'] += p['realizedPnl']
        ld['actual_invested'] += p['totalBought']
        ld['n_positions'] += 1
        league_events[league].add(p['eventSlug'])

        # Flat (all legs)
        flat_pnl, flat_inv = calc_flat_pnl(p)
        ld['flat_pnl'] += flat_pnl
        ld['flat_invested'] += flat_inv

        # Win NO only
        if leg_type == 'win_no':
            pnl, inv = calc_flat_pnl(p)
            ld['win_no_flat_pnl'] += pnl
            ld['win_no_flat_invested'] += inv
            ld['n_win_no'] += 1
            if p['curPrice'] == 1:
                ld['win_no_wins'] += 1

        # Draw YES only
        if leg_type == 'draw_yes':
            pnl, inv = calc_flat_pnl(p)
            ld['draw_yes_flat_pnl'] += pnl
            ld['draw_yes_flat_invested'] += inv
            ld['n_draw_yes'] += 1
            if p['curPrice'] == 1:
                ld['draw_yes_wins'] += 1

    for league in league_events:
        league_data[league]['n_events'] = len(league_events[league])

    # ========== OUTPUT ==========
    def roi(pnl, inv):
        if inv == 0:
            return 0
        return pnl / inv * 100

    def winrate(wins, total):
        if total == 0:
            return 0
        return wins / total * 100

    # Sort by actual invested (biggest leagues first)
    sorted_leagues = sorted(league_data.keys(), key=lambda l: league_data[l]['actual_invested'], reverse=True)

    # Print header
    print("=" * 145)
    print(f"{'League':<7} {'Events':>6} {'Pos':>5} | {'Cannae ROI':>10} {'Cannae P&L':>11} | {'Flat ROI':>9} {'Flat P&L':>10} | {'WinNO ROI':>9} {'WinNO WR':>8} {'N':>5} | {'DrawY ROI':>9} {'DrawY WR':>8} {'N':>5} | {'Alpha':>7}")
    print("=" * 145)

    # Totals
    totals = {
        'actual_pnl': 0, 'actual_invested': 0,
        'flat_pnl': 0, 'flat_invested': 0,
        'win_no_flat_pnl': 0, 'win_no_flat_invested': 0,
        'draw_yes_flat_pnl': 0, 'draw_yes_flat_invested': 0,
        'n_positions': 0, 'n_events': 0,
        'n_win_no': 0, 'n_draw_yes': 0,
        'win_no_wins': 0, 'draw_yes_wins': 0,
    }

    for league in sorted_leagues:
        ld = league_data[league]

        for k in totals:
            totals[k] += ld[k]

        c_roi = roi(ld['actual_pnl'], ld['actual_invested'])
        f_roi = roi(ld['flat_pnl'], ld['flat_invested'])
        wn_roi = roi(ld['win_no_flat_pnl'], ld['win_no_flat_invested'])
        dy_roi = roi(ld['draw_yes_flat_pnl'], ld['draw_yes_flat_invested'])
        alpha = c_roi - f_roi
        wn_wr = winrate(ld['win_no_wins'], ld['n_win_no'])
        dy_wr = winrate(ld['draw_yes_wins'], ld['n_draw_yes'])

        print(f"{league:<7} {ld['n_events']:>6} {ld['n_positions']:>5} | {c_roi:>9.2f}% {ld['actual_pnl']:>10.0f}$ | {f_roi:>8.2f}% {ld['flat_pnl']:>9.0f}$ | {wn_roi:>8.2f}% {wn_wr:>7.1f}% {ld['n_win_no']:>5} | {dy_roi:>8.2f}% {dy_wr:>7.1f}% {ld['n_draw_yes']:>5} | {alpha:>6.2f}%")

    print("=" * 145)

    # Totals row
    c_roi = roi(totals['actual_pnl'], totals['actual_invested'])
    f_roi = roi(totals['flat_pnl'], totals['flat_invested'])
    wn_roi = roi(totals['win_no_flat_pnl'], totals['win_no_flat_invested'])
    dy_roi = roi(totals['draw_yes_flat_pnl'], totals['draw_yes_flat_invested'])
    alpha = c_roi - f_roi
    wn_wr = winrate(totals['win_no_wins'], totals['n_win_no'])
    dy_wr = winrate(totals['draw_yes_wins'], totals['n_draw_yes'])

    print(f"{'TOTAL':<7} {totals['n_events']:>6} {totals['n_positions']:>5} | {c_roi:>9.2f}% {totals['actual_pnl']:>10.0f}$ | {f_roi:>8.2f}% {totals['flat_pnl']:>9.0f}$ | {wn_roi:>8.2f}% {wn_wr:>7.1f}% {totals['n_win_no']:>5} | {dy_roi:>8.2f}% {dy_wr:>7.1f}% {totals['n_draw_yes']:>5} | {alpha:>6.2f}%")
    print()

    # ========== INTERPRETATION ==========
    print("=" * 80)
    print("INTERPRETATION")
    print("=" * 80)
    print()
    print(f"Cannae Actual ROI:       {c_roi:.2f}%  (${totals['actual_pnl']:.0f} P&L on ${totals['actual_invested']:.0f} invested)")
    print(f"Naive Flat ROI:          {f_roi:.2f}%  (same events, $100 flat per leg)")
    print(f"Sizing/Selection Alpha:  {alpha:.2f}pp")
    print()
    print(f"Win NO only ROI (flat):  {wn_roi:.2f}%  (winrate: {wn_wr:.1f}%, n={totals['n_win_no']})")
    print(f"Draw YES only ROI (flat):{dy_roi:.2f}%  (winrate: {dy_wr:.1f}%, n={totals['n_draw_yes']})")
    print()

    structural_roi = roi(totals['win_no_flat_pnl'] + totals['draw_yes_flat_pnl'],
                         totals['win_no_flat_invested'] + totals['draw_yes_flat_invested'])
    print(f"Structural (WinNO+DrawY combined flat): {structural_roi:.2f}%")
    print(f"Other legs contribute: {f_roi:.2f}% (all flat) vs {structural_roi:.2f}% (structural only)")
    print()

    # Win NO vs Draw YES P&L contribution
    print("=" * 80)
    print("P&L CONTRIBUTION BREAKDOWN (Cannae Actual)")
    print("=" * 80)

    type_pnl = defaultdict(lambda: {'pnl': 0, 'invested': 0, 'n': 0})
    for p in football:
        lt = p['_leg_type']
        type_pnl[lt]['pnl'] += p['realizedPnl']
        type_pnl[lt]['invested'] += p['totalBought']
        type_pnl[lt]['n'] += 1

    for lt in ['win_no', 'draw_yes', 'draw_no', 'win_yes', 'other']:
        d = type_pnl[lt]
        r = roi(d['pnl'], d['invested'])
        pct_pnl = d['pnl'] / totals['actual_pnl'] * 100 if totals['actual_pnl'] != 0 else 0
        print(f"  {lt:<12}: ROI={r:>7.2f}%, P&L=${d['pnl']:>10.0f}, Invested=${d['invested']:>10.0f}, N={d['n']:>5}, P&L share={pct_pnl:>6.1f}%")

    print()

    # ========== AVG PRICE ANALYSIS ==========
    print("=" * 80)
    print("AVERAGE ENTRY PRICE BY LEG TYPE (Cannae vs Market)")
    print("=" * 80)

    for lt in ['win_no', 'draw_yes', 'draw_no', 'win_yes']:
        legs = [p for p in football if p['_leg_type'] == lt]
        if not legs:
            continue
        avg_entry = sum(p['avgPrice'] * p['totalBought'] for p in legs) / sum(p['totalBought'] for p in legs)
        simple_avg = sum(p['avgPrice'] for p in legs) / len(legs)
        wr = sum(1 for p in legs if p['curPrice'] == 1) / len(legs) * 100
        print(f"  {lt:<12}: Weighted avg price={avg_entry:.3f}, Simple avg={simple_avg:.3f}, Winrate={wr:.1f}%, N={len(legs)}")

if __name__ == '__main__':
    main()
