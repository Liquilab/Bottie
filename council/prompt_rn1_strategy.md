# Reverse-Engineering a $200 → $7.9M Polymarket Trading Strategy

## Context

A trader known as "RN1" turned $228 into $7.9 million on Polymarket over approximately 9 months. Polymarket is a prediction market exchange on Polygon (L2 Ethereum) where users trade binary outcome tokens. Each token pays $1 if correct, $0 if wrong. Orders are placed via a central limit order book (CLOB) with GTC, FOK, and GTD order types.

## Verified Facts About RN1

These facts were verified through on-chain analysis (Polygonscan ERC-1155 transfers), the Polymarket data API, and live position monitoring:

- **Scale**: 52,000 predictions, $410M total volume, ~$1.8M/month revenue
- **Current portfolio**: ~$817K deployed across 1,749 open positions in 908 events
- **Sports coverage**: 38+ leagues/sports. Tennis (28%), UCL (14%), esports (6%), NBA (4%), NHL (4%), EPL (4%), MLB (3%)
- **Timing**: 82% of first trades on a market occur DURING the live game (verified via on-chain timestamps vs game start times). 18% are pre-game.
- **Both sides**: RN1 frequently holds positions on multiple outcomes within the same event. Example: a UCL match with 22 positions across 6 sub-markets totaling $79K.
- **Order style**: GTC limit orders (maker). Not taker/FOK. This means 0% taker fee and potential maker rebates.
- **Position sizing**: Ranges from $10 probes to $21K on a single match. 39 positions at $5K+.
- **Price range**: Heaviest buying in the 20-60 cent range ($635K of $817K deployed there).
- **Win rate on directional bets (hauptbet)**: 21%. This is critically low for a directional strategy — yet the account grew 34,000x.
- **No merges observed**: 0 merge transactions found across 5,000 Polygonscan transfers. Only 30 redemptions (market resolution payouts).
- **Started with $228**: Grew to $7.9M in cumulative value. Not funded by external deposits.

## Polymarket Market Structure

- **Neg-risk events**: Sports events use the neg-risk exchange. In a 2-team event (NBA, NHL, MLB), Team A Yes + Team B Yes = $1 at resolution (guaranteed by the exchange). In football (3-outcome), Team A Yes + Team B Yes + Draw Yes = $1.
- **Orderbook spread**: The combined best ask across all outcomes in an event is typically $1.01-$1.03. Market makers maintain this floor.
- **Token types per event**: Each "Will X win?" question is a separate conditionId with its own Yes and No token. A single NBA game has 2 win markets, a football match has 3 (Team A, Team B, Draw). Events also have spread, O/U, BTTS, and other sub-markets.
- **Merge mechanism**: The CTF contract (0x4D97...6045) allows merging complementary tokens back into $1 USDC via `mergePositions()`. This provides instant capital recycling without waiting for resolution.

## What I Want From You

I'm building an automated trading bot (Rust + Polymarket CLOB API) and I want to replicate RN1's strategy at small scale ($1,500 bankroll) with the goal of growing to $1M+.

**Please analyze the facts above and answer:**

1. **What is RN1's strategy?** Based on the verified data, explain exactly how this account generates returns. Walk through the math with concrete examples.

2. **How should positions be sized and timed?** When should each leg be placed? How do you determine allocation across outcomes? What determines whether to buy pre-game vs during-game?

3. **What are the key risks and failure modes?** What can go wrong at each stage? How does bankroll management work when capital is locked in open GTC orders?

4. **How does this scale from $1,500 to $1M?** What are the liquidity constraints at each level? How many concurrent events can you run? What's the expected daily/monthly return at various bankroll sizes?

5. **What am I probably wrong about?** Based on the data I've shared, what alternative explanations exist? What critical information am I missing? What assumptions should I test before committing capital?

Be specific. Use numbers. Show your work. If you disagree with my framing of the data, say so and explain why.
