"""
Cannae Intelligence Engine — daily analysis of Cannae's trading behavior.

Replaces the old evolutionary wallet optimizer. Runs daily (or --once):
1. Collect Cannae data (trades, positions, events)
2. Run intelligence modules (event selection, entry prices, sizing, temporal, odds)
3. Save report + history
4. Claude curator distills rules → cannae_playbook.md
5. Check yesterday's predictions vs today's actuals

Usage:
    python3 autoresearch.py --once          # Single run
    python3 autoresearch.py                 # Continuous (daily)
    python3 autoresearch.py --odds-only     # Only collect odds (for 2x/day cron)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("intelligence")

# Add research dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "cannae_quant_analysis"))

from cannae_quant_analysis.analyze import (
    collect_cannae_data,
    analyze_by_group,
    analyze_sizing_signal,
    analyze_timing,
    analyze_leg_correlation,
    analyze_edge_decay,
    analyze_hauptbet,
    generate_alerts,
    generate_recommendations,
    wilson_ci,
    REPORT_FILE,
    HISTORY,
)
from intelligence.temporal import analyze_temporal
from intelligence.sizing_model import analyze_sizing_model
from intelligence.event_menu import analyze_event_selection
from intelligence.entry_price import analyze_entry_prices
from intelligence.odds_edge import collect_odds, analyze_odds_edge
from intelligence.first_principles import analyze_first_principles
from curator import curate_playbook, load_playbook, save_playbook
from dag import (
    load_dag,
    append_discovery,
    extract_discoveries,
    save_predictions,
    check_predictions,
)

INTEL_REPORT_PATH = Path("research/cannae_quant_analysis/intelligence_report.json")
INTEL_HISTORY_DIR = Path("research/cannae_quant_analysis/history")
PREDICTIONS_PATH = Path("data/predictions.json")


async def intelligence_cycle():
    """Run one full intelligence cycle."""
    log.info("=" * 70)
    log.info("CANNAE INTELLIGENCE ENGINE — CYCLE START")
    log.info("=" * 70)

    # --- 1. Data Collection ---
    log.info("\n--- Phase 1: Data Collection ---")
    dataset = collect_cannae_data()
    resolved = dataset["resolved"]
    event_cache = dataset["event_cache"]
    log.info(f"Dataset: {len(dataset['all_bets'])} bets ({len(resolved)} resolved, {len(dataset['open_bets'])} open)")

    # --- 2. Original Quant Analysis (preserve existing report.json) ---
    log.info("\n--- Phase 2: Quant Analysis (existing) ---")
    quant_report = _run_quant_analysis(dataset)

    # --- 3. Intelligence Modules ---
    log.info("\n--- Phase 3: Intelligence Modules ---")

    log.info("  Running temporal analysis...")
    temporal = analyze_temporal(dataset)

    log.info("  Running sizing model...")
    sizing = analyze_sizing_model(dataset)

    log.info("  Running event selection analysis...")
    event_selection = analyze_event_selection(dataset)

    log.info("  Running entry price analysis...")
    entry_prices = analyze_entry_prices(dataset)

    log.info("  Running odds edge analysis...")
    odds_edge = analyze_odds_edge(dataset)

    log.info("  Running first principles analysis...")
    first_principles = analyze_first_principles(dataset)

    # --- 4. Collect fresh odds (budget-aware) ---
    log.info("\n--- Phase 4: Odds Collection ---")
    _collect_odds_if_needed(dataset)

    # --- 5. Build full report ---
    log.info("\n--- Phase 5: Build Report ---")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "quant_analysis": quant_report,
        "event_selection": event_selection,
        "entry_prices": entry_prices,
        "sizing": sizing,
        "temporal": temporal,
        "odds_edge": odds_edge,
        "first_principles": first_principles,
    }

    # Save reports
    INTEL_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTEL_REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))
    log.info(f"Intelligence report → {INTEL_REPORT_PATH}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    INTEL_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    history_file = INTEL_HISTORY_DIR / f"{today}_intel.json"
    history_file.write_text(json.dumps(report, indent=2, default=str))
    log.info(f"History → {history_file}")

    # --- 6. Extract discoveries → DAG ---
    log.info("\n--- Phase 6: Discovery Extraction ---")
    discoveries = extract_discoveries(report)
    for d in discoveries:
        append_discovery(d)
    log.info(f"Logged {len(discoveries)} discoveries to DAG")

    # --- 7. Check yesterday's predictions ---
    log.info("\n--- Phase 7: Prediction Check ---")
    prediction_results = _check_yesterdays_predictions(dataset)

    # --- 8. Curator: distill playbook ---
    log.info("\n--- Phase 8: Strategy Curator ---")
    try:
        current_playbook = load_playbook()
        new_playbook = await curate_playbook(
            report, current_playbook, prediction_results
        )
        save_playbook(new_playbook)
        log.info("Playbook updated → data/cannae_playbook.md")

        # Extract predictions from playbook for tomorrow
        _save_curator_predictions(new_playbook)
    except Exception as e:
        log.error(f"Curator failed: {e}")

    # --- 9. Telegram Report ---
    log.info("\n--- Phase 9: Telegram Report ---")
    try:
        playbook_text = load_playbook()
        _send_telegram_report(report, playbook_text)
    except Exception as e:
        log.error(f"Telegram report failed: {e}")

    # --- Summary ---
    log.info("\n" + "=" * 70)
    log.info("CYCLE COMPLETE")
    log.info("=" * 70)
    _print_summary(report)


def _run_quant_analysis(dataset: dict) -> dict:
    """Run the original quant analysis modules and save report.json."""
    resolved = dataset["resolved"]
    event_cache = dataset["event_cache"]
    all_bets = dataset["all_bets"]

    if not resolved:
        return {"error": "no resolved bets"}

    total_wins = sum(1 for b in resolved if b["result"] == "WIN")
    total_cost = sum(b["cost"] for b in resolved)
    total_pnl = sum(b["pnl"] for b in resolved)
    overall = {
        "bets": len(resolved),
        "wins": total_wins,
        "losses": len(resolved) - total_wins,
        "wr": round(total_wins / len(resolved), 4),
        "wr_ci_95": list(wilson_ci(total_wins, len(resolved))),
        "roi": round(total_pnl / total_cost, 4) if total_cost > 0 else 0,
        "pnl": round(total_pnl, 2),
    }

    by_mt = analyze_by_group(resolved, lambda r: r["mt"])
    by_league = analyze_by_group(resolved, lambda r: r["league"])
    sizing = analyze_sizing_signal(resolved)
    timing = analyze_timing(resolved, event_cache)
    correlation = analyze_leg_correlation(resolved)
    decay = analyze_edge_decay(resolved)
    hauptbet = analyze_hauptbet(resolved)

    ts_range = sorted(b["first_ts"] for b in resolved if b["first_ts"])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "from": datetime.fromtimestamp(ts_range[0], tz=timezone.utc).strftime("%Y-%m-%d") if ts_range else "",
            "to": datetime.fromtimestamp(ts_range[-1], tz=timezone.utc).strftime("%Y-%m-%d") if ts_range else "",
        },
        "total_bets_analyzed": len(all_bets),
        "resolved_bets": len(resolved),
        "open_bets": len(dataset["open_bets"]),
        "overall": overall,
        "by_market_type": by_mt,
        "by_league": by_league,
        "sizing_signal": sizing,
        "timing": timing,
        "leg_correlation": correlation,
        "edge_decay": decay,
        "hauptbet_analysis": hauptbet,
        "alerts": generate_alerts({"overall": overall, "by_league": by_league, "timing": timing, "leg_correlation": correlation}),
        "recommendations": generate_recommendations({"overall": overall, "by_league": by_league, "timing": timing, "leg_correlation": correlation}),
    }

    # Save original report.json (backwards compatible)
    REPORT_FILE.write_text(json.dumps(report, indent=2))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (HISTORY / f"{today}.json").write_text(json.dumps(report, indent=2))

    return report


def _collect_odds_if_needed(dataset: dict):
    """Collect odds only if we haven't already today. Budget: max 5 sport keys/day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_file = Path(f"data/odds_snapshots/{today}.jsonl")

    if snapshot_file.exists() and snapshot_file.stat().st_size > 100:
        log.info("Odds already collected today — skipping")
        return

    # Determine active leagues from today's/recent bets
    from collections import Counter
    recent_leagues = Counter()
    for b in dataset["all_bets"]:
        recent_leagues[b["league"]] += 1

    # Top 5 most active leagues (budget: 5 req/day = 150/month)
    top_leagues = [league for league, _ in recent_leagues.most_common(5)]
    if not top_leagues:
        top_leagues = ["epl", "bun", "lal", "itc", "nba"]

    log.info(f"Collecting odds for: {top_leagues}")
    collect_odds(active_leagues=top_leagues)


def _check_yesterdays_predictions(dataset: dict) -> dict:
    """Load yesterday's predictions and check against today's data."""
    if not PREDICTIONS_PATH.exists():
        return {}
    try:
        all_preds = json.loads(PREDICTIONS_PATH.read_text())
        if not all_preds:
            return {}
        # Find unchecked predictions
        for batch in reversed(all_preds):
            if not batch.get("checked", False):
                result = check_predictions(batch.get("predictions", []), dataset)
                batch["checked"] = True
                batch["result"] = result
                PREDICTIONS_PATH.write_text(json.dumps(all_preds, indent=2))
                log.info(f"Prediction check: {result.get('correct', 0)}/{result.get('total_checkable', 0)} correct")
                return result
    except Exception as e:
        log.error(f"Prediction check failed: {e}")
    return {}


def _save_curator_predictions(playbook_text: str):
    """Extract predictions from curator output and save for tomorrow's check."""
    # Simple heuristic: look for lines after "VOORSPELLINGEN" or "PREDICTIONS"
    lines = playbook_text.split("\n")
    predictions = []
    in_predictions = False

    for line in lines:
        lower = line.lower().strip()
        if "voorspelling" in lower or "prediction" in lower:
            in_predictions = True
            continue
        if in_predictions and line.strip():
            predictions.append({
                "type": "curator_prediction",
                "text": line.strip(),
            })
        if in_predictions and not line.strip():
            break

    if predictions:
        save_predictions(predictions[:5])
        log.info(f"Saved {len(predictions[:5])} predictions for tomorrow")


def _print_summary(report: dict):
    """Print concise summary to stdout."""
    overall = report.get("quant_analysis", {}).get("overall", {})
    if overall:
        log.info(f"Overall: {overall.get('bets', 0)} bets, {overall.get('wr', 0):.1%} WR, {overall.get('roi', 0):.1%} ROI, ${overall.get('pnl', 0):.0f} PnL")

    es = report.get("event_selection", {})
    sr = es.get("selection_rate", {})
    if isinstance(sr, dict) and sr.get("available_events"):
        log.info(f"Event selection: {sr.get('selected_events', '?')}/{sr.get('available_events', '?')} ({sr.get('rate', 0):.1%})")

    oe = report.get("odds_edge", {})
    if oe.get("matched_trades"):
        edge = oe.get("edge_analysis", {})
        log.info(f"Odds edge: {oe['matched_trades']} matched, avg edge {edge.get('avg_edge', 0):.1%}")

    tmp = report.get("temporal", {})
    batches = tmp.get("batches", {})
    if batches.get("total_batches"):
        log.info(f"Temporal: {batches['total_batches']} batches, avg size {batches.get('avg_batch_size', 0):.1f}")


def _send_telegram_report(report: dict, playbook: str = ""):
    """Format and send intelligence report via Telegram."""
    from lib.pm_api import send_telegram

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"*CANNAE INTELLIGENCE — {today}*\n"]

    # --- Overall Performance ---
    overall = report.get("quant_analysis", {}).get("overall", {})
    if overall:
        wr = overall.get("wr", 0)
        roi = overall.get("roi", 0)
        pnl = overall.get("pnl", 0)
        ci = overall.get("wr_ci_95", [0, 0])
        lines.append(f"*P&L:* ${pnl:+.0f} | *ROI:* {roi:.0%} | *WR:* {wr:.0%} [{ci[0]:.0%}-{ci[1]:.0%}]")
        lines.append(f"Bets: {overall.get('bets', 0)} resolved, {report.get('quant_analysis', {}).get('open_bets', 0)} open\n")

    # --- Top Leagues ---
    by_league = report.get("quant_analysis", {}).get("by_league", {})
    if by_league:
        lines.append("*Top Leagues (by PnL):*")
        top = sorted(by_league.items(), key=lambda x: -x[1].get("pnl", 0))[:5]
        for league, s in top:
            lines.append(f"  {league.upper():5s} {s['bets']:3d}b {s['wr']:.0%} WR  ${s['pnl']:+.0f}")
        lines.append("")

    # --- Event Selection ---
    es = report.get("event_selection", {})
    sr = es.get("selection_rate", {})
    if isinstance(sr, dict) and sr.get("available_events"):
        lines.append(f"*Event Selection:* {sr.get('selected_events', '?')}/{sr.get('available_events', '?')} events ({sr.get('rate', 0):.1%})")
    filters = es.get("inferred_filters", [])
    for f in filters[:3]:
        lines.append(f"  Filter: {f.get('description', '')}")
    if filters:
        lines.append("")

    # --- Entry Price Edge ---
    ep = report.get("entry_prices", {})
    ie = ep.get("implied_edge", {})
    if ie:
        best_buckets = sorted(ie.items(), key=lambda x: -x[1].get("edge", 0))[:3]
        if best_buckets:
            lines.append("*Best Edge Buckets:*")
            for bucket, data in best_buckets:
                if data.get("bets", 0) >= 5:
                    lines.append(f"  {bucket}: edge={data['edge']:+.1%} ({data['bets']}b, ${data.get('pnl', 0):+.0f})")
            lines.append("")

    # --- Sizing Rules ---
    sz = report.get("sizing", {})
    rules = sz.get("decision_rules", [])
    if rules:
        lines.append("*Sizing Rules:*")
        for r in rules[:3]:
            lines.append(f"  {r.get('description', '')}")
        lines.append("")

    # --- Temporal ---
    tmp = report.get("temporal", {})
    batches = tmp.get("batches", {})
    if batches.get("total_batches"):
        lines.append(f"*Temporal:* {batches['total_batches']} batches, avg {batches.get('avg_batch_size', 0):.1f} bets, {batches.get('median_gap_hours', 0):.1f}h gap")

    # Best hour
    hod = tmp.get("hour_of_day", {})
    if hod:
        best_hour = max(hod.items(), key=lambda x: x[1].get("pnl", 0))
        worst_hour = min(hod.items(), key=lambda x: x[1].get("pnl", 0))
        lines.append(f"  Best hour: {best_hour[0]}:00 UTC (${best_hour[1].get('pnl', 0):+.0f})")
        lines.append(f"  Worst hour: {worst_hour[0]}:00 UTC (${worst_hour[1].get('pnl', 0):+.0f})")
        lines.append("")

    # --- Odds Edge ---
    oe = report.get("odds_edge", {})
    edge_data = oe.get("edge_analysis", {})
    if edge_data:
        lines.append(f"*Odds Edge:* avg {edge_data.get('avg_edge', 0):+.1%}, {edge_data.get('positive_edge_pct', 0):.0%} positive ({oe.get('matched_trades', 0)} matched)")
        ev = oe.get("edge_vs_outcome", {})
        if not ev.get("insufficient_data"):
            lines.append(f"  Winners: {ev.get('avg_edge_winners', 0):+.1%} | Losers: {ev.get('avg_edge_losers', 0):+.1%}")
        lines.append("")

    # --- First Principles ---
    fp = report.get("first_principles", {})
    if fp and not fp.get("insufficient_data"):
        # Return decomposition
        rd = fp.get("return_decomposition", {})
        by_mt = rd.get("by_market_type", {})
        if by_mt:
            lines.append("*Rendement Decompositie:*")
            for mt, d in sorted(by_mt.items(), key=lambda x: -x[1].get("pnl", 0)):
                lines.append(f"  {mt:8s} {d['pnl_contribution_pct']:+5.1f}% van PnL | WR={d['wr']:.0%} | payoff={d.get('payoff_ratio', 0):.1f}x | {d.get('verdict', '')}")
            lines.append("")

        # Actionable rules
        rules = fp.get("actionable_rules", [])
        if rules:
            lines.append("*First Principles Regels:*")
            for r in rules[:4]:
                lines.append(f"  {r['rule']}: {r['reason']}")
            lines.append("")

        # Edge stability
        es = fp.get("edge_stability", {})
        if es and not es.get("insufficient_data"):
            trend_emoji = {"DECLINING": "↘", "IMPROVING": "↗", "STABLE": "→"}.get(es.get("trend", ""), "?")
            lines.append(f"*Edge Stabiliteit:* {trend_emoji} {es['trend']} (vroeg={es['early_wr']:.0%} → recent={es['recent_wr']:.0%})")
            if es.get("alert"):
                lines.append(f"  ⚠ ALERT: edge is dalend!")
            lines.append("")

    # --- Edge Decay ---
    decay = report.get("quant_analysis", {}).get("edge_decay", {})
    if decay.get("trend"):
        lines.append(f"*Edge Trend (quant):* {decay['trend']} (1st half WR={decay.get('first_half_wr', 0):.0%} → 2nd half={decay.get('second_half_wr', 0):.0%})")

    # --- Alerts ---
    alerts = report.get("quant_analysis", {}).get("alerts", [])
    if alerts:
        lines.append("\n*ALERTS:*")
        for a in alerts:
            lines.append(f"  {a['msg']}")

    # --- Recommendations ---
    recs = report.get("quant_analysis", {}).get("recommendations", [])
    if recs:
        lines.append("\n*Recommendations:*")
        for r in recs[:3]:
            lines.append(f"  {r}")

    # --- Playbook excerpt (last 5 rules) ---
    if playbook:
        pb_lines = [l.strip() for l in playbook.strip().split("\n") if l.strip() and not l.startswith("#")]
        if pb_lines:
            lines.append("\n*Playbook (top regels):*")
            for l in pb_lines[:5]:
                lines.append(f"  {l}")

    message = "\n".join(lines)

    # Telegram has 4096 char limit — truncate if needed
    if len(message) > 4000:
        message = message[:3950] + "\n\n_...afgekapt (zie intelligence_report.json)_"

    ok = send_telegram(message)
    if ok:
        log.info("Telegram report sent")
    else:
        log.warning("Telegram report failed — check TELEGRAM_BOT_TOKEN/CHAT_ID")


async def odds_only():
    """Only collect odds — for 2x/day cron job."""
    log.info("Odds-only collection run")
    # Determine active leagues from recent intelligence report
    top_leagues = ["epl", "bun", "lal", "itc", "nba"]
    if INTEL_REPORT_PATH.exists():
        try:
            report = json.loads(INTEL_REPORT_PATH.read_text())
            by_league = report.get("quant_analysis", {}).get("by_league", {})
            if by_league:
                top_leagues = sorted(by_league.keys(), key=lambda k: -by_league[k].get("bets", 0))[:5]
        except Exception:
            pass
    collect_odds(active_leagues=top_leagues)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Cannae Intelligence Engine")
    parser.add_argument("--once", action="store_true", help="Single run")
    parser.add_argument("--odds-only", action="store_true", help="Only collect odds")
    args = parser.parse_args()

    if args.odds_only:
        await odds_only()
        return

    if args.once:
        await intelligence_cycle()
        return

    while True:
        try:
            await intelligence_cycle()
        except Exception as e:
            log.error(f"Intelligence cycle failed: {e}", exc_info=True)

        log.info("Sleeping 24 hours until next cycle...")
        await asyncio.sleep(86400)


if __name__ == "__main__":
    asyncio.run(main())
