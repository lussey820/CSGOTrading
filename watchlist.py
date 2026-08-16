"""Watchlist management CLI - add/remove/list monitored CS2 items."""

import sys
import os
import yaml
from pathlib import Path
from apis.cs2market.api import get_cs2_stock_daily_candles_df

WATCHLIST_PATH = Path(__file__).parent / "config" / "watchlist.yaml"


def load_watchlist():
    if not WATCHLIST_PATH.exists():
        return []
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tickers", [])


def save_watchlist(tickers):
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"tickers": tickers}, f, allow_unicode=True, sort_keys=False)


def verify_item(name):
    """Verify item exists on Steam market by fetching its kline data."""
    df = get_cs2_stock_daily_candles_df(ticker=name, trading_date=None)
    return not df.empty


def add(name):
    tickers = load_watchlist()
    if name in tickers:
        print(f"Already in watchlist: {name}")
        return
    print(f"Verifying {name} ...")
    if not verify_item(name):
        print(f"Verification failed: no market data for '{name}'")
        print("Check the exact market_hash_name on Steam Community Market.")
        return
    tickers.append(name)
    save_watchlist(tickers)
    print(f"Added: {name}")


def remove(name):
    tickers = load_watchlist()
    if name not in tickers:
        print(f"Not in watchlist: {name}")
        return
    tickers.remove(name)
    save_watchlist(tickers)
    print(f"Removed: {name}")


def list_items():
    tickers = load_watchlist()
    if not tickers:
        print('Watchlist is empty. Add items: python watchlist.py add "Item Name"')
        return
    print(f"Watchlist ({len(tickers)} items):")
    for i, t in enumerate(tickers, 1):
        print(f"  {i}. {t}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python watchlist.py add "Item Market Hash Name"')
        print('  python watchlist.py remove "Item Market Hash Name"')
        print('  python watchlist.py list')
        print('  python watchlist.py clear')
        return
    cmd = sys.argv[1].lower()
    if cmd == "add" and len(sys.argv) >= 3:
        add(sys.argv[2])
    elif cmd == "remove" and len(sys.argv) >= 3:
        remove(sys.argv[2])
    elif cmd == "list":
        list_items()
    elif cmd == "clear":
        save_watchlist([])
        print("Watchlist cleared")
    else:
        print("Invalid command. Usage: python watchlist.py [add|remove|list|clear] [name]")


if __name__ == "__main__":
    main()
