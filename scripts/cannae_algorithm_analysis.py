#!/usr/bin/env python3
"""
Cannae Algorithm Reverse-Engineering Analysis

Analyzes 16K+ closed positions to answer:
1. Is there a sizing algorithm?
2. Is there a selection algorithm?
3. Is there a hedge algorithm?
4. What's the loss pattern?
5. Backtest: our strategy vs alternatives
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "research" / "cannae_trades"
CLOSED_CSV = DATA_DIR / "cannae_closed_full.csv"
OUT_MD = DATA_DIR / "cannae_algorithm_analysis.md"


def sf(v, d=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return d


def load_data():
    with open(CLOSED_CSV) as f:
        return list(csv.DictReader(f))


def group_by_event(rows):
    """Group positions by eventSlug to form games."""
    games = defaultdict(list)
    for r in rows:
        slug = r.get("event_slug", "")
        if slug:
            games[slug].append(r)
    return dict(games)


def classify_game(positions):
    """Classify a game's structure."""
    outcomes = {}
    for p in positions:
        outcome = p["outcome"]
        key = outcome
        bought = sf(p["total_bought"])
        price = sf(p["avg_price"])
        pnl = sf(p["realized_pnl"])
        cost = bought * price
        won = p["won"] == "1"

        if key not in outcomes:
            outcomes[key] = {"outcome": outcome, "shares": 0, "cost": 0, "pnl": 0, "won": won, "prices": [], "positions": []}
        outcomes[key]["shares"] += bought
        outcomes[key]["cost"] += cost
        outcomes[key]["pnl"] += pnl
        outcomes[key]["prices"].append(price)
        outcomes[key]["positions"].append(p)

    # Sort by cost (biggest leg first)
    legs = sorted(outcomes.values(), key=lambda x: x["cost"], reverse=True)

    total_cost = sum(l["cost"] for l in legs)
    total_pnl = sum(l["pnl"] for l in legs)
    total_shares = sum(l["shares"] for l in legs)
    game_won = total_pnl > 0

    # Determine structure
    n_legs = len(legs)
    hauptbet = legs[0] if legs else None
    hauptbet_pct = (hauptbet["cost"] / total_cost * 100) if hauptbet and total_cost > 0 else 0

    # Extract league from slug
    slug = positions[0].get("event_slug", "")
    parts = slug.split("-")
    league = parts[0] if parts else "?"
    date = positions[0].get("date", "")
    end_date = positions[0].get("end_date", "")

    return {
        "slug": slug,
        "league": league,
        "date": date,
        "end_date": end_date,
        "n_legs": n_legs,
        "legs": legs,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_shares": total_shares,
        "game_won": game_won,
        "hauptbet_pct": hauptbet_pct,
    }


def analyze_sizing(games):
    """Question 1: Is there a sizing algorithm?"""
    lines = ["## 1. Sizing Algorithm Analysis\n"]

    # Distribution of game sizes
    sizes = [g["total_cost"] for g in games]
    sizes_sorted = sorted(sizes, reverse=True)

    lines.append(f"**Total games:** {len(games)}")
    lines.append(f"**Total invested:** ${sum(sizes):,.0f}")
    lines.append(f"**Avg game size:** ${sum(sizes)/len(sizes):,.2f}")
    lines.append(f"**Median game size:** ${sizes_sorted[len(sizes_sorted)//2]:,.2f}")
    lines.append(f"**Max game size:** ${sizes_sorted[0]:,.2f}")
    lines.append(f"**Min game size:** ${sizes_sorted[-1]:,.2f}")
    lines.append("")

    # Size tiers
    tiers = [(0, 1), (1, 10), (10, 50), (50, 100), (100, 500), (500, 1000), (1000, 5000), (5000, float("inf"))]
    lines.append("### Game size distribution (USDC)\n")
    lines.append("| Tier | Count | % | Avg PnL | Win% |")
    lines.append("|------|-------|---|---------|------|")
    for lo, hi in tiers:
        tier_games = [g for g in games if lo <= g["total_cost"] < hi]
        if tier_games:
            avg_pnl = sum(g["total_pnl"] for g in tier_games) / len(tier_games)
            win_pct = sum(1 for g in tier_games if g["game_won"]) / len(tier_games) * 100
            label = f"${lo}-${hi}" if hi != float("inf") else f"${lo}+"
            lines.append(f"| {label} | {len(tier_games)} | {len(tier_games)/len(games)*100:.1f}% | ${avg_pnl:,.2f} | {win_pct:.1f}% |")
    lines.append("")

    # Hauptbet price vs size
    lines.append("### Hauptbet price vs game size\n")
    price_buckets = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
    lines.append("| Price range | Count | Avg game size | Avg PnL | Win% |")
    lines.append("|-------------|-------|---------------|---------|------|")
    for lo, hi in price_buckets:
        bucket_games = []
        for g in games:
            if g["legs"]:
                hp = sum(g["legs"][0]["prices"]) / len(g["legs"][0]["prices"])
                if lo <= hp < hi:
                    bucket_games.append(g)
        if bucket_games:
            avg_size = sum(g["total_cost"] for g in bucket_games) / len(bucket_games)
            avg_pnl = sum(g["total_pnl"] for g in bucket_games) / len(bucket_games)
            win_pct = sum(1 for g in bucket_games if g["game_won"]) / len(bucket_games) * 100
            lines.append(f"| {lo:.1f}-{hi:.1f} | {len(bucket_games)} | ${avg_size:,.2f} | ${avg_pnl:,.2f} | {win_pct:.1f}% |")
    lines.append("")

    # Hauptbet % distribution
    lines.append("### Hauptbet % of total game\n")
    hb_buckets = [(0, 50), (50, 70), (70, 80), (80, 90), (90, 95), (95, 100.1)]
    lines.append("| Hauptbet % | Count | Avg PnL | Win% |")
    lines.append("|------------|-------|---------|------|")
    for lo, hi in hb_buckets:
        b = [g for g in games if lo <= g["hauptbet_pct"] < hi]
        if b:
            avg_pnl = sum(g["total_pnl"] for g in b) / len(b)
            win_pct = sum(1 for g in b if g["game_won"]) / len(b) * 100
            lines.append(f"| {lo:.0f}-{hi:.0f}% | {len(b)} | ${avg_pnl:,.2f} | {win_pct:.1f}% |")
    lines.append("")

    return "\n".join(lines)


def analyze_selection(games):
    """Question 2: Is there a selection algorithm?"""
    lines = ["## 2. Selection Algorithm Analysis\n"]

    # Per league
    league_stats = defaultdict(lambda: {"count": 0, "won": 0, "pnl": 0, "invested": 0})
    for g in games:
        l = g["league"]
        league_stats[l]["count"] += 1
        league_stats[l]["won"] += 1 if g["game_won"] else 0
        league_stats[l]["pnl"] += g["total_pnl"]
        league_stats[l]["invested"] += g["total_cost"]

    lines.append("### Per-league performance\n")
    lines.append("| League | Games | Won | Win% | Total PnL | Avg PnL | ROI% |")
    lines.append("|--------|-------|-----|------|-----------|---------|------|")
    for l, s in sorted(league_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        if s["count"] >= 5:
            wr = s["won"] / s["count"] * 100
            roi = s["pnl"] / s["invested"] * 100 if s["invested"] > 0 else 0
            lines.append(f"| {l} | {s['count']} | {s['won']} | {wr:.1f}% | ${s['pnl']:,.0f} | ${s['pnl']/s['count']:,.0f} | {roi:.1f}% |")
    lines.append("")

    # N-legs distribution
    legs_dist = Counter(g["n_legs"] for g in games)
    lines.append("### Number of legs per game\n")
    lines.append("| Legs | Count | % | Win% | Avg PnL |")
    lines.append("|------|-------|---|------|---------|")
    for n in sorted(legs_dist.keys()):
        ng = [g for g in games if g["n_legs"] == n]
        wr = sum(1 for g in ng if g["game_won"]) / len(ng) * 100
        ap = sum(g["total_pnl"] for g in ng) / len(ng)
        lines.append(f"| {n} | {legs_dist[n]} | {legs_dist[n]/len(games)*100:.1f}% | {wr:.1f}% | ${ap:,.2f} |")
    lines.append("")

    # Sport type analysis (from title keywords)
    lines.append("### Market type analysis\n")
    types = {"spread": 0, "o/u": 0, "draw": 0, "win": 0, "btts": 0, "other": 0}
    type_pnl = defaultdict(float)
    type_count = defaultdict(int)
    type_won = defaultdict(int)

    for g in games:
        for leg in g["legs"]:
            for p in leg["positions"]:
                title = p["title"].lower()
                if "spread" in title:
                    t = "spread"
                elif "o/u" in title:
                    t = "o/u"
                elif "draw" in title:
                    t = "draw"
                elif "both teams" in title or "btts" in title:
                    t = "btts"
                elif "win" in title:
                    t = "win"
                else:
                    t = "other"
                type_count[t] += 1
                type_pnl[t] += sf(p["realized_pnl"])
                type_won[t] += 1 if p["won"] == "1" else 0

    lines.append("| Type | Positions | Won | Win% | Total PnL |")
    lines.append("|------|-----------|-----|------|-----------|")
    for t in sorted(type_count.keys(), key=lambda x: type_pnl[x], reverse=True):
        wr = type_won[t] / type_count[t] * 100 if type_count[t] > 0 else 0
        lines.append(f"| {t} | {type_count[t]} | {type_won[t]} | {wr:.1f}% | ${type_pnl[t]:,.0f} |")
    lines.append("")

    return "\n".join(lines)


def analyze_hedge(games):
    """Question 3: Is there a hedge algorithm?"""
    lines = ["## 3. Hedge / Multi-Leg Analysis\n"]

    multi_leg = [g for g in games if g["n_legs"] >= 2]
    single_leg = [g for g in games if g["n_legs"] == 1]

    lines.append(f"**Single-leg games:** {len(single_leg)}")
    lines.append(f"**Multi-leg games:** {len(multi_leg)}")
    lines.append("")

    if multi_leg:
        # Hedge ratio = (total_cost - hauptbet_cost) / total_cost
        lines.append("### Hedge ratio distribution (multi-leg games)\n")
        lines.append("| Hedge ratio | Count | Win% | Avg PnL |")
        lines.append("|-------------|-------|------|---------|")
        hr_buckets = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 100.1)]
        for lo, hi in hr_buckets:
            b = [g for g in multi_leg if lo <= (100 - g["hauptbet_pct"]) < hi]
            if b:
                wr = sum(1 for g in b if g["game_won"]) / len(b) * 100
                ap = sum(g["total_pnl"] for g in b) / len(b)
                lines.append(f"| {lo}-{hi:.0f}% | {len(b)} | {wr:.1f}% | ${ap:,.2f} |")
        lines.append("")

    # Compare single vs multi-leg PnL
    lines.append("### Single-leg vs Multi-leg comparison\n")
    for label, subset in [("Single-leg", single_leg), ("Multi-leg (2+)", multi_leg)]:
        if subset:
            wr = sum(1 for g in subset if g["game_won"]) / len(subset) * 100
            tp = sum(g["total_pnl"] for g in subset)
            ti = sum(g["total_cost"] for g in subset)
            roi = tp / ti * 100 if ti > 0 else 0
            ap = tp / len(subset)
            lines.append(f"- **{label}:** {len(subset)} games, {wr:.1f}% win, ${tp:,.0f} PnL, {roi:.1f}% ROI, ${ap:,.0f} avg")
    lines.append("")

    # What does the 2nd/3rd leg look like?
    if multi_leg:
        lines.append("### Secondary leg structure (multi-leg games)\n")
        sec_outcomes = Counter()
        for g in multi_leg:
            if len(g["legs"]) >= 2:
                sec_outcomes[g["legs"][1]["outcome"]] += 1
        lines.append("| 2nd leg outcome | Count |")
        lines.append("|-----------------|-------|")
        for o, c in sec_outcomes.most_common(10):
            lines.append(f"| {o} | {c} |")
        lines.append("")

    return "\n".join(lines)


def analyze_losses(games):
    """Question 4: What's the loss pattern?"""
    lines = ["## 4. Loss Pattern Analysis\n"]

    winners = [g for g in games if g["game_won"]]
    losers = [g for g in games if not g["game_won"]]

    lines.append(f"**Winners:** {len(winners)} ({len(winners)/len(games)*100:.1f}%)")
    lines.append(f"**Losers:** {len(losers)} ({len(losers)/len(games)*100:.1f}%)")
    lines.append("")

    if winners:
        avg_win = sum(g["total_pnl"] for g in winners) / len(winners)
        med_win = sorted(g["total_pnl"] for g in winners)[len(winners)//2]
        lines.append(f"**Avg win:** ${avg_win:,.2f}, **Median win:** ${med_win:,.2f}")

    if losers:
        avg_loss = sum(g["total_pnl"] for g in losers) / len(losers)
        med_loss = sorted(g["total_pnl"] for g in losers)[len(losers)//2]
        lines.append(f"**Avg loss:** ${avg_loss:,.2f}, **Median loss:** ${med_loss:,.2f}")
    lines.append("")

    # Biggest losses
    lines.append("### Top 20 biggest losses\n")
    lines.append("| Game | League | Cost | PnL | Legs | Hauptbet% |")
    lines.append("|------|--------|------|-----|------|-----------|")
    for g in sorted(games, key=lambda x: x["total_pnl"])[:20]:
        lines.append(f"| {g['slug'][:40]} | {g['league']} | ${g['total_cost']:,.0f} | ${g['total_pnl']:,.0f} | {g['n_legs']} | {g['hauptbet_pct']:.0f}% |")
    lines.append("")

    # Top 20 biggest wins
    lines.append("### Top 20 biggest wins\n")
    lines.append("| Game | League | Cost | PnL | Legs | Hauptbet% |")
    lines.append("|------|--------|------|-----|------|-----------|")
    for g in sorted(games, key=lambda x: x["total_pnl"], reverse=True)[:20]:
        lines.append(f"| {g['slug'][:40]} | {g['league']} | ${g['total_cost']:,.0f} | ${g['total_pnl']:,.0f} | {g['n_legs']} | {g['hauptbet_pct']:.0f}% |")
    lines.append("")

    # Loss by game size
    lines.append("### Loss rate by game size\n")
    lines.append("| Size tier | Games | Losers | Loss% | Total loss |")
    lines.append("|-----------|-------|--------|-------|------------|")
    tiers = [(0, 10), (10, 50), (50, 100), (100, 500), (500, 1000), (1000, float("inf"))]
    for lo, hi in tiers:
        t = [g for g in games if lo <= g["total_cost"] < hi]
        if t:
            tl = [g for g in t if not g["game_won"]]
            total_loss = sum(g["total_pnl"] for g in tl)
            label = f"${lo}-${hi}" if hi != float("inf") else f"${lo}+"
            lines.append(f"| {label} | {len(t)} | {len(tl)} | {len(tl)/len(t)*100:.1f}% | ${total_loss:,.0f} |")
    lines.append("")

    return "\n".join(lines)


def backtest_strategies(games):
    """Backtest different strategies on the historical data."""
    lines = ["## 5. Strategy Backtest\n"]

    lines.append("Simulating what our PnL would have been with different strategies.\n")

    # Strategy 1: Full copy (all legs, proportional)
    full_pnl = sum(g["total_pnl"] for g in games)
    full_invested = sum(g["total_cost"] for g in games)

    # Strategy 2: Only hauptbet (1 leg)
    haupt_pnl = sum(g["legs"][0]["pnl"] for g in games if g["legs"])
    haupt_invested = sum(g["legs"][0]["cost"] for g in games if g["legs"])

    # Strategy 3: Top 3 legs
    top3_pnl = 0
    top3_invested = 0
    for g in games:
        for leg in g["legs"][:3]:
            top3_pnl += leg["pnl"]
            top3_invested += leg["cost"]

    # Strategy 4: Only games with hedge >10%
    hedged = [g for g in games if g["n_legs"] >= 2 and (100 - g["hauptbet_pct"]) >= 10]
    hedged_pnl = sum(g["total_pnl"] for g in hedged)
    hedged_invested = sum(g["total_cost"] for g in hedged)

    # Strategy 5: Only winning leagues (positive ROI)
    league_roi = defaultdict(lambda: {"pnl": 0, "invested": 0})
    for g in games:
        league_roi[g["league"]]["pnl"] += g["total_pnl"]
        league_roi[g["league"]]["invested"] += g["total_cost"]
    winning_leagues = {l for l, s in league_roi.items() if s["invested"] > 0 and s["pnl"] / s["invested"] > 0}
    league_filter_games = [g for g in games if g["league"] in winning_leagues]
    lf_pnl = sum(g["total_pnl"] for g in league_filter_games)
    lf_invested = sum(g["total_cost"] for g in league_filter_games)

    # Strategy 6: Only games > $10 (skip dust)
    big_games = [g for g in games if g["total_cost"] >= 10]
    big_pnl = sum(g["total_pnl"] for g in big_games)
    big_invested = sum(g["total_cost"] for g in big_games)

    # Strategy 7: Only games > $50
    bigger_games = [g for g in games if g["total_cost"] >= 50]
    bigger_pnl = sum(g["total_pnl"] for g in bigger_games)
    bigger_invested = sum(g["total_cost"] for g in bigger_games)

    lines.append("| Strategy | Games | Invested | PnL | ROI% |")
    lines.append("|----------|-------|----------|-----|------|")

    strategies = [
        ("Full copy (all legs)", len(games), full_invested, full_pnl),
        ("Only hauptbet (1 leg)", len(games), haupt_invested, haupt_pnl),
        ("Top 3 legs", len(games), top3_invested, top3_pnl),
        ("Only hedged (>10% hedge)", len(hedged), hedged_invested, hedged_pnl),
        (f"Winning leagues only ({len(winning_leagues)})", len(league_filter_games), lf_invested, lf_pnl),
        ("Games > $10", len(big_games), big_invested, big_pnl),
        ("Games > $50", len(bigger_games), bigger_invested, bigger_pnl),
    ]

    for name, count, inv, pnl in strategies:
        roi = pnl / inv * 100 if inv > 0 else 0
        lines.append(f"| {name} | {count} | ${inv:,.0f} | ${pnl:,.0f} | {roi:.1f}% |")
    lines.append("")

    # Winning leagues list
    lines.append(f"\n**Winning leagues:** {', '.join(sorted(winning_leagues))}\n")

    return "\n".join(lines)


def time_analysis(games):
    """Analyze performance over time."""
    lines = ["## 6. Performance Over Time\n"]

    # Group by week
    from datetime import datetime
    weekly = defaultdict(lambda: {"pnl": 0, "invested": 0, "count": 0, "won": 0})
    for g in games:
        if g["date"]:
            try:
                dt = datetime.strptime(g["date"], "%Y-%m-%d")
                week = dt.strftime("%Y-W%U")
                weekly[week]["pnl"] += g["total_pnl"]
                weekly[week]["invested"] += g["total_cost"]
                weekly[week]["count"] += 1
                weekly[week]["won"] += 1 if g["game_won"] else 0
            except ValueError:
                pass

    lines.append("### Weekly performance\n")
    lines.append("| Week | Games | Won | Win% | Invested | PnL | ROI% | Cumulative PnL |")
    lines.append("|------|-------|-----|------|----------|-----|------|----------------|")
    cum_pnl = 0
    for week in sorted(weekly.keys()):
        s = weekly[week]
        cum_pnl += s["pnl"]
        wr = s["won"] / s["count"] * 100 if s["count"] > 0 else 0
        roi = s["pnl"] / s["invested"] * 100 if s["invested"] > 0 else 0
        lines.append(f"| {week} | {s['count']} | {s['won']} | {wr:.0f}% | ${s['invested']:,.0f} | ${s['pnl']:,.0f} | {roi:.1f}% | ${cum_pnl:,.0f} |")
    lines.append("")

    return "\n".join(lines)


def main():
    print("Loading data...", flush=True)
    rows = load_data()
    print(f"  {len(rows)} closed positions loaded")

    print("Grouping by event...", flush=True)
    event_groups = group_by_event(rows)
    print(f"  {len(event_groups)} unique events")

    print("Classifying games...", flush=True)
    games = []
    for slug, positions in event_groups.items():
        g = classify_game(positions)
        games.append(g)

    # Sort by date desc
    games.sort(key=lambda x: x["date"], reverse=True)
    print(f"  {len(games)} games classified")

    # Summary
    total_pnl = sum(g["total_pnl"] for g in games)
    total_invested = sum(g["total_cost"] for g in games)
    won = sum(1 for g in games if g["game_won"])
    print(f"\n  SUMMARY: {len(games)} games, {won} won ({won/len(games)*100:.1f}%), PnL=${total_pnl:,.0f}, ROI={total_pnl/total_invested*100:.1f}%")

    # Run analyses
    print("\nRunning analyses...", flush=True)
    report = []
    report.append("# Cannae Algorithm Reverse-Engineering Analysis\n")
    report.append(f"**Data:** {len(rows)} closed positions across {len(games)} games")
    report.append(f"**Period:** {games[-1]['date']} → {games[0]['date']}")
    report.append(f"**Total invested:** ${total_invested:,.0f}")
    report.append(f"**Total PnL:** ${total_pnl:,.0f}")
    report.append(f"**ROI:** {total_pnl/total_invested*100:.2f}%")
    report.append(f"**Win rate:** {won/len(games)*100:.1f}% ({won}/{len(games)})")
    report.append("")

    report.append(analyze_sizing(games))
    report.append(analyze_selection(games))
    report.append(analyze_hedge(games))
    report.append(analyze_losses(games))
    report.append(backtest_strategies(games))
    report.append(time_analysis(games))

    # Write report
    with open(OUT_MD, "w") as f:
        f.write("\n".join(report))

    print(f"\nReport written to {OUT_MD}")


if __name__ == "__main__":
    main()
