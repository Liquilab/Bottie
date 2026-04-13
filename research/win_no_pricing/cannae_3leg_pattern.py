#!/usr/bin/env python3
"""
Find Cannae games where he bought ALL THREE conviction legs:
  - WIN_YES on team A
  - WIN_NO on team B (opponent) — i.e. "Will B win? No"
  - DRAW_NO

These are pure-conviction "team A wins" structures (3x payoff if A wins).
Question: what distinguishes these games from 1-leg or 2-leg games?

Looks at:
  - stake size per leg vs game total
  - entry prices
  - frequency over time
  - ROI of these 3-leg games
"""
import csv
from collections import defaultdict
from pathlib import Path

CSV = Path(__file__).resolve().parents[2] / "research/cannae_trades/cannae_closed_full.csv"


def classify_football_leg(title: str, outcome: str) -> str:
    t = (title or "").lower()
    o = (outcome or "").strip().lower()
    if "draw" in t and t.startswith("will "):
        return "DRAW_YES" if o == "yes" else "DRAW_NO"
    if t.startswith("will ") and " win" in t and o in ("yes", "no"):
        return "WIN_YES" if o == "yes" else "WIN_NO"
    return "OTHER"


def main():
    # Group by event_slug
    games = defaultdict(lambda: {"legs": [], "date": None, "title_team": {}})
    with CSV.open() as f:
        for r in csv.DictReader(f):
            slug = r.get("event_slug", "")
            if not slug:
                continue
            leg = classify_football_leg(r.get("title", ""), r.get("outcome", ""))
            if leg == "OTHER":
                continue
            try:
                price = float(r["avg_price"])
                stake = float(r["total_bought"])
                pnl = float(r["realized_pnl"])
            except (ValueError, KeyError):
                continue
            if stake <= 0:
                continue
            cid = r.get("condition_id", "")
            games[slug]["legs"].append({
                "leg": leg,
                "title": r.get("title", ""),
                "outcome": r.get("outcome", ""),
                "cid": cid,
                "price": price,
                "stake": stake,
                "pnl": pnl,
                "won": r.get("won") == "1",
            })
            games[slug]["date"] = r.get("date")

    # Filter to football games (must have at least one DRAW leg = football marker)
    football_games = {s: g for s, g in games.items()
                      if any(l["leg"].startswith("DRAW") for l in g["legs"])}

    # For each game, classify the structure based on which legs Cannae has
    structures = defaultdict(list)
    for slug, g in football_games.items():
        legs_by_type = defaultdict(list)
        for l in g["legs"]:
            legs_by_type[l["leg"]].append(l)

        has_win_yes = bool(legs_by_type["WIN_YES"])
        has_win_no = bool(legs_by_type["WIN_NO"])
        has_draw_no = bool(legs_by_type["DRAW_NO"])
        has_draw_yes = bool(legs_by_type["DRAW_YES"])

        # 3-LEG conviction structures
        if has_win_yes and has_win_no and has_draw_no:
            label = "3LEG_WIN+OPP_NO+DRAW_NO"
        elif has_win_yes and has_draw_no and not has_win_no:
            label = "2LEG_WIN_YES+DRAW_NO"
        elif has_win_no and has_draw_yes and not has_win_yes:
            label = "2LEG_WIN_NO+DRAW_YES (hedge)"
        elif has_win_yes and has_draw_yes:
            label = "MIXED_WIN_YES+DRAW_YES"
        elif has_win_no and has_draw_no and not has_win_yes:
            label = "2LEG_WIN_NO+DRAW_NO"
        elif has_win_yes and not has_draw_yes and not has_draw_no:
            label = "1LEG_WIN_YES"
        elif has_win_no and not has_draw_yes and not has_draw_no:
            label = "1LEG_WIN_NO"
        else:
            label = "OTHER"
        structures[label].append((slug, g))

    print(f"\nCannae football game structures (n={len(football_games)} football games)\n")
    print(f"{'structure':<35} {'n':>5} {'avg_total$':>11} {'win_yes_p':>10} "
          f"{'draw_no_p':>10} {'opp_no_p':>10} {'pnl':>11} {'ROI':>8}")
    print("-" * 105)

    sort_order = [
        "3LEG_WIN+OPP_NO+DRAW_NO",
        "2LEG_WIN_YES+DRAW_NO",
        "2LEG_WIN_NO+DRAW_YES (hedge)",
        "MIXED_WIN_YES+DRAW_YES",
        "2LEG_WIN_NO+DRAW_NO",
        "1LEG_WIN_YES",
        "1LEG_WIN_NO",
        "OTHER",
    ]
    for label in sort_order:
        items = structures.get(label, [])
        if not items:
            continue
        totals = []
        wy_prices = []
        dn_prices = []
        opp_no_prices = []
        total_pnl = 0.0
        total_stake = 0.0
        for slug, g in items:
            game_total = sum(l["stake"] for l in g["legs"])
            game_pnl = sum(l["pnl"] for l in g["legs"])
            totals.append(game_total)
            total_pnl += game_pnl
            total_stake += game_total
            for l in g["legs"]:
                if l["leg"] == "WIN_YES":
                    wy_prices.append(l["price"])
                elif l["leg"] == "DRAW_NO":
                    dn_prices.append(l["price"])
                elif l["leg"] == "WIN_NO":
                    opp_no_prices.append(l["price"])

        def avg(xs):
            return sum(xs) / len(xs) if xs else 0.0

        roi = total_pnl / total_stake if total_stake else 0.0
        print(f"{label:<35} {len(items):>5} ${avg(totals):>10,.0f} "
              f"{avg(wy_prices):>9.2f} {avg(dn_prices):>9.2f} "
              f"{avg(opp_no_prices):>9.2f} ${total_pnl:>10,.0f} {roi*100:>+7.1f}%")

    # Deep dive on 3LEG: what are the entry prices? When does it trigger?
    three_leg = structures.get("3LEG_WIN+OPP_NO+DRAW_NO", [])
    if three_leg:
        print(f"\n--- Deep dive 3-LEG conviction (n={len(three_leg)}) ---")
        print(f"{'date':<12} {'wy_p':>5} {'opp_no_p':>9} {'dn_p':>5} "
              f"{'wy$':>8} {'opp$':>8} {'dn$':>8} {'ratios':>20}")
        # Sample first 30
        for slug, g in sorted(three_leg, key=lambda x: x[1]["date"] or "")[-30:]:
            wy = next((l for l in g["legs"] if l["leg"] == "WIN_YES"), None)
            wn = next((l for l in g["legs"] if l["leg"] == "WIN_NO"), None)
            dn = next((l for l in g["legs"] if l["leg"] == "DRAW_NO"), None)
            if not (wy and wn and dn):
                continue
            total = wy["stake"] + wn["stake"] + dn["stake"]
            print(f"{g['date'] or '':<12} {wy['price']:>5.2f} {wn['price']:>9.2f} "
                  f"{dn['price']:>5.2f} ${wy['stake']:>7,.0f} ${wn['stake']:>7,.0f} "
                  f"${dn['stake']:>7,.0f}  "
                  f"wy={wy['stake']/total*100:>4.0f}% dn={dn['stake']/total*100:>4.0f}%")


if __name__ == "__main__":
    main()
