"""Module 8: First Principles Analysis — WHY does Cannae's edge work?

Not descriptive (what happens) but causal (why it works, when it stops).

Sections:
1. Return decomposition: which market types/leagues DRIVE profit?
2. Sizing confidence: are Q4 results statistically significant?
3. Edge stability: rolling 30-bet WR/ROI with regime detection
4. Actionable rules: not "WR=49%" but "keep O/U despite low WR because payoff > cost"
"""

import math
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("intelligence.first_principles")


def analyze_first_principles(dataset: dict) -> dict:
    """Full first principles analysis."""
    resolved = dataset["resolved"]
    if len(resolved) < 20:
        return {"insufficient_data": True, "n": len(resolved)}

    return {
        "return_decomposition": _return_decomposition(resolved),
        "sizing_significance": _sizing_significance(resolved),
        "edge_stability": _edge_stability(resolved),
        "actionable_rules": _actionable_rules(resolved),
        "risk_metrics": _risk_metrics(resolved),
    }


def _return_decomposition(resolved: list) -> dict:
    """Where does profit actually come from? Decompose by market type and league."""
    total_pnl = sum(b["pnl"] for b in resolved)
    total_cost = sum(b["cost"] for b in resolved)

    # By market type
    by_mt = defaultdict(lambda: {"pnl": 0, "cost": 0, "w": 0, "l": 0, "bets": []})
    for b in resolved:
        d = by_mt[b["mt"]]
        d["pnl"] += b["pnl"]
        d["cost"] += b["cost"]
        d["bets"].append(b)
        if b["result"] == "WIN":
            d["w"] += 1
        else:
            d["l"] += 1

    mt_decomp = {}
    for mt, d in sorted(by_mt.items(), key=lambda x: -x[1]["pnl"]):
        n = d["w"] + d["l"]
        wr = d["w"] / n if n > 0 else 0
        roi = d["pnl"] / d["cost"] if d["cost"] > 0 else 0
        avg_win = statistics.mean([b["pnl"] for b in d["bets"] if b["result"] == "WIN"]) if d["w"] > 0 else 0
        avg_loss = statistics.mean([abs(b["pnl"]) for b in d["bets"] if b["result"] == "LOSS"]) if d["l"] > 0 else 0
        # Profit contribution
        contribution = d["pnl"] / total_pnl if total_pnl != 0 else 0

        mt_decomp[mt] = {
            "bets": n,
            "pnl": round(d["pnl"], 2),
            "pnl_contribution_pct": round(contribution * 100, 1),
            "wr": round(wr, 4),
            "roi": round(roi, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "payoff_ratio": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0,
            "verdict": _mt_verdict(wr, roi, avg_win, avg_loss, n),
        }

    # By league
    by_league = defaultdict(lambda: {"pnl": 0, "cost": 0, "w": 0, "l": 0})
    for b in resolved:
        d = by_league[b["league"]]
        d["pnl"] += b["pnl"]
        d["cost"] += b["cost"]
        if b["result"] == "WIN":
            d["w"] += 1
        else:
            d["l"] += 1

    league_decomp = {}
    for lg, d in sorted(by_league.items(), key=lambda x: -x[1]["pnl"]):
        n = d["w"] + d["l"]
        wr = d["w"] / n if n > 0 else 0
        roi = d["pnl"] / d["cost"] if d["cost"] > 0 else 0
        contribution = d["pnl"] / total_pnl if total_pnl != 0 else 0
        league_decomp[lg] = {
            "bets": n,
            "pnl": round(d["pnl"], 2),
            "pnl_contribution_pct": round(contribution * 100, 1),
            "wr": round(wr, 4),
            "roi": round(roi, 4),
        }

    return {
        "total_pnl": round(total_pnl, 2),
        "total_cost": round(total_cost, 2),
        "by_market_type": mt_decomp,
        "by_league": league_decomp,
    }


def _mt_verdict(wr, roi, avg_win, avg_loss, n) -> str:
    """Actionable verdict for a market type."""
    if n < 10:
        return "INSUFFICIENT_DATA"
    if roi < -0.05:
        return "LOSING — consider dropping"
    if wr < 0.50 and roi > 0.10:
        return "LOW_WR_HIGH_PAYOFF — keep (payoff compensates)"
    if wr >= 0.65 and roi > 0.10:
        return "STRONG — core strategy"
    if roi > 0 and roi < 0.10:
        return "MARGINAL — monitor closely"
    return "PROFITABLE"


def _sizing_significance(resolved: list) -> dict:
    """Are the sizing quartile results statistically significant?

    Uses Wilson CI + binomial test approximation.
    """
    costs = sorted(b["cost"] for b in resolved)
    if len(costs) < 20:
        return {"insufficient_data": True}

    q25 = costs[len(costs) // 4]
    q75 = costs[3 * len(costs) // 4]

    quartiles = {"Q1_small": [], "Q4_large": []}
    for b in resolved:
        if b["cost"] <= q25:
            quartiles["Q1_small"].append(b)
        elif b["cost"] > q75:
            quartiles["Q4_large"].append(b)

    result = {}
    overall_wr = sum(1 for b in resolved if b["result"] == "WIN") / len(resolved)

    for label, bets in quartiles.items():
        wins = sum(1 for b in bets if b["result"] == "WIN")
        n = len(bets)
        if n == 0:
            continue
        wr = wins / n
        ci_low, ci_high = _wilson_ci(wins, n)

        # Is this quartile significantly different from overall?
        # Simple z-test for proportions
        if n > 0 and overall_wr > 0 and overall_wr < 1:
            se = math.sqrt(overall_wr * (1 - overall_wr) / n)
            z = (wr - overall_wr) / se if se > 0 else 0
        else:
            z = 0

        result[label] = {
            "bets": n,
            "wr": round(wr, 4),
            "ci_95": [round(ci_low, 4), round(ci_high, 4)],
            "z_score": round(z, 2),
            "significant": abs(z) > 1.96,
            "interpretation": _interpret_significance(label, wr, overall_wr, z, n),
        }

    # Does bigger size = higher WR? (Spearman-like)
    result["overall_wr"] = round(overall_wr, 4)
    result["sizing_matters"] = (
        result.get("Q4_large", {}).get("significant", False) and
        result.get("Q4_large", {}).get("wr", 0) > overall_wr
    )

    return result


def _interpret_significance(label, wr, overall_wr, z, n):
    """Human-readable interpretation."""
    if n < 15:
        return f"Alleen {n} bets — te weinig voor conclusie"
    if abs(z) < 1.96:
        return f"WR {wr:.0%} is NIET significant anders dan gemiddeld ({overall_wr:.0%}). Kan toeval zijn."
    if z > 0:
        return f"WR {wr:.0%} is SIGNIFICANT hoger dan gemiddeld ({overall_wr:.0%}). {label} is echt beter."
    return f"WR {wr:.0%} is SIGNIFICANT lager dan gemiddeld ({overall_wr:.0%}). {label} presteert echt slechter."


def _edge_stability(resolved: list) -> dict:
    """Rolling 30-bet WR/ROI windows to detect regime changes."""
    if len(resolved) < 30:
        return {"insufficient_data": True}

    # Sort by timestamp
    sorted_bets = sorted(resolved, key=lambda b: b.get("first_ts", 0))
    window = 30

    windows = []
    for i in range(0, len(sorted_bets) - window + 1, max(1, window // 3)):
        chunk = sorted_bets[i:i + window]
        wins = sum(1 for b in chunk if b["result"] == "WIN")
        cost = sum(b["cost"] for b in chunk)
        pnl = sum(b["pnl"] for b in chunk)
        ts_start = chunk[0].get("first_ts", 0)
        ts_end = chunk[-1].get("first_ts", 0)

        windows.append({
            "start_idx": i,
            "end_idx": i + window,
            "date_start": datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime("%Y-%m-%d") if ts_start else "",
            "date_end": datetime.fromtimestamp(ts_end, tz=timezone.utc).strftime("%Y-%m-%d") if ts_end else "",
            "wr": round(wins / window, 4),
            "roi": round(pnl / cost, 4) if cost > 0 else 0,
            "pnl": round(pnl, 2),
        })

    if not windows:
        return {"insufficient_data": True}

    wrs = [w["wr"] for w in windows]
    rois = [w["roi"] for w in windows]

    # Detect regime: compare recent vs mid-range (not earliest — small sample bias)
    # Use middle third as baseline, last third as recent
    third = max(1, len(windows) // 3)
    baseline_wrs = wrs[third:2*third]
    recent_wrs = wrs[-third:]
    baseline_rois = rois[third:2*third]
    recent_rois = rois[-third:]

    early_wr = statistics.mean(baseline_wrs) if baseline_wrs else 0
    recent_wr = statistics.mean(recent_wrs) if recent_wrs else 0
    early_roi = statistics.mean(baseline_rois) if baseline_rois else 0
    recent_roi = statistics.mean(recent_rois) if recent_rois else 0

    # Trend detection: 15% threshold to avoid noise
    if recent_wr < early_wr - 0.15:
        trend = "DECLINING"
        alert = True
    elif recent_wr > early_wr + 0.15:
        trend = "IMPROVING"
        alert = False
    else:
        trend = "STABLE"
        alert = False

    # Lowest point
    worst_window = min(windows, key=lambda w: w["wr"])
    best_window = max(windows, key=lambda w: w["wr"])

    return {
        "trend": trend,
        "alert": alert,
        "early_wr": round(early_wr, 4),
        "recent_wr": round(recent_wr, 4),
        "early_roi": round(early_roi, 4),
        "recent_roi": round(recent_roi, 4),
        "wr_volatility": round(statistics.stdev(wrs), 4) if len(wrs) > 1 else 0,
        "worst_window": {
            "period": f"{worst_window['date_start']} → {worst_window['date_end']}",
            "wr": worst_window["wr"],
            "roi": worst_window["roi"],
        },
        "best_window": {
            "period": f"{best_window['date_start']} → {best_window['date_end']}",
            "wr": best_window["wr"],
            "roi": best_window["roi"],
        },
        "total_windows": len(windows),
        "rolling_windows": windows[-10:],  # last 10 for chart
    }


def _actionable_rules(resolved: list) -> list:
    """Extract actionable rules from the data. Not descriptions, decisions."""
    rules = []
    total_pnl = sum(b["pnl"] for b in resolved)

    # Rule 1: Market type profitability
    by_mt = defaultdict(lambda: {"pnl": 0, "w": 0, "l": 0, "cost": 0})
    for b in resolved:
        d = by_mt[b["mt"]]
        d["pnl"] += b["pnl"]
        d["cost"] += b["cost"]
        if b["result"] == "WIN":
            d["w"] += 1
        else:
            d["l"] += 1

    for mt, d in by_mt.items():
        n = d["w"] + d["l"]
        roi = d["pnl"] / d["cost"] if d["cost"] > 0 else 0
        if n >= 20 and roi < -0.05:
            rules.append({
                "rule": f"DROP {mt.upper()}",
                "reason": f"{mt} verliest geld: ROI={roi:.0%} op {n} bets, PnL=${d['pnl']:+.0f}",
                "impact": f"Bespaart ${abs(d['pnl']):.0f}",
                "confidence": "high" if n >= 50 else "medium",
            })
        elif n >= 20 and d["w"] / n < 0.50 and roi > 0.10:
            rules.append({
                "rule": f"KEEP {mt.upper()} (ondanks lage WR)",
                "reason": f"WR={d['w']/n:.0%} maar ROI={roi:.0%} — hoge payoff compenseert",
                "impact": f"Draagt ${d['pnl']:+.0f} bij",
                "confidence": "medium",
            })

    # Rule 2: League profitability
    by_league = defaultdict(lambda: {"pnl": 0, "w": 0, "l": 0, "cost": 0})
    for b in resolved:
        d = by_league[b["league"]]
        d["pnl"] += b["pnl"]
        d["cost"] += b["cost"]
        if b["result"] == "WIN":
            d["w"] += 1
        else:
            d["l"] += 1

    for lg, d in by_league.items():
        n = d["w"] + d["l"]
        roi = d["pnl"] / d["cost"] if d["cost"] > 0 else 0
        if n >= 10 and roi < -0.10:
            rules.append({
                "rule": f"CONSIDER DROPPING {lg.upper()}",
                "reason": f"ROI={roi:.0%} op {n} bets. Negatief rendement.",
                "impact": f"Bespaart ${abs(d['pnl']):.0f}",
                "confidence": "medium" if n >= 20 else "low",
            })

    # Rule 3: Sizing confidence
    costs = sorted(b["cost"] for b in resolved)
    q75 = costs[3 * len(costs) // 4] if len(costs) >= 4 else 0
    big_bets = [b for b in resolved if b["cost"] > q75]
    small_bets = [b for b in resolved if b["cost"] <= costs[len(costs) // 4]]
    if big_bets and small_bets:
        big_wr = sum(1 for b in big_bets if b["result"] == "WIN") / len(big_bets)
        small_wr = sum(1 for b in small_bets if b["result"] == "WIN") / len(small_bets)
        if big_wr > small_wr + 0.15:
            rules.append({
                "rule": "SIZING SIGNAL BEVESTIGD",
                "reason": f"Grote bets: {big_wr:.0%} WR vs kleine: {small_wr:.0%} WR. Cannae weet wanneer hij zeker is.",
                "impact": "Tiered sizing correct geïmplementeerd",
                "confidence": "high" if len(big_bets) >= 30 else "medium",
            })

    return rules


def _risk_metrics(resolved: list) -> dict:
    """Risk metrics: max drawdown, consecutive losses, exposure concentration."""
    if not resolved:
        return {}

    sorted_bets = sorted(resolved, key=lambda b: b.get("first_ts", 0))

    # Cumulative PnL for drawdown
    cum_pnl = []
    running = 0
    for b in sorted_bets:
        running += b["pnl"]
        cum_pnl.append(running)

    # Max drawdown
    peak = cum_pnl[0]
    max_dd = 0
    for p in cum_pnl:
        peak = max(peak, p)
        dd = peak - p
        max_dd = max(max_dd, dd)

    # Consecutive losses
    max_consec_loss = 0
    current_streak = 0
    for b in sorted_bets:
        if b["result"] == "LOSS":
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0

    # Concentration: top 3 leagues % of total bets
    league_counts = defaultdict(int)
    for b in resolved:
        league_counts[b["league"]] += 1
    top3 = sorted(league_counts.values(), reverse=True)[:3]
    concentration = sum(top3) / len(resolved) if resolved else 0

    return {
        "max_drawdown": round(max_dd, 2),
        "max_consecutive_losses": max_consec_loss,
        "league_concentration_top3": round(concentration, 4),
        "total_pnl": round(cum_pnl[-1] if cum_pnl else 0, 2),
    }


def _wilson_ci(wins, total, z=1.96):
    """Wilson score 95% CI."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (max(0, center - spread), min(1, center + spread))
