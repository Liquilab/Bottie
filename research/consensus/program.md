# Consensus Wallet Discovery — Research Program

## Goal

Find the optimal portfolio of 6-8 Polymarket wallets that maximizes
**consensus signal frequency × consensus win rate**.

A "consensus signal" = 2+ wallets from our portfolio betting the same side
of the same event within a 2-hour window.

Consensus is searched across ALL domains — not just sports:
- Sports (NBA, NFL, NHL, soccer, tennis, MMA, esports, NCAA)
- Finance (stocks, S&P, earnings, IPOs)
- Economics (Fed rates, CPI, GDP, unemployment)
- Politics (elections, legislation)
- Entertainment (Oscars, box office)
- Crypto (Bitcoin, Ethereum price)

## Method (Karpathy-style)

1. `prepare.py` downloads data (immutable, run once)
2. `score.py` scores wallet portfolios (iterable, agent modifies)
3. Each experiment: modify scoring → run → evaluate → keep or discard

## What prepare.py does (DO NOT MODIFY)

- Fetches SPORTS leaderboard top 100 (WEEK + MONTH, PNL + VOL)
- For each wallet: downloads closed positions (last 7 days)
- For each wallet: downloads current positions (for spread farmer detection)
- Saves everything to `data/consensus_bulk.json`

## What score.py optimizes

### Inputs
- `data/consensus_bulk.json` (from prepare.py)

### Scoring dimensions
1. **Overlap frequency**: how many events do wallet pairs share?
2. **Agreement rate**: when they share an event, do they bet the same side?
3. **Consensus WR**: when they agree, does the consensus bet win?
4. **Individual quality**: WR > 55%, sharpe > 0, sport_pct > 50%

### Filters (hard constraints)
- both_sides_ratio > 0.20 → EXCLUDE (spread farmer)
- closed_positions < 10 → EXCLUDE (insufficient data)
- last_activity > 4 days → EXCLUDE (stale)
- sport_pct < 0.50 → EXCLUDE (not sport-focused)

### Portfolio score formula
```
portfolio_score = Σ over all pairs (i,j):
    overlap_count(i,j) × agreement_rate(i,j) × consensus_wr(i,j)

Penalize: portfolios with < 3 overlapping pairs
Bonus: portfolios where top pair has 5+ shared events
```

### Constraints
- Portfolio size: 5-8 wallets
- At least 2 wallets must share 3+ events
- No more than 3 wallets in the same sport niche (diversification)

## Output

`data/consensus_results.json`:
```json
{
  "best_portfolio": [...],
  "pair_rankings": [...],
  "individual_rankings": [...],
  "consensus_stats": {...}
}
```

## Metric to minimize

`-1 × portfolio_score` (maximize consensus quality)

Equivalent to: maximize expected profitable consensus signals per week.
