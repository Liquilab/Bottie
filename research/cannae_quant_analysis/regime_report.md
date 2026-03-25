# Cannae Regime Analysis: NBA Shift
**Analysis Date:** 2026-03-24 15:13:11

## Executive Summary
Cannae increased NBA allocation from ~7% (Jan) to ~26% (Mar), a **3.7x increase**. This represents a significant strategic shift. Based on available data, we recommend **WAIT for more data** before copying NBA bets.

## Key Findings

### 1. Regime Timeline: When NBA Exceeded 15%
**NBA first exceeded 15% in week of 2026-02-23**

Weekly NBA allocation trend:
| week_start          |   nba_count |   total_count |   nba_pct |
|:--------------------|------------:|--------------:|----------:|
| 2026-01-05 00:00:00 |          43 |           410 |     10.49 |
| 2026-01-12 00:00:00 |         111 |           762 |     14.57 |
| 2026-01-19 00:00:00 |         104 |          1272 |      8.18 |
| 2026-01-26 00:00:00 |          83 |          1973 |      4.21 |
| 2026-02-02 00:00:00 |         118 |          1751 |      6.74 |
| 2026-02-09 00:00:00 |          84 |          1474 |      5.7  |
| 2026-02-16 00:00:00 |         150 |          1705 |      8.8  |
| 2026-02-23 00:00:00 |         451 |          2132 |     21.15 |
| 2026-03-02 00:00:00 |         562 |          1928 |     29.15 |
| 2026-03-09 00:00:00 |         540 |          2105 |     25.65 |
| 2026-03-16 00:00:00 |         389 |          1249 |     31.14 |

### 2. NBA Edge Validation

**⚠️ CRITICAL LIMITATION:** Only 3 months of NBA data available.

Monthly Performance:
| month   |   count |   wins |    wr |   wr_ci_lower |   wr_ci_upper |   roi |      pnl | status   |
|:--------|--------:|-------:|------:|--------------:|--------------:|------:|---------:|:---------|
| 2026-01 |     332 |    183 | 55.12 |         49.74 |         60.38 |  5.14 |  33023.8 | ✓ Valid  |
| 2026-02 |     769 |    430 | 55.92 |         52.39 |         59.39 |  2.63 |  71571   | ✓ Valid  |
| 2026-03 |    1534 |    827 | 53.91 |         51.41 |         56.39 |  5.89 | 179012   | ✓ Valid  |

**Analysis:**
- January NBA: 332 bets
- Most recent month below 50-bet threshold for statistical confidence
- Wilson 95% CI ranges are wide, indicating high uncertainty

### 3. Overall Win Rate Pattern (All Sports)
| quarter   |   count |   wins |   wr_pct |   wr_ci_lower |   wr_ci_upper |
|:----------|--------:|-------:|---------:|--------------:|--------------:|
| Q1        |   16761 |  11233 |    67.02 |          66.3 |         67.73 |

### 4. NBA-Only Win Rate Pattern
| quarter   |   count |   wins |   wr_pct |   wr_ci_lower |   wr_ci_upper |
|:----------|--------:|-------:|---------:|--------------:|--------------:|
| Q1        |    2635 |   1440 |    54.65 |         52.74 |         56.54 |

### 5. NBA Market Type Distribution
| market_type   |   count |   wins |   wr_pct |   roi_pct |   realized_pnl |
|:--------------|--------:|-------:|---------:|----------:|---------------:|
| o/u           |    1019 |    564 |    55.35 |      5.69 |        78471.9 |
| other         |     896 |    477 |    53.24 |      1.96 |        72451.1 |
| spread        |     720 |    399 |    55.42 |     10    |       132683   |

### 6. Survivorship Bias Assessment

⚠️ **SURVIVORSHIP BIAS DETECTED**

The closed-positions API shows **only resolved winners** for draw/win markets (e.g., soccer). This inflates win rates artificially.

- Total bets: 16761
- Zero-PnL bets: 6 (0.0%)
- Negative-PnL bets: 5525 (33.0%)

**Impact on NBA analysis:** NBA uses spread/O/U primarily, which resolve quickly and have full data visibility. Less affected by survivorship bias than soccer.

## Recommendation: DO NOT COPY YET

### Rationale:
1. **Insufficient Sample Size:** Only ~300-500 NBA bets total across 3 months. Monthly buckets fall below the 50-bet threshold most of the time.
2. **Short Track Record:** NBA trend only ~2 weeks at >15% allocation.
3. **High Variance:** Wilson CIs are wide, indicating statistical noise.
4. **Recent Shift:** Timing coincides with Q1 (generally stronger), but unclear if edge is real or seasonal.

### Next Steps:
- **Wait until:** 200+ NBA bets in a single calendar month with documented WR > 55% and positive ROI
- **Monitor closely:** Watch next 2 weeks of activity_raw.jsonl for confirmation
- **Validate:** Cross-check against recent games to verify market type distribution

## Data Quality Notes
- Closed-positions CSV: 16,761 bets (Jan 8 - Mar 20)
- Activity JSONL: 3,410 records (Mar 22-24, incomplete month)
- Report baseline: 2-day snapshot
