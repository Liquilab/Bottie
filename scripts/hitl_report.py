#!/usr/bin/env python3
"""
HITL (Human-In-The-Loop) Report Generator

Vergelijkt Cannae's posities met onze posities en genereert een kooplijst.
Categorieën:
  BOT  = >$2.50 → bot handelt dit automatisch
  HITL = $1.00-$2.50 → handmatig kopen (onder bot minimum)
  SKIP = <$1.00 → te klein om winstgevend te handelen

Usage:
  python3 hitl_report.py                    # rapport + dry-run
  python3 hitl_report.py --buy              # koop HITL orders
  python3 hitl_report.py --date 2026-03-22  # specifieke datum
  python3 hitl_report.py --all-dates        # alle open games
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CANNAE = "0x7ea571c40408f340c1c8fc8eaacebab53c1bde7b"
US = "0x9f23f6d5d18f9Fc5aeF42EFEc8f63a7db3dB6D15"

BOT_MIN = 2.50
HITL_MIN = 1.00

# Profitable leagues from backtest (skip ucl, mex, fr2, elc, spl, aus, cdr, cde, tur, sea)
ALLOWED_LEAGUES = {"epl", "bun", "lal", "fl1", "uel", "arg", "mls", "rou1", "efa", "por", "bra", "itc", "ere", "es2", "bl2"}


def fetch(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "Bottie/1.0", "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
            elif e.code in (400, 404):
                return []
            elif attempt == retries - 1:
                return []
            else:
                time.sleep(1)
        except Exception:
            if attempt == retries - 1:
                return []
            time.sleep(1)
    return []


def get_all_positions(user, label=""):
    """Fetch all positions (paginated)."""
    all_pos = []
    offset = 0
    while True:
        data = fetch(f"{DATA_API}/positions?user={user}&limit=500&sizeThreshold=0.01&offset={offset}")
        if not data:
            break
        all_pos.extend(data)
        if len(data) < 500:
            break
        offset += 500
        time.sleep(0.2)
    active = [p for p in all_pos if float(p.get("size", 0)) > 0.01]
    if label:
        print(f"  {label}: {len(active)} open positions")
    return active


def is_win_draw(title):
    """Check if position is win or draw market (core strategy)."""
    if not title:
        return False
    t = title.lower()
    return bool(re.search(r'will .* win', t)) or 'draw' in t


def market_type(title):
    t = (title or '').lower()
    if 'draw' in t:
        return 'draw'
    if re.search(r'will .* win', t):
        return 'win'
    if 'spread' in t:
        return 'spread'
    if 'o/u' in t:
        return 'o/u'
    if 'both teams' in t:
        return 'btts'
    return 'other'


def extract_game_date(pos):
    """Extract game date from title or endDate."""
    title = pos.get('title', '') or ''
    m = re.search(r'(\d{4}-\d{2}-\d{2})', title)
    if m:
        return m.group(1)
    end_date = (pos.get('endDate') or '')[:10]
    if end_date:
        return end_date
    return ''


def main():
    parser = argparse.ArgumentParser(description="HITL Report")
    parser.add_argument("--date", default=None, help="Filter by game date")
    parser.add_argument("--all-dates", action="store_true", help="Show all open games")
    parser.add_argument("--buy", action="store_true", help="Execute HITL buys")
    parser.add_argument("--bankroll", type=float, default=0, help="Override bankroll")
    args = parser.parse_args()

    if not args.date and not args.all_dates:
        args.date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"═══ HITL Report — {args.date or 'ALL DATES'} ═══\n")

    # Fetch positions
    print("Fetching positions...")
    cannae_pos = get_all_positions(CANNAE, "Cannae")
    our_pos = get_all_positions(US, "Ons")

    # Build lookup: conditionId → our position
    our_by_cid = {}
    for p in our_pos:
        cid = p.get('conditionId', '')
        if cid:
            our_by_cid[cid] = p

    our_by_event = defaultdict(list)
    for p in our_pos:
        slug = (p.get('eventSlug', '') or '').split('-more-markets')[0]
        if slug:
            our_by_event[slug].append(p)

    # Group Cannae positions by event
    cannae_games = defaultdict(list)
    for p in cannae_pos:
        title = p.get('title', '') or ''
        # Only "Will X win?" markets (hauptbet strategy)
        if not re.search(r'will .* win', title.lower()):
            continue

        game_date = extract_game_date(p)
        if args.date and game_date != args.date:
            continue

        slug = (p.get('eventSlug', '') or '').split('-more-markets')[0]
        if not slug:
            continue

        # League filter
        league = slug.split('-')[0] if '-' in slug else ''
        if league not in ALLOWED_LEAGUES:
            continue

        price = float(p.get('avgPrice', 0) or 0)
        cur_price = float(p.get('curPrice', 0) or 0)
        iv = float(p.get('initialValue', 0) or 0)
        size = float(p.get('size', 0) or 0)
        shares = size  # size = current shares

        cannae_games[slug].append({
            'title': p.get('title', ''),
            'outcome': p.get('outcome', ''),
            'avg_price': price,
            'cur_price': cur_price,
            'usdc': iv,
            'shares': shares,
            'market_type': market_type(p.get('title', '')),
            'condition_id': p.get('conditionId', ''),
            'asset': p.get('asset', ''),
            'game_date': game_date,
        })

    if not cannae_games:
        print(f"\nGeen win/draw games gevonden voor {args.date or 'open dates'}.")
        return

    # Get bankroll
    if args.bankroll > 0:
        bankroll = args.bankroll
    else:
        try:
            val = fetch(f"{DATA_API}/value?user={US}")
            pv = float(val[0]['value']) if val else 0
        except Exception:
            pv = 0
        bankroll = max(pv, 800)
    print(f"  Bankroll: ${bankroll:.0f}\n")

    # Process games
    stats = {'bot': 0, 'hitl': 0, 'skip': 0, 'already': 0}
    hitl_orders = []
    total_hitl_cost = 0

    for slug in sorted(cannae_games.keys()):
        legs = cannae_games[slug]
        game_date = legs[0]['game_date']

        # Per conditionId: keep the leg with most shares
        best = {}
        for l in legs:
            cid = l['condition_id']
            if cid not in best or l['shares'] > best[cid]['shares']:
                best[cid] = l

        # Hauptbet strategy: only the biggest win-leg per event
        win_legs = sorted([l for l in best.values() if l['market_type'] == 'win'],
                          key=lambda x: x['shares'], reverse=True)

        if not win_legs:
            continue
        selected = win_legs[:1]  # Only hauptbet

        cannae_total_shares = sum(l['shares'] for l in selected)
        cannae_total_usdc = sum(l['shares'] * l['avg_price'] for l in selected)

        # Proportional sizing: our shares = cannae shares × (our_bankroll / cannae_portfolio_ref)
        # Use copy_base_size_pct from config (20%)
        game_budget = bankroll * 0.20
        if cannae_total_usdc > 0:
            factor = game_budget / cannae_total_usdc
        else:
            factor = 0

        # Check what we already have
        we_have = []
        we_miss = []
        for l in selected:
            cid = l['condition_id']
            if cid in our_by_cid:
                we_have.append(l)
            else:
                we_miss.append(l)

        if not we_miss:
            stats['already'] += 1
            continue

        # Print game header
        print(f"{'─'*90}")
        print(f"  {slug}  ({game_date})  Cannae ${cannae_total_usdc:,.0f}")

        for l in selected:
            cid = l['condition_id']
            buy_price = l['cur_price'] if 0.02 < l['cur_price'] < 0.98 else l['avg_price']
            our_shares = l['shares'] * factor
            our_usdc = our_shares * buy_price

            # Determine category
            have = cid in our_by_cid
            if have:
                our_existing = our_by_cid[cid]
                our_shares_have = float(our_existing.get('size', 0))
                cat = '✓ HAVE'
                stats['already'] += 0  # counted at game level
            elif our_usdc >= BOT_MIN:
                cat = 'BOT'
                stats['bot'] += 1
            elif our_usdc >= HITL_MIN:
                cat = '→ HITL'
                stats['hitl'] += 1
                hitl_orders.append({
                    'slug': slug,
                    'title': l['title'],
                    'outcome': l['outcome'],
                    'asset': l['asset'],
                    'condition_id': cid,
                    'price': round(buy_price, 2),
                    'shares': max(round(our_shares, 1), 5),
                    'usdc': round(max(our_usdc, HITL_MIN), 2),
                    'game_date': game_date,
                })
                total_hitl_cost += max(our_usdc, HITL_MIN)
            else:
                cat = 'SKIP'
                stats['skip'] += 1

            pct = l['shares'] / cannae_total_shares * 100 if cannae_total_shares > 0 else 0
            price_display = f"@{buy_price*100:4.0f}ct"
            size_display = f"${our_usdc:5.2f}" if not have else f"${float(our_existing.get('currentValue', 0)):5.2f}"

            print(f"    {cat:>8s}  {l['market_type']:>4s}  {l['title'][:42]:42s}  {l['outcome']:>5s}  "
                  f"{price_display}  {size_display}  [{pct:4.1f}%]")

    # Summary
    print(f"\n{'═'*90}")
    print(f"SAMENVATTING")
    print(f"  Games: {len(cannae_games)}")
    print(f"  Al in bezit: {stats['already']}")
    print(f"  BOT orders (>$2.50): {stats['bot']}")
    print(f"  HITL orders ($1-$2.50): {stats['hitl']} → ${total_hitl_cost:.2f}")
    print(f"  SKIP (<$1): {stats['skip']}")
    print(f"{'═'*90}")

    if not hitl_orders:
        print("\nGeen HITL orders. Klaar.")
        return

    # Print HITL order list
    print(f"\n{'─'*90}")
    print(f"HITL KOOPLIJST ({len(hitl_orders)} orders, ${total_hitl_cost:.2f}):")
    print(f"{'─'*90}")
    for o in hitl_orders:
        print(f"  {o['title'][:45]:45s} {o['outcome']:>5s} @{o['price']*100:.0f}ct "
              f"${o['usdc']:.2f} ({o['shares']:.0f}sh)")

    if not args.buy:
        print(f"\n  Gebruik --buy om deze orders te plaatsen.")
        return

    # Execute HITL buys
    print(f"\nOrders plaatsen...")
    from py_clob_client.client import ClobClient
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderArgs, OrderType as OT
    from dotenv import load_dotenv
    load_dotenv("/opt/bottie/.env")

    pk = os.environ.get("PRIVATE_KEY", "")
    if not pk.startswith("0x"):
        pk = "0x" + pk

    client = ClobClient(CLOB_API, key=pk, chain_id=137, signature_type=2, funder=US)
    client.set_api_creds(client.derive_api_key())

    filled = 0
    failed = 0
    for i, order in enumerate(hitl_orders):
        print(f"  [{i+1}/{len(hitl_orders)}] {order['title'][:40]} {order['outcome']} "
              f"@{order['price']*100:.0f}ct ${order['usdc']:.2f}...", end=" ", flush=True)
        try:
            order_args = OrderArgs(
                price=order['price'],
                size=order['shares'],
                side=BUY,
                token_id=order['asset'],
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OT.GTC)
            status = resp.get("status", "?")
            print(f"→ {status}")
            if "matched" in str(status).lower() or resp.get("size_matched", "0") != "0":
                filled += 1
            time.sleep(0.3)
        except Exception as e:
            err = str(e)
            if "taker fee" in err:
                m = re.search(r'taker fee: (\d+)', err)
                if m:
                    print(f"retry...", end=" ", flush=True)
                    try:
                        resp = client.post_order(signed, OT.GTC)
                        print(f"→ {resp.get('status', '?')}")
                        filled += 1
                        continue
                    except Exception:
                        pass
            print(f"FAIL: {err[:60]}")
            failed += 1
            time.sleep(0.5)

    print(f"\nDONE: {filled} filled, {failed} failed, {len(hitl_orders) - filled - failed} pending")


if __name__ == "__main__":
    main()
