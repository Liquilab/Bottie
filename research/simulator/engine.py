#!/usr/bin/env python3
"""
Cannae Strategy Simulator — backtest any leg combination on historical games.

Usage:
    from simulator.engine import Simulator
    sim = Simulator()
    sim.load_data()

    # NO-NO-NO strategy: $5 per leg
    results = sim.run(
        legs=[
            {"side": "NO", "market": "team_a_win", "usd": 5},
            {"side": "NO", "market": "team_b_win", "usd": 5},
            {"side": "NO", "market": "draw", "usd": 5},
        ]
    )

    # Cannae-style proportional: conviction × 8%
    results = sim.run(
        legs=[
            {"side": "BEST", "market": "team_a_win", "usd": "proportional"},
            {"side": "BEST", "market": "team_b_win", "usd": "proportional"},
            {"side": "BEST", "market": "draw", "usd": "proportional"},
        ],
        bankroll=1410,
        max_pct=0.08,
    )

    # Win-only
    results = sim.run(
        legs=[
            {"side": "BEST", "market": "team_a_win", "usd": "proportional"},
            {"side": "BEST", "market": "team_b_win", "usd": "proportional"},
        ],
        bankroll=1410,
        max_pct=0.08,
    )

    sim.report(results)
    sim.compare([results1, results2, results3])
"""

import csv
import json
import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("simulator")

BASE = Path(__file__).parent.parent
CLOSED_CSV = BASE / "cannae_trades" / "cannae_closed_full.csv"
GAMES_CSV = BASE / "cannae_trades" / "cannae_full_games.csv"


@dataclass
class Market:
    """One market within a game (e.g., 'Will Team A win?')"""
    condition_id: str
    title: str
    market_type: str  # win, draw, ou, btts, spread
    outcome: str      # Yes, No, Over, Under
    avg_price: float  # entry price (NO price = 1 - YES price)
    yes_price: float  # YES price at entry
    no_price: float   # NO price at entry
    total_bought: float  # Cannae's $ on this market
    won: bool
    side: str  # which side Cannae took (largest position)


@dataclass
class Game:
    """A single sports game with all its markets."""
    slug: str
    league: str
    date: str
    markets: list  # List[Market]
    total_usdc: float
    result: str  # team_a, team_b, draw, unknown

    @property
    def team_a_win(self) -> Optional[Market]:
        wins = [m for m in self.markets if m.market_type == "win"]
        if len(wins) >= 1:
            return wins[0]
        return None

    @property
    def team_b_win(self) -> Optional[Market]:
        wins = [m for m in self.markets if m.market_type == "win"]
        if len(wins) >= 2:
            return wins[1]
        return None

    @property
    def draw_market(self) -> Optional[Market]:
        draws = [m for m in self.markets if m.market_type == "draw"]
        return draws[0] if draws else None

    @property
    def ou_markets(self) -> list:
        return [m for m in self.markets if m.market_type == "ou"]

    @property
    def spread_markets(self) -> list:
        return [m for m in self.markets if m.market_type == "spread"]

    def get_market(self, market_type: str) -> Optional[Market]:
        """Get first market of given type."""
        for m in self.markets:
            if m.market_type == market_type:
                return m
        return None

    def get_markets(self, market_type: str) -> list:
        """Get all markets of given type."""
        return [m for m in self.markets if m.market_type == market_type]


@dataclass
class LegConfig:
    """Configuration for one leg of a strategy."""
    side: str          # "YES", "NO", "BEST" (copy Cannae's largest side)
    market: str        # "team_a_win", "team_b_win", "draw", "ou", "spread"
    usd: any           # float (fixed $) or "proportional"
    min_usd: float = 2.50
    max_price: float = 0.95
    min_price: float = 0.05


@dataclass
class TradeResult:
    """Result of one simulated trade."""
    game_slug: str
    leg_desc: str
    side: str
    price: float
    cost: float
    shares: float
    payout: float
    pnl: float
    won: bool


@dataclass
class SimResult:
    """Result of a full simulation run."""
    name: str
    legs_config: list
    trades: list  # List[TradeResult]
    games_played: int
    games_skipped: int
    total_invested: float
    total_pnl: float
    roi: float
    wr: float
    avg_pnl_per_game: float
    by_league: dict
    by_price_bucket: dict
    pnl_curve: list  # cumulative PnL


class Simulator:
    """Main simulation engine."""

    def __init__(self):
        self.games: list[Game] = []

    def load_data(self, sport_filter: str = "soccer", min_markets: int = 2):
        """Load historical games from Cannae's closed positions CSV."""
        log.info("Loading data from %s", CLOSED_CSV)

        # Group closed positions by event_slug
        by_event = defaultdict(list)
        with open(CLOSED_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                slug = row.get("event_slug", "").strip()
                if not slug:
                    continue
                by_event[slug].append(row)

        log.info("Found %d unique events", len(by_event))

        # Soccer league prefixes
        soccer_prefixes = {
            "epl", "bun", "lal", "fl1", "uel", "uef", "arg", "mls",
            "rou1", "efa", "por", "bra", "itc", "ere", "es2", "bl2",
            "sea", "elc", "mex", "fr2", "aus", "spl", "efl", "tur",
            "ucl", "cde", "cdr", "acn", "nfl",
        }
        nba_nhl = {"nba", "nhl", "mlb"}

        def is_sport(slug):
            prefix = slug.split("-")[0]
            if sport_filter == "soccer":
                return prefix in soccer_prefixes
            elif sport_filter == "us":
                return prefix in nba_nhl
            else:
                return True

        def detect_mt(title):
            tl = title.lower()
            if "o/u" in tl or "over/under" in tl:
                return "ou"
            elif "spread" in tl:
                return "spread"
            elif "draw" in tl:
                return "draw"
            elif "both teams" in tl or "btts" in tl:
                return "btts"
            else:
                return "win"

        games = []
        for slug, positions in by_event.items():
            if not is_sport(slug):
                continue

            league = slug.split("-")[0]
            date = ""

            # Group by conditionId
            by_cid = defaultdict(list)
            for p in positions:
                cid = p.get("condition_id", "")
                if cid:
                    by_cid[cid].append(p)

            markets = []
            for cid, cid_positions in by_cid.items():
                # Best position (largest total_bought)
                sorted_pos = sorted(
                    cid_positions,
                    key=lambda x: float(x.get("total_bought", 0) or 0),
                    reverse=True,
                )
                best = sorted_pos[0]
                title = best.get("title", "")
                mt = detect_mt(title)
                outcome = best.get("outcome", "")
                avg_price = float(best.get("avg_price", 0) or 0)
                total_bought = float(best.get("total_bought", 0) or 0)
                won = best.get("won", "").lower() in ("true", "1", "yes")
                date = best.get("date", date) or date

                # Compute YES/NO prices
                # If outcome is "Yes" or team name, avg_price = YES price
                # If outcome starts with "No", avg_price = NO price
                outcome_lower = outcome.lower()
                if outcome_lower.startswith("no") or outcome_lower == "under":
                    no_price = avg_price
                    yes_price = 1 - avg_price
                else:
                    yes_price = avg_price
                    no_price = 1 - avg_price

                markets.append(Market(
                    condition_id=cid,
                    title=title,
                    market_type=mt,
                    outcome=outcome,
                    avg_price=avg_price,
                    yes_price=yes_price,
                    no_price=no_price,
                    total_bought=total_bought,
                    won=won,
                    side="NO" if outcome_lower.startswith("no") or outcome_lower == "under" else "YES",
                ))

            if len(markets) < min_markets:
                continue

            # Sort markets: win first (by total_bought desc), then draw, then rest
            mt_order = {"win": 0, "draw": 1, "ou": 2, "spread": 3, "btts": 4}
            markets.sort(key=lambda m: (mt_order.get(m.market_type, 9), -m.total_bought))

            total_usdc = sum(m.total_bought for m in markets)

            games.append(Game(
                slug=slug,
                league=league,
                date=date,
                markets=markets,
                total_usdc=total_usdc,
                result="unknown",
            ))

        # Sort by date
        games.sort(key=lambda g: g.date)
        self.games = games
        log.info("Loaded %d %s games with %d+ markets", len(games), sport_filter, min_markets)

    def run(
        self,
        legs: list[dict],
        name: str = "unnamed",
        bankroll: float = 10000,
        max_pct: float = 0.08,
        n_games: int = 1000,
        compounding: bool = False,
        min_game_total: float = 0,
    ) -> SimResult:
        """Run a simulation with given leg configuration.

        Args:
            legs: list of leg configs, e.g.:
                [{"side": "NO", "market": "team_a_win", "usd": 5}]
                [{"side": "BEST", "market": "team_a_win", "usd": "proportional"}]
            name: strategy name for reporting
            bankroll: starting bankroll (for proportional sizing)
            max_pct: max % per leg (for proportional sizing)
            n_games: number of games to simulate
            compounding: if True, bankroll grows/shrinks with PnL
            min_game_total: minimum Cannae game total to include
        """
        leg_configs = [LegConfig(**l) if isinstance(l, dict) else l for l in legs]

        current_bankroll = bankroll
        trades = []
        games_played = 0
        games_skipped = 0
        pnl_curve = [0]
        by_league = defaultdict(lambda: {"pnl": 0, "cost": 0, "wins": 0, "losses": 0})
        by_price = defaultdict(lambda: {"pnl": 0, "cost": 0, "wins": 0, "losses": 0})

        for game in self.games[:n_games + 500]:  # extra buffer for skips
            if games_played >= n_games:
                break

            if game.total_usdc < min_game_total:
                games_skipped += 1
                continue

            game_trades = self._execute_game(game, leg_configs, current_bankroll, max_pct)

            if not game_trades:
                games_skipped += 1
                continue

            games_played += 1
            game_pnl = sum(t.pnl for t in game_trades)

            if compounding:
                current_bankroll += game_pnl

            trades.extend(game_trades)
            pnl_curve.append(pnl_curve[-1] + game_pnl)

            # Track by league
            lg = by_league[game.league]
            lg["pnl"] += game_pnl
            lg["cost"] += sum(t.cost for t in game_trades)
            lg["wins"] += sum(1 for t in game_trades if t.won)
            lg["losses"] += sum(1 for t in game_trades if not t.won)

            # Track by price bucket
            for t in game_trades:
                bucket = _price_bucket(t.price)
                bp = by_price[bucket]
                bp["pnl"] += t.pnl
                bp["cost"] += t.cost
                bp["wins"] += 1 if t.won else 0
                bp["losses"] += 0 if t.won else 1

        total_invested = sum(t.cost for t in trades)
        total_pnl = sum(t.pnl for t in trades)
        wins = sum(1 for t in trades if t.won)

        # Compute league stats
        league_stats = {}
        for lg, d in sorted(by_league.items(), key=lambda x: -x[1]["pnl"]):
            n = d["wins"] + d["losses"]
            if n == 0:
                continue
            league_stats[lg] = {
                "games": n,
                "pnl": round(d["pnl"], 2),
                "roi": round(d["pnl"] / d["cost"], 4) if d["cost"] > 0 else 0,
                "wr": round(d["wins"] / n, 4),
            }

        price_stats = {}
        for bucket, d in sorted(by_price.items()):
            n = d["wins"] + d["losses"]
            if n == 0:
                continue
            price_stats[bucket] = {
                "trades": n,
                "pnl": round(d["pnl"], 2),
                "roi": round(d["pnl"] / d["cost"], 4) if d["cost"] > 0 else 0,
                "wr": round(d["wins"] / n, 4),
            }

        return SimResult(
            name=name,
            legs_config=[l.__dict__ for l in leg_configs],
            trades=trades,
            games_played=games_played,
            games_skipped=games_skipped,
            total_invested=round(total_invested, 2),
            total_pnl=round(total_pnl, 2),
            roi=round(total_pnl / total_invested, 4) if total_invested > 0 else 0,
            wr=round(wins / len(trades), 4) if trades else 0,
            avg_pnl_per_game=round(total_pnl / games_played, 2) if games_played > 0 else 0,
            by_league=league_stats,
            by_price_bucket=price_stats,
            pnl_curve=pnl_curve,
        )

    def _execute_game(self, game: Game, legs: list[LegConfig], bankroll: float, max_pct: float) -> list[TradeResult]:
        """Execute all legs for one game."""
        results = []

        for leg in legs:
            market = self._resolve_market(game, leg.market)
            if not market:
                continue

            # Determine side
            if leg.side == "BEST":
                # Copy Cannae's side (largest position)
                side = market.side
            elif leg.side == "NO":
                side = "NO"
            elif leg.side == "YES":
                side = "YES"
            else:
                side = leg.side

            # Get price for our side
            if side == "NO":
                price = market.no_price
            else:
                price = market.yes_price

            if price < leg.min_price or price > leg.max_price:
                continue

            # Determine USD amount
            if leg.usd == "proportional":
                # Proportional to Cannae's allocation
                leg_weight = market.total_bought / game.total_usdc if game.total_usdc > 0 else 0
                # Conviction: we don't have second-side data in CSV, assume 1.0
                usd = bankroll * leg_weight * max_pct
            else:
                usd = float(leg.usd)

            if usd < leg.min_usd:
                continue

            shares = usd / price
            # Did this side win?
            # market.won = whether Cannae's SPECIFIC position won
            # market.side = which side Cannae took ("YES" or "NO")
            # If Cannae took YES and won=True → YES won → NO lost
            # If Cannae took NO and won=True → NO won → YES lost
            if market.side == "YES":
                yes_won = market.won
            else:
                yes_won = not market.won  # Cannae took NO, if he won then YES lost

            if side == "NO":
                won = not yes_won  # NO wins when YES loses
            else:
                won = yes_won

            payout = shares if won else 0
            pnl = payout - usd

            results.append(TradeResult(
                game_slug=game.slug,
                leg_desc=f"{side} {market.title[:40]}",
                side=side,
                price=price,
                cost=usd,
                shares=shares,
                payout=payout,
                pnl=pnl,
                won=won,
            ))

        return results

    def _resolve_market(self, game: Game, market_key: str) -> Optional[Market]:
        """Resolve a market key to an actual market in the game."""
        if market_key == "team_a_win":
            return game.team_a_win
        elif market_key == "team_b_win":
            return game.team_b_win
        elif market_key == "draw":
            return game.draw_market
        elif market_key == "ou":
            ous = game.ou_markets
            return ous[0] if ous else None
        elif market_key == "spread":
            sps = game.spread_markets
            return sps[0] if sps else None
        else:
            return game.get_market(market_key)

    @staticmethod
    def report(result: SimResult):
        """Print a human-readable report."""
        print()
        print("=" * 70)
        print(f"SIMULATIE: {result.name}")
        print("=" * 70)
        print(f"Games:     {result.games_played} gespeeld, {result.games_skipped} geskipt")
        print(f"Trades:    {len(result.trades)}")
        print(f"Invested:  ${result.total_invested:,.0f}")
        print(f"PnL:       ${result.total_pnl:+,.0f}")
        print(f"ROI:       {result.roi:+.1%}")
        print(f"WR:        {result.wr:.1%}")
        print(f"Avg/game:  ${result.avg_pnl_per_game:+.2f}")
        print()

        if result.by_league:
            print("PER LEAGUE (top 10):")
            for lg, d in list(result.by_league.items())[:10]:
                print(f"  {lg:10s} {d['games']:>4d} games  ROI={d['roi']:+.0%}  PnL=${d['pnl']:+,.0f}")
            print()

        if result.by_price_bucket:
            print("PER PRIJSRANGE:")
            for bucket, d in result.by_price_bucket.items():
                print(f"  {bucket:12s} {d['trades']:>4d} trades  WR={d['wr']:.0%}  ROI={d['roi']:+.0%}  PnL=${d['pnl']:+,.0f}")
            print()

        # Max drawdown
        peak = 0
        max_dd = 0
        for pnl in result.pnl_curve:
            peak = max(peak, pnl)
            dd = peak - pnl
            max_dd = max(max_dd, dd)
        print(f"Max drawdown: ${max_dd:,.0f}")
        print(f"Final PnL:    ${result.pnl_curve[-1]:+,.0f}")

    @staticmethod
    def compare(results: list[SimResult]):
        """Compare multiple simulation results side by side."""
        print()
        print("=" * 90)
        print("VERGELIJKING")
        print("=" * 90)
        print(f"{'Strategie':<30} {'Games':>6} {'Invested':>10} {'PnL':>10} {'ROI':>8} {'WR':>6} {'Avg/game':>10} {'MaxDD':>8}")
        print("-" * 90)

        for r in results:
            peak = 0
            max_dd = 0
            for pnl in r.pnl_curve:
                peak = max(peak, pnl)
                dd = peak - pnl
                max_dd = max(max_dd, dd)

            print(f"{r.name:<30} {r.games_played:>6} ${r.total_invested:>9,.0f} ${r.total_pnl:>+9,.0f} {r.roi:>+7.1%} {r.wr:>5.0%} ${r.avg_pnl_per_game:>+9.2f} ${max_dd:>7,.0f}")


def _price_bucket(price: float) -> str:
    if price < 0.20:
        return "0-20ct"
    elif price < 0.40:
        return "20-40ct"
    elif price < 0.60:
        return "40-60ct"
    elif price < 0.80:
        return "60-80ct"
    else:
        return "80-95ct"


# ============================================================
# Pre-built strategies
# ============================================================

STRATEGIES = {
    "triple_no_5": {
        "name": "Triple NO ($5 elk)",
        "legs": [
            {"side": "NO", "market": "team_a_win", "usd": 5},
            {"side": "NO", "market": "team_b_win", "usd": 5},
            {"side": "NO", "market": "draw", "usd": 5},
        ],
    },
    "triple_no_10": {
        "name": "Triple NO ($10 elk)",
        "legs": [
            {"side": "NO", "market": "team_a_win", "usd": 10},
            {"side": "NO", "market": "team_b_win", "usd": 10},
            {"side": "NO", "market": "draw", "usd": 10},
        ],
    },
    "win_only_best": {
        "name": "Win only (BEST side, proportioneel)",
        "legs": [
            {"side": "BEST", "market": "team_a_win", "usd": "proportional"},
            {"side": "BEST", "market": "team_b_win", "usd": "proportional"},
        ],
    },
    "win_draw_best": {
        "name": "Win+Draw (BEST side, proportioneel)",
        "legs": [
            {"side": "BEST", "market": "team_a_win", "usd": "proportional"},
            {"side": "BEST", "market": "team_b_win", "usd": "proportional"},
            {"side": "BEST", "market": "draw", "usd": "proportional"},
        ],
    },
    "cannae_full": {
        "name": "Cannae full copy (alle types, proportioneel)",
        "legs": [
            {"side": "BEST", "market": "team_a_win", "usd": "proportional"},
            {"side": "BEST", "market": "team_b_win", "usd": "proportional"},
            {"side": "BEST", "market": "draw", "usd": "proportional"},
            {"side": "BEST", "market": "ou", "usd": "proportional"},
        ],
    },
    "double_no": {
        "name": "Double NO (team_a + team_b, $5 elk)",
        "legs": [
            {"side": "NO", "market": "team_a_win", "usd": 5},
            {"side": "NO", "market": "team_b_win", "usd": 5},
        ],
    },
}


if __name__ == "__main__":
    sim = Simulator()
    sim.load_data(sport_filter="soccer", min_markets=3)

    results = []
    for key, strat in STRATEGIES.items():
        r = sim.run(
            legs=strat["legs"],
            name=strat["name"],
            bankroll=1410,
            max_pct=0.08,
            n_games=1000,
        )
        sim.report(r)
        results.append(r)

    sim.compare(results)
