"""
Consensus Pipeline — Portfolio Scoring (the "train.py" equivalent)

Loads bulk wallet data from prepare.py, computes overlap matrix,
and finds optimal wallet portfolios for consensus trading.

Usage:
    python research/consensus/score.py
"""

import json
import logging
import itertools
from collections import defaultdict
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("consensus_score")

BULK_PATH = Path("data/consensus_bulk.json")
RESULTS_PATH = Path("data/consensus_results.json")

# ── Hard filters ─────────────────────────────────────────────────────────────

MIN_CLOSED = 10
MAX_BOTH_SIDES = 0.20
MAX_STALE_DAYS = 4
MIN_SPORT_PCT = 0.0   # No sport filter — consensus can come from any domain
MIN_WIN_RATE = 0.50

# ── Portfolio constraints ────────────────────────────────────────────────────

MIN_PORTFOLIO_SIZE = 5
MAX_PORTFOLIO_SIZE = 8
MIN_PAIR_OVERLAP = 2       # Minimum shared events for a pair to count
MIN_OVERLAPPING_PAIRS = 2  # Portfolio must have at least N pairs with overlap


def load_bulk() -> dict:
    if not BULK_PATH.exists():
        raise FileNotFoundError(f"{BULK_PATH} not found. Run prepare.py first.")
    return json.loads(BULK_PATH.read_text())


def filter_wallets(wallets: list[dict]) -> list[dict]:
    """Apply hard filters to remove unsuitable wallets."""
    valid = []
    for w in wallets:
        reasons = []
        if w["closed_count"] < MIN_CLOSED:
            reasons.append(f"low_data({w['closed_count']})")
        if w["both_sides_ratio"] > MAX_BOTH_SIDES:
            reasons.append(f"spread_farmer({w['both_sides_ratio']:.0%})")
        if w["last_activity_days"] > MAX_STALE_DAYS:
            reasons.append(f"stale({w['last_activity_days']}d)")
        if w["sport_pct"] < MIN_SPORT_PCT:
            reasons.append(f"non_sport({w['sport_pct']:.0%})")
        if w["win_rate"] < MIN_WIN_RATE:
            reasons.append(f"low_wr({w['win_rate']:.0%})")

        if reasons:
            log.debug(f"  EXCLUDE {w['name']:20s}: {', '.join(reasons)}")
        else:
            valid.append(w)
    return valid


def compute_overlap_matrix(wallets: list[dict]) -> dict:
    """Compute multi-dimensional overlap between all wallet pairs.

    Consensus dimensions:
    1. Event-level: same eventSlug (strongest — same game/market)
    2. Domain-level: same sport/category (same expertise area)
    3. Market type: both trade moneylines, or both trade spreads
    4. Price tier: both pick favorites, or both pick underdogs
    5. Timing: trade in similar time windows

    Returns: {(addr_a, addr_b): {event overlap, domain overlap, consensus stats}}
    """
    matrix = {}

    for i in range(len(wallets)):
        for j in range(i + 1, len(wallets)):
            a = wallets[i]
            b = wallets[j]

            events_a = a.get("events", {})
            events_b = b.get("events", {})

            if not events_a or not events_b:
                continue

            # ── Dimension 1: Event overlap (same eventSlug) ──
            shared_slugs = set(events_a.keys()) & set(events_b.keys())
            same_side = 0
            consensus_wins = 0
            consensus_total = 0
            consensus_pnl = 0.0
            events_detail = []

            for slug in shared_slugs:
                ea = events_a[slug]
                eb = events_b[slug]
                outcome_a = ea.get("outcome", "")
                outcome_b = eb.get("outcome", "")
                agree = outcome_a == outcome_b

                if agree:
                    same_side += 1
                    won = ea.get("won", False) or eb.get("won", False)
                    consensus_total += 1
                    if won:
                        consensus_wins += 1
                    consensus_pnl += (ea.get("pnl", 0) + eb.get("pnl", 0)) / 2

                events_detail.append({
                    "slug": slug,
                    "title": ea.get("title", "")[:50],
                    "domain": ea.get("sport", "other"),
                    "market_type": ea.get("market_type", "other"),
                    "agree": agree,
                    "won": ea.get("won") if agree else None,
                })

            # ── Dimension 2: Domain overlap ──
            domains_a = set(e.get("sport", "other") for e in events_a.values())
            domains_b = set(e.get("sport", "other") for e in events_b.values())
            shared_domains = domains_a & domains_b
            # Count how many trades each has in shared domains
            domain_trades_a = sum(1 for e in events_a.values() if e.get("sport") in shared_domains)
            domain_trades_b = sum(1 for e in events_b.values() if e.get("sport") in shared_domains)
            domain_overlap_pct = min(
                domain_trades_a / len(events_a) if events_a else 0,
                domain_trades_b / len(events_b) if events_b else 0,
            )

            # ── Dimension 3: Market type overlap ──
            mtypes_a = set(e.get("market_type", "other") for e in events_a.values())
            mtypes_b = set(e.get("market_type", "other") for e in events_b.values())
            shared_market_types = mtypes_a & mtypes_b

            # ── Dimension 4: Price tier overlap ──
            tiers_a = [e.get("price_tier", "mid") for e in events_a.values()]
            tiers_b = [e.get("price_tier", "mid") for e in events_b.values()]
            from collections import Counter
            tier_dist_a = Counter(tiers_a)
            tier_dist_b = Counter(tiers_b)
            # Cosine similarity of price tier distributions
            all_tiers = set(tier_dist_a.keys()) | set(tier_dist_b.keys())
            dot = sum(tier_dist_a.get(t, 0) * tier_dist_b.get(t, 0) for t in all_tiers)
            mag_a = sum(v ** 2 for v in tier_dist_a.values()) ** 0.5
            mag_b = sum(v ** 2 for v in tier_dist_b.values()) ** 0.5
            price_similarity = dot / (mag_a * mag_b) if mag_a > 0 and mag_b > 0 else 0

            # ── Dimension 5: Per-domain consensus WR ──
            domain_consensus = {}
            for slug in shared_slugs:
                ea = events_a[slug]
                eb = events_b[slug]
                if ea.get("outcome") == eb.get("outcome"):
                    domain = ea.get("sport", "other")
                    if domain not in domain_consensus:
                        domain_consensus[domain] = {"wins": 0, "total": 0}
                    domain_consensus[domain]["total"] += 1
                    if ea.get("won") or eb.get("won"):
                        domain_consensus[domain]["wins"] += 1

            agreement_rate = same_side / len(shared_slugs) if shared_slugs else 0
            consensus_wr = consensus_wins / consensus_total if consensus_total > 0 else None

            # Only store pairs with SOME relationship
            if not shared_slugs and not shared_domains:
                continue

            pair_key = tuple(sorted([a["address"], b["address"]]))
            matrix[pair_key] = {
                "addr_a": a["address"],
                "name_a": a["name"],
                "addr_b": b["address"],
                "name_b": b["name"],
                # Event-level consensus
                "shared_events": len(shared_slugs),
                "same_side": same_side,
                "agreement_rate": round(agreement_rate, 3),
                "consensus_wr": round(consensus_wr, 3) if consensus_wr is not None else None,
                "consensus_total": consensus_total,
                "consensus_wins": consensus_wins,
                "consensus_pnl": round(consensus_pnl, 2),
                # Domain-level overlap
                "shared_domains": list(shared_domains),
                "domain_overlap_pct": round(domain_overlap_pct, 3),
                # Market type overlap
                "shared_market_types": list(shared_market_types),
                # Price behavior similarity
                "price_similarity": round(price_similarity, 3),
                # Per-domain consensus breakdown
                "domain_consensus": {
                    k: {"wr": round(v["wins"] / v["total"], 2) if v["total"] > 0 else 0, **v}
                    for k, v in domain_consensus.items()
                },
                "events_detail": events_detail,
            }

    return matrix


def score_pair(pair_data: dict) -> float:
    """Score a wallet pair by multi-dimensional consensus quality.

    Dimensions weighted:
    - Event consensus (60%): same event, same side, wins
    - Domain overlap (20%): they operate in the same domains
    - Price behavior (10%): they pick similar price tiers
    - Market type (10%): they trade same market types
    """
    import math

    shared = pair_data["shared_events"]
    agreement = pair_data["agreement_rate"]
    cons_wr = pair_data.get("consensus_wr")
    cons_total = pair_data.get("consensus_total", 0)
    domain_overlap = pair_data.get("domain_overlap_pct", 0)
    price_sim = pair_data.get("price_similarity", 0)
    shared_mtypes = len(pair_data.get("shared_market_types", []))

    # Event consensus score (0-100)
    if shared >= MIN_PAIR_OVERLAP and cons_wr is not None and cons_total >= 2:
        data_bonus = math.log2(max(cons_total, 1) + 1)
        event_score = shared * agreement * cons_wr * data_bonus
    elif shared >= 1:
        # Some overlap but not enough consensus data yet — partial credit
        event_score = shared * agreement * 0.5
    else:
        event_score = 0

    # Domain overlap score (0-10)
    domain_score = domain_overlap * 10

    # Price similarity score (0-5)
    price_score = price_sim * 5

    # Market type overlap score (0-5)
    mtype_score = min(shared_mtypes, 3) * 1.67

    # Weighted total
    total = event_score * 0.60 + domain_score * 0.20 + price_score * 0.10 + mtype_score * 0.10

    return total


def score_portfolio(portfolio_addrs: set, pair_scores: dict, wallet_map: dict) -> dict:
    """Score a portfolio of wallet addresses."""
    # Sum pair scores for all pairs in portfolio
    total_pair_score = 0.0
    active_pairs = 0
    total_consensus_events = 0
    total_consensus_wins = 0
    pair_details = []

    for pair_key, pair_data in pair_scores.items():
        if pair_key[0] in portfolio_addrs and pair_key[1] in portfolio_addrs:
            ps = score_pair(pair_data)
            if ps > 0:
                active_pairs += 1
                total_pair_score += ps
                total_consensus_events += pair_data.get("consensus_total", 0)
                total_consensus_wins += pair_data.get("consensus_wins", 0)
                pair_details.append({
                    "pair": f"{pair_data['name_a']} × {pair_data['name_b']}",
                    "score": round(ps, 2),
                    "shared": pair_data["shared_events"],
                    "agreement": pair_data["agreement_rate"],
                    "consensus_wr": pair_data.get("consensus_wr"),
                })

    # Penalty: too few overlapping pairs
    if active_pairs < MIN_OVERLAPPING_PAIRS:
        total_pair_score *= 0.3

    # Individual quality bonus
    individual_bonus = 0.0
    for addr in portfolio_addrs:
        w = wallet_map.get(addr)
        if w:
            # Bonus for high WR and sharpe
            individual_bonus += w["win_rate"] * 5 + max(w["sharpe"], 0) * 3

    # Sport diversity: penalize if all wallets have same top sport
    sports = [wallet_map[a]["top_sport"] for a in portfolio_addrs if a in wallet_map]
    from collections import Counter
    sport_counts = Counter(sports)
    max_sport_share = max(sport_counts.values()) / len(sports) if sports else 1
    diversity_mult = 1.0 if max_sport_share <= 0.5 else (1.0 - (max_sport_share - 0.5))

    consensus_wr = total_consensus_wins / total_consensus_events if total_consensus_events > 0 else 0

    final_score = (total_pair_score + individual_bonus) * diversity_mult

    return {
        "score": round(final_score, 2),
        "pair_score": round(total_pair_score, 2),
        "individual_bonus": round(individual_bonus, 2),
        "diversity_mult": round(diversity_mult, 2),
        "active_pairs": active_pairs,
        "consensus_events": total_consensus_events,
        "consensus_wins": total_consensus_wins,
        "consensus_wr": round(consensus_wr, 3) if total_consensus_events > 0 else None,
        "size": len(portfolio_addrs),
        "pair_details": sorted(pair_details, key=lambda x: -x["score"]),
    }


def find_best_portfolios(valid_wallets: list[dict], pair_scores: dict, top_n: int = 10) -> list[dict]:
    """Find the best portfolios using indexed greedy search.

    Optimization: pre-compute per-wallet adjacency list with pair scores
    so we don't iterate over all 84K pairs each evaluation.
    """
    wallet_map = {w["address"]: w for w in valid_wallets}
    addrs = set(w["address"] for w in valid_wallets)

    # Build adjacency index: addr -> [(other_addr, pair_key, pair_score)]
    adj = defaultdict(list)
    scored_pairs = []
    for pair_key, pair_data in pair_scores.items():
        ps = score_pair(pair_data)
        if ps <= 0:
            continue
        a, b = pair_key
        if a not in addrs or b not in addrs:
            continue
        scored_pairs.append((pair_key, pair_data, ps))
        adj[a].append((b, pair_key, ps))
        adj[b].append((a, pair_key, ps))

    scored_pairs.sort(key=lambda x: -x[2])
    log.info(f"Indexed {len(scored_pairs)} positive pairs across {len(adj)} wallets")

    def fast_score_portfolio(portfolio_set):
        """Score using adjacency index — O(portfolio_size × avg_degree) instead of O(all_pairs)."""
        total_pair_score = 0.0
        active_pairs = 0
        cons_events = 0
        cons_wins = 0
        pair_details = []

        seen_pairs = set()
        for addr in portfolio_set:
            for other, pair_key, ps in adj.get(addr, []):
                if other in portfolio_set and pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    total_pair_score += ps
                    active_pairs += 1
                    pd = pair_scores[pair_key]
                    cons_events += pd.get("consensus_total", 0)
                    cons_wins += pd.get("consensus_wins", 0)
                    pair_details.append({
                        "pair": f"{pd['name_a']} × {pd['name_b']}",
                        "score": round(ps, 2),
                        "shared": pd["shared_events"],
                        "agreement": pd["agreement_rate"],
                        "consensus_wr": pd.get("consensus_wr"),
                    })

        if active_pairs < MIN_OVERLAPPING_PAIRS:
            total_pair_score *= 0.3

        individual_bonus = sum(
            wallet_map[a]["win_rate"] * 5 + max(wallet_map[a]["sharpe"], 0) * 3
            for a in portfolio_set if a in wallet_map
        )

        sports = [wallet_map[a]["top_sport"] for a in portfolio_set if a in wallet_map]
        from collections import Counter
        sport_counts = Counter(sports)
        max_sport_share = max(sport_counts.values()) / len(sports) if sports else 1
        diversity_mult = 1.0 if max_sport_share <= 0.5 else (1.0 - (max_sport_share - 0.5))

        consensus_wr = cons_wins / cons_events if cons_events > 0 else 0
        final_score = (total_pair_score + individual_bonus) * diversity_mult

        return {
            "score": round(final_score, 2),
            "pair_score": round(total_pair_score, 2),
            "individual_bonus": round(individual_bonus, 2),
            "diversity_mult": round(diversity_mult, 2),
            "active_pairs": active_pairs,
            "consensus_events": cons_events,
            "consensus_wins": cons_wins,
            "consensus_wr": round(consensus_wr, 3) if cons_events > 0 else None,
            "size": len(portfolio_set),
            "pair_details": sorted(pair_details, key=lambda x: -x["score"])[:10],
        }

    best_portfolios = []

    # Strategy 1: Greedy build from top pairs
    top_pairs = scored_pairs[:20]
    log.info(f"Building portfolios from top {len(top_pairs)} pairs...")

    for pair_key, pair_data, ps in top_pairs[:10]:
        portfolio = set(pair_key)

        # Greedy: only consider wallets that have overlap with current portfolio
        for _ in range(MAX_PORTFOLIO_SIZE - 2):
            best_add = None
            best_add_score = -1

            # Only test wallets adjacent to current portfolio
            candidates = set()
            for addr in portfolio:
                for other, _, _ in adj.get(addr, []):
                    if other not in portfolio:
                        candidates.add(other)

            for candidate in candidates:
                test = portfolio | {candidate}
                result = fast_score_portfolio(test)
                if result["score"] > best_add_score:
                    best_add_score = result["score"]
                    best_add = candidate

            if best_add:
                portfolio.add(best_add)
            else:
                break

        result = fast_score_portfolio(portfolio)
        result["wallets"] = [
            {"address": a, "name": wallet_map[a]["name"], "wr": wallet_map[a]["win_rate"],
             "sport": wallet_map[a]["top_sport"], "hhi": wallet_map[a]["hhi"]}
            for a in portfolio if a in wallet_map
        ]
        best_portfolios.append(result)
        log.info(f"  Portfolio from {pair_data['name_a']}×{pair_data['name_b']}: score={result['score']}, {result['active_pairs']} pairs, cons_wr={result.get('consensus_wr', 0):.0%}")

    # Strategy 2: Start from individually best wallets with most connections
    top_connected = sorted(adj.keys(), key=lambda a: sum(ps for _, _, ps in adj[a]), reverse=True)[:5]
    for seed in top_connected:
        if seed not in wallet_map:
            continue
        portfolio = {seed}
        for _ in range(MAX_PORTFOLIO_SIZE - 1):
            candidates = set()
            for addr in portfolio:
                for other, _, _ in adj.get(addr, []):
                    if other not in portfolio:
                        candidates.add(other)
            best_add = None
            best_add_score = -1
            for candidate in candidates:
                test = portfolio | {candidate}
                result = fast_score_portfolio(test)
                if result["score"] > best_add_score:
                    best_add_score = result["score"]
                    best_add = candidate
            if best_add:
                portfolio.add(best_add)
            else:
                break

        result = fast_score_portfolio(portfolio)
        result["wallets"] = [
            {"address": a, "name": wallet_map[a]["name"], "wr": wallet_map[a]["win_rate"],
             "sport": wallet_map[a]["top_sport"], "hhi": wallet_map[a]["hhi"]}
            for a in portfolio if a in wallet_map
        ]
        best_portfolios.append(result)
        log.info(f"  Portfolio from {wallet_map[seed]['name']}: score={result['score']}")

    # Deduplicate and sort
    seen = set()
    unique_portfolios = []
    for p in sorted(best_portfolios, key=lambda x: -x["score"]):
        key = frozenset(w["address"] for w in p["wallets"])
        if key not in seen:
            seen.add(key)
            unique_portfolios.append(p)

    return unique_portfolios[:top_n]


def main():
    log.info("=" * 60)
    log.info("CONSENSUS SCORE — Portfolio optimization")
    log.info("=" * 60)

    bulk = load_bulk()
    wallets = bulk["wallets"]
    log.info(f"Loaded {len(wallets)} wallets from {bulk['timestamp']}")

    # Filter
    valid = filter_wallets(wallets)
    log.info(f"After filters: {len(valid)} valid wallets")

    if len(valid) < 2:
        log.error("Not enough valid wallets for consensus analysis")
        return

    # Compute overlap matrix
    log.info("Computing overlap matrix...")
    matrix = compute_overlap_matrix(valid)
    log.info(f"Pairs with any overlap: {len(matrix)}")

    pairs_with_score = sum(1 for v in matrix.values() if score_pair(v) > 0)
    log.info(f"Pairs with positive consensus score: {pairs_with_score}")

    # Show top pairs
    log.info("\n=== TOP 15 WALLET PAIRS ===")
    ranked_pairs = sorted(matrix.items(), key=lambda x: -score_pair(x[1]))
    for i, (pair_key, pair_data) in enumerate(ranked_pairs[:15]):
        ps = score_pair(pair_data)
        if ps <= 0:
            break
        log.info(
            f"  {i+1:2d}. {pair_data['name_a']:18s} × {pair_data['name_b']:18s} | "
            f"shared={pair_data['shared_events']:2d} agree={pair_data['agreement_rate']:.0%} "
            f"cons_wr={pair_data.get('consensus_wr', 0):.0%} "
            f"({pair_data['consensus_wins']}/{pair_data['consensus_total']}) "
            f"score={ps:.1f}"
        )

    # Find best portfolios
    log.info("\n=== PORTFOLIO SEARCH ===")
    pair_scores = {k: v for k, v in matrix.items()}
    best = find_best_portfolios(valid, pair_scores, top_n=5)

    log.info(f"\n=== TOP {len(best)} PORTFOLIOS ===")
    for i, p in enumerate(best):
        log.info(f"\n--- Portfolio {i+1} (score={p['score']}) ---")
        log.info(f"  Size: {p['size']} wallets | Active pairs: {p['active_pairs']}")
        log.info(f"  Consensus: {p['consensus_wins']}/{p['consensus_events']} = "
                 f"{p.get('consensus_wr', 0):.0%} WR")
        log.info(f"  Pair score: {p['pair_score']} | Individual: {p['individual_bonus']} | "
                 f"Diversity: {p['diversity_mult']}")
        log.info("  Wallets:")
        for w in p["wallets"]:
            log.info(f"    {w['name']:20s} | WR={w['wr']:.0%} | {w['sport']:8s} | HHI={w['hhi']:.2f}")
        if p["pair_details"]:
            log.info("  Key pairs:")
            for pd in p["pair_details"][:5]:
                log.info(f"    {pd['pair']:40s} | shared={pd['shared']} agree={pd['agreement']:.0%} "
                         f"cons_wr={pd.get('consensus_wr', 0):.0%} score={pd['score']}")

    # Save results
    output = {
        "timestamp": bulk["timestamp"],
        "valid_wallets": len(valid),
        "pairs_analyzed": len(matrix),
        "top_pairs": [
            {
                "names": f"{v['name_a']} × {v['name_b']}",
                "addresses": list(k),
                "shared_events": v["shared_events"],
                "agreement_rate": v["agreement_rate"],
                "consensus_wr": v.get("consensus_wr"),
                "consensus_total": v["consensus_total"],
                "score": round(score_pair(v), 2),
            }
            for k, v in ranked_pairs[:20]
            if score_pair(v) > 0
        ],
        "portfolios": best,
        "individual_ranking": [
            {
                "address": w["address"],
                "name": w["name"],
                "win_rate": w["win_rate"],
                "sharpe": w["sharpe"],
                "sport_pct": w["sport_pct"],
                "top_sport": w["top_sport"],
                "hhi": w["hhi"],
                "closed_count": w["closed_count"],
                "both_sides": w["both_sides_ratio"],
            }
            for w in sorted(valid, key=lambda x: x["win_rate"] * max(x["sharpe"], 0.01), reverse=True)[:20]
        ],
    }

    RESULTS_PATH.write_text(json.dumps(output, indent=2))
    log.info(f"\nResults saved to {RESULTS_PATH}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
