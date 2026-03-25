#!/usr/bin/env bash
# Consensus Pipeline Runner — Karpathy-style prepare → score → results
set -euo pipefail

cd "$(dirname "$0")/../.."

echo "=== CONSENSUS PIPELINE ==="
echo "Started: $(date)"

# Step 1: Check if bulk data exists and is recent (< 12h old)
BULK="data/consensus_bulk.json"
if [ -f "$BULK" ]; then
    AGE=$(( $(date +%s) - $(stat -f%m "$BULK" 2>/dev/null || stat -c%Y "$BULK" 2>/dev/null) ))
    if [ "$AGE" -lt 43200 ]; then
        echo "Bulk data is ${AGE}s old (< 12h). Skipping download."
    else
        echo "Bulk data is ${AGE}s old (> 12h). Re-downloading..."
        python3 research/consensus/prepare.py
    fi
else
    echo "No bulk data found. Downloading..."
    python3 research/consensus/prepare.py
fi

# Step 2: Run scoring
echo ""
echo "=== SCORING ==="
python3 research/consensus/score.py

echo ""
echo "=== DONE ==="
echo "Results: data/consensus_results.json"
echo "Finished: $(date)"
