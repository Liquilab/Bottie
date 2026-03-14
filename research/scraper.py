"""Scrape Polymarket leaderboard and wallet histories."""

import json
from pathlib import Path

import httpx


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


async def scrape_leaderboard(
    category: str = "OVERALL",
    time_period: str = "MONTH",
    order_by: str = "PNL",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Fetch top traders from the Polymarket leaderboard.

    Endpoint: GET /v1/leaderboard
    Params:
      category: OVERALL|POLITICS|SPORTS|CRYPTO|CULTURE|WEATHER|ECONOMICS|TECH|FINANCE
      timePeriod: DAY|WEEK|MONTH|ALL
      orderBy: PNL|VOL
      limit: 1-50
      offset: 0-1000
    """
    url = f"{DATA_API}/v1/leaderboard"
    params = {
        "category": category,
        "timePeriod": time_period,
        "orderBy": order_by,
        "limit": limit,
        "offset": offset,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    return data if isinstance(data, list) else []


async def get_public_profile(address: str) -> dict | None:
    """Fetch public profile for a wallet address.

    Endpoint: GET /public-profile?address=0x...
    """
    url = f"{GAMMA_API}/public-profile"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, params={"address": address})
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return None


async def download_wallet_trades(
    wallet: str,
    limit: int = 500,
    output_dir: str = "data/wallet_trades/",
) -> int:
    """Download historical trades for a specific wallet."""
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)

    output_file = p / f"{wallet[:10].lower()}.jsonl"

    # Load existing trade IDs for deduplication
    existing_ids = set()
    if output_file.exists():
        for line in output_file.read_text().splitlines():
            try:
                t = json.loads(line)
                tid = str(t.get("id") or t.get("transactionHash") or "")
                if tid:
                    existing_ids.add(tid)
            except (json.JSONDecodeError, KeyError):
                pass

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{DATA_API}/trades",
            params={"maker": wallet, "limit": limit},
        )
        resp.raise_for_status()
        trades = resp.json()

    new_count = 0
    with open(output_file, "a") as f:
        for trade in trades:
            tid = str(trade.get("id") or trade.get("transactionHash") or "")
            if tid and tid in existing_ids:
                continue
            f.write(json.dumps(trade) + "\n")
            new_count += 1

    return new_count


async def update_watchlist_from_leaderboard(
    current_watchlist: list[dict],
) -> list[dict]:
    """Stub — wallet discovery is now handled by wallet_scout.py.
    Returns the current watchlist unchanged."""
    return current_watchlist
