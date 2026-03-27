#!/bin/bash
# Dagelijkse evaluatie O/U+spread experiment
# Draait op VPS als cron: 0 7 * * * /opt/bottie/scripts/evaluate_experiment.sh >> /opt/bottie/data/evaluation.log 2>&1
# (7:00 UTC = 9:00 CET)
#
# Rapporteert alleen. Past NIETS automatisch aan.

set -euo pipefail

TRADES="/opt/bottie/data/trades.jsonl"
EXPERIMENT_START="2026-03-26"
LOG_DATE=$(date -u +"%Y-%m-%d %H:%M UTC")

echo "============================================"
echo "EXPERIMENT EVALUATIE — $LOG_DATE"
echo "============================================"

if [ ! -f "$TRADES" ]; then
    echo "ERROR: $TRADES niet gevonden"
    exit 1
fi

# Filter trades sinds experiment start, alleen filled, niet dry-run
# Python one-liner voor betrouwbare JSON parsing
python3 -c "
import json, sys
from datetime import datetime

trades = []
for line in open('$TRADES'):
    line = line.strip()
    if not line: continue
    try:
        t = json.loads(line)
    except: continue
    if not t.get('filled') or t.get('dry_run'): continue
    ts = t.get('timestamp', '')
    if ts < '$EXPERIMENT_START': continue
    trades.append(t)

if not trades:
    print('Geen trades sinds $EXPERIMENT_START')
    sys.exit(0)

# Split per market type
from collections import defaultdict
by_type = defaultdict(list)
for t in trades:
    title = t.get('market_title', '')
    tl = title.lower()
    if 'o/u' in tl or 'over/under' in tl:
        mt = 'ou'
    elif 'spread' in tl:
        mt = 'spread'
    else:
        mt = 'win'
    by_type[mt].append(t)

def stats(trades_list, label):
    n = len(trades_list)
    if n == 0:
        print(f'  {label}: 0 trades')
        return
    resolved = [t for t in trades_list if t.get('result')]
    wins = [t for t in resolved if t.get('result') == 'win']
    losses = [t for t in resolved if t.get('result') == 'loss']
    open_count = len([t for t in trades_list if not t.get('result')])

    wr = len(wins) / len(resolved) * 100 if resolved else 0

    total_invested = sum(t.get('size_usdc', 0) for t in resolved)
    total_pnl = sum(t.get('actual_pnl', 0) or 0 for t in resolved)
    roi = total_pnl / total_invested * 100 if total_invested > 0 else 0

    print(f'  {label}: {n} trades ({len(resolved)} resolved, {open_count} open)')
    print(f'    WR: {wr:.1f}% ({len(wins)}W / {len(losses)}L)')
    print(f'    PnL: \${total_pnl:+.2f} on \${total_invested:.2f} invested')
    print(f'    ROI: {roi:+.1f}%')

    return {'n': n, 'resolved': len(resolved), 'wr': wr, 'roi': roi, 'pnl': total_pnl}

print(f'Totaal: {len(trades)} trades sinds $EXPERIMENT_START')
print()

all_stats = {}
for mt in ['win', 'ou', 'spread']:
    result = stats(by_type[mt], mt.upper())
    if result:
        all_stats[mt] = result

print()

# Experiment check
experiment_trades = by_type['ou'] + by_type['spread']
exp_resolved = [t for t in experiment_trades if t.get('result')]
exp_n = len(exp_resolved)

if exp_n == 0:
    print('EXPERIMENT: Geen resolved O/U+spread trades yet. Afwachten.')
elif exp_n < 10:
    print(f'EXPERIMENT: {exp_n} resolved trades. Minimaal 10 nodig voor evaluatie.')
else:
    exp_wins = len([t for t in exp_resolved if t.get('result') == 'win'])
    exp_invested = sum(t.get('size_usdc', 0) for t in exp_resolved)
    exp_pnl = sum(t.get('actual_pnl', 0) or 0 for t in exp_resolved)
    exp_wr = exp_wins / exp_n * 100
    exp_roi = exp_pnl / exp_invested * 100 if exp_invested > 0 else 0

    print(f'EXPERIMENT O/U+SPREAD: {exp_n} resolved, WR={exp_wr:.1f}%, ROI={exp_roi:+.1f}%')

    if exp_roi < -15:
        print('  ⚠ ACTIE NODIG: ROI < -15%. Overweeg terugschalen naar 25% of stop.')
        print('  Commando: ssh root@45.76.38.183 \"sed -i s/ou_spread_multiplier:.*/ou_spread_multiplier: 0.25/ /opt/bottie/config.yaml\"')
    elif exp_n >= 30 and exp_roi > 5:
        print('  ✓ OPSCHALEN NAAR 100%: ≥30 trades, ROI > 5%')
        print('  Commando: ssh root@45.76.38.183 \"sed -i s/ou_spread_multiplier:.*/ou_spread_multiplier: 1.0/ /opt/bottie/config.yaml\"')
    elif exp_n >= 15 and exp_roi > 0:
        print('  ✓ OPSCHALEN NAAR 75%: ≥15 trades, ROI > 0%')
        print('  Commando: ssh root@45.76.38.183 \"sed -i s/ou_spread_multiplier:.*/ou_spread_multiplier: 0.75/ /opt/bottie/config.yaml\"')
    else:
        print('  → Huidige 50% handhaven. Afwachten meer data.')

print()
print('============================================')
"
