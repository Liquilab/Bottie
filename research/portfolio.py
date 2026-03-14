"""Portfolio data structures and evolutionary mutation logic."""

import copy
import random
from dataclasses import dataclass, field


@dataclass
class WalletSlot:
    address: str
    name: str
    weight: float
    score: float = 0.0
    win_rate: float = 0.0
    sharpe: float = 0.0
    sport_pct: float = 0.0
    closed_positions: int = 0


@dataclass
class Portfolio:
    wallets: list[WalletSlot] = field(default_factory=list)
    mutation_type: str = "original"
    fitness: float = 0.0


def portfolio_from_config(watchlist: list[dict], scout_data: dict) -> Portfolio:
    """Build a Portfolio from config watchlist, enriched with scout scores."""
    scores_by_addr = {}
    for entry in scout_data.get("current_wallet_scores", []):
        addr = (entry.get("address") or "").lower()
        if addr:
            scores_by_addr[addr] = entry

    for entry in scout_data.get("top_new_candidates", []):
        addr = (entry.get("address") or "").lower()
        if addr and addr not in scores_by_addr:
            scores_by_addr[addr] = entry

    slots = []
    for w in watchlist:
        addr = w["address"].lower()
        s = scores_by_addr.get(addr, {})
        slots.append(WalletSlot(
            address=addr,
            name=w.get("name", addr[:10]),
            weight=w.get("weight", 0.5),
            score=s.get("score", 0),
            win_rate=s.get("win_rate", 0),
            sharpe=s.get("sharpe", 0),
            sport_pct=s.get("sport_pct", 0),
            closed_positions=s.get("closed_positions", 0),
        ))
    return Portfolio(wallets=slots)


def available_candidates(scout_report: dict, current_addresses: set) -> list[WalletSlot]:
    """All wallets from scout that are NOT in the current portfolio and have score > 0."""
    candidates = []
    for entry in scout_report.get("top_new_candidates", []) + scout_report.get("recommended_additions", []):
        addr = (entry.get("address") or "").lower()
        if addr and addr not in current_addresses and entry.get("score", 0) > 0:
            candidates.append(WalletSlot(
                address=addr,
                name=entry.get("name", addr[:10]),
                weight=0.5,
                score=entry.get("score", 0),
                win_rate=entry.get("win_rate", 0),
                sharpe=entry.get("sharpe", 0),
                sport_pct=entry.get("sport_pct", 0),
                closed_positions=entry.get("closed_positions", 0),
            ))
    # Deduplicate
    seen = set()
    unique = []
    for c in candidates:
        if c.address not in seen:
            seen.add(c.address)
            unique.append(c)
    return sorted(unique, key=lambda w: w.score, reverse=True)


def mutate_swap(portfolio: Portfolio, candidates: list[WalletSlot]) -> Portfolio:
    """Replace the weakest wallet with a random candidate."""
    if not candidates or len(portfolio.wallets) < 2:
        return mutate_reweight(portfolio)

    p = Portfolio(wallets=copy.deepcopy(portfolio.wallets), mutation_type="swap")
    # Find weakest
    weakest_idx = min(range(len(p.wallets)), key=lambda i: p.wallets[i].score)
    # Pick random candidate from top 10
    candidate = random.choice(candidates[:min(10, len(candidates))])
    candidate_copy = copy.deepcopy(candidate)
    candidate_copy.weight = max(0.3, min(1.0, candidate_copy.score / 100))
    p.wallets[weakest_idx] = candidate_copy
    return p


def mutate_reweight(portfolio: Portfolio) -> Portfolio:
    """Randomly adjust 1-3 wallet weights."""
    p = Portfolio(wallets=copy.deepcopy(portfolio.wallets), mutation_type="reweight")
    n_changes = random.randint(1, min(3, len(p.wallets)))
    for _ in range(n_changes):
        idx = random.randint(0, len(p.wallets) - 1)
        delta = random.uniform(-0.3, 0.3)
        p.wallets[idx].weight = round(max(0.05, min(1.0, p.wallets[idx].weight + delta)), 2)
    return p


def mutate_prune(portfolio: Portfolio) -> Portfolio:
    """Remove 1-2 lowest-scoring wallets."""
    if len(portfolio.wallets) <= 3:
        return mutate_reweight(portfolio)

    p = Portfolio(wallets=copy.deepcopy(portfolio.wallets), mutation_type="prune")
    n_remove = random.randint(1, min(2, len(p.wallets) - 3))
    # Sort by score, remove worst
    p.wallets.sort(key=lambda w: w.score)
    p.wallets = p.wallets[n_remove:]
    return p


def mutate_expand(portfolio: Portfolio, candidates: list[WalletSlot], max_wallets: int = 15) -> Portfolio:
    """Add 1-2 wallets from candidates."""
    if not candidates or len(portfolio.wallets) >= max_wallets:
        return mutate_reweight(portfolio)

    p = Portfolio(wallets=copy.deepcopy(portfolio.wallets), mutation_type="expand")
    current_addrs = {w.address for w in p.wallets}
    available = [c for c in candidates if c.address not in current_addrs]
    if not available:
        return mutate_reweight(portfolio)

    n_add = random.randint(1, min(2, max_wallets - len(p.wallets), len(available)))
    for c in random.sample(available[:10], min(n_add, len(available[:10]))):
        slot = copy.deepcopy(c)
        slot.weight = max(0.3, min(0.8, slot.score / 100))
        p.wallets.append(slot)
    return p


def mutate_random(all_wallets: list[WalletSlot], min_size: int = 3, max_size: int = 10) -> Portfolio:
    """Completely random subset with random weights."""
    if len(all_wallets) < min_size:
        return Portfolio(wallets=copy.deepcopy(all_wallets), mutation_type="random")

    n = random.randint(min_size, min(max_size, len(all_wallets)))
    selected = random.sample(all_wallets, n)
    p = Portfolio(mutation_type="random")
    for w in selected:
        slot = copy.deepcopy(w)
        slot.weight = round(random.uniform(0.2, 1.0), 2)
        p.wallets.append(slot)
    return p


def generate_mutations(
    current: Portfolio,
    candidates: list[WalletSlot],
    mode: str = "normal",
    n: int = 30,
) -> list[Portfolio]:
    """Generate n mutations of the current portfolio."""
    max_wallets = {"conservative": 5, "normal": 10, "aggressive": 15}.get(mode, 10)

    # All known wallets for random mutations
    all_wallets = list({w.address: w for w in current.wallets + candidates}.values())

    mutations = []
    # Distribution: 8 swap, 8 reweight, 5 prune, 5 expand, 4 random
    for _ in range(8):
        mutations.append(mutate_swap(current, candidates))
    for _ in range(8):
        mutations.append(mutate_reweight(current))
    for _ in range(5):
        mutations.append(mutate_prune(current))
    for _ in range(5):
        mutations.append(mutate_expand(current, candidates, max_wallets))
    for _ in range(4):
        mutations.append(mutate_random(all_wallets, min_size=3, max_size=max_wallets))

    return mutations[:n]
