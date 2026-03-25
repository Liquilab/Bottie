#!/usr/bin/env bash
# Wait for prepare.py to finish, then run score.py
set -euo pipefail
cd "$(dirname "$0")/../.."

BULK="data/consensus_bulk.json"
echo "Waiting for prepare.py to complete..."

while true; do
    # Check if prepare.py is still running
    if ! pgrep -f "consensus/prepare.py" > /dev/null 2>&1; then
        if [ -f "$BULK" ]; then
            echo "prepare.py finished. Bulk data ready."
            break
        else
            echo "prepare.py not running and no data found. Something went wrong."
            exit 1
        fi
    fi
    
    # Show progress
    if [ -f "$BULK" ]; then
        SIZE=$(wc -c < "$BULK" 2>/dev/null || echo 0)
        echo "  $(date +%H:%M:%S) - Still downloading... bulk size: ${SIZE} bytes"
    fi
    sleep 30
done

echo ""
echo "=== Running score.py ==="
python3 research/consensus/score.py

echo ""
echo "=== RESULTS READY ==="
echo "File: data/consensus_results.json"
echo "Finished: $(date)"

# Show quick summary
python3 -c "
import json
r = json.loads(open('data/consensus_results.json').read())
print(f\"Valid wallets: {r['valid_wallets']}\")
print(f\"Pairs analyzed: {r['pairs_analyzed']}\")
print(f\"Top pairs: {len(r.get('top_pairs',[]))}\")
print(f\"Portfolios: {len(r.get('portfolios',[]))}\")
if r.get('portfolios'):
    best = r['portfolios'][0]
    print(f\"\\nBest portfolio (score={best['score']}):\")
    for w in best.get('wallets',[]):
        print(f\"  {w['name']:20s} | WR={w['wr']:.0%} | {w.get('sport','?')}\")
    print(f\"  Consensus: {best.get('consensus_wins',0)}/{best.get('consensus_events',0)} = {best.get('consensus_wr',0):.0%}\")
"
