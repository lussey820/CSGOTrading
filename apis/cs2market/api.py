"""
CS2 Market API client - SteamDT API implementation.
Uses SteamDT Open API to fetch market data (K-line, prices).
"""

import os
import logging
import time
import pandas as pd
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("cs2market")
STEAMDT_API_BASE = os.getenv("STEAMDT_API_BASE", "https://open.steamdt.com")


def _steamdt_headers():
    # 运行时读取,保证「API 设置」页保存的 Key 无需重启即可生效
    return {
        "Authorization": f"Bearer {os.getenv('STEAMDT_API_KEY')}",
        "Content-Type": "application/json",
    }


def _fetch_kline(market_hash_name: str, kline_type: int = 1, platform: str = "ALL"):
    """
    Fetch K-line data from SteamDT API.
    
    Args:
        market_hash_name: Steam item market hash name
        kline_type: 1=daily, 2=weekly, 3=monthly
        platform: ALL/BUFF/YOUPIN/C5/STEAM/HALOSKINS
    
    Returns:
        List of kline arrays: [[timestamp, open, close, high, low], ...]
    """
    url = f"{STEAMDT_API_BASE}/open/cs2/item/v1/kline"
    payload = {
        "marketHashName": market_hash_name,
        "type": kline_type,
        "platform": platform,
    }

    last_err = None
    for attempt in range(3):  # 网络偶发超时自动重试
        try:
            resp = requests.post(url, json=payload, headers=_steamdt_headers(), timeout=30)
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"SteamDT kline API error: {data.get('errorMsg', 'unknown')}")
            return data.get("data", [])
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"SteamDT kline request failed after 3 attempts: {last_err}")


def _fetch_current_price(market_hash_name: str):
    """
    Fetch current price from SteamDT API.
    Returns the lowest sell price across all platforms, or 0.0 if unavailable.
    """
    url = f"{STEAMDT_API_BASE}/open/cs2/v1/price/single"
    params = {"marketHashName": market_hash_name}

    last_err = None
    for attempt in range(3):  # 网络偶发超时自动重试
        try:
            resp = requests.get(url, params=params, headers=_steamdt_headers(), timeout=15)
            data = resp.json()
            if not data.get("success"):
                return 0.0
            platforms = data.get("data", [])
            sell_prices = [p.get("sellPrice") for p in platforms if p.get("sellPrice", 0) > 0]
            return min(sell_prices) if sell_prices else 0.0
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    logger.warning(f"SteamDT price request failed after 3 attempts for {market_hash_name}: {last_err}")
    return 0.0


def get_cs2_stock_daily_candles_df(ticker="cs2_weapons", trading_date=None):
    """
    Return CS2 item OHLCV history in stock-market compatible format via SteamDT API.
    
    Args:
        ticker: Steam item market hash name
        trading_date: Trading date; data strictly after this date is excluded.
    
    Returns:
        DataFrame with columns: date, open, high, low, close, volume.
    """
    raw_kline = _fetch_kline(ticker, kline_type=1)
    
    if not raw_kline:
        print(f"Warning: no kline data for {ticker}")
        return pd.DataFrame()
    
    # Each kline entry: [timestamp, open, close, high, low]
    records = []
    for entry in raw_kline:
        if isinstance(entry, list) and len(entry) >= 5:
            ts_val = entry[0]
            open_val = float(entry[1])
            close_val = float(entry[2])
            high_val = float(entry[3])
            low_val = float(entry[4])
            
            # Convert timestamp to datetime (handle both string and numeric)
            if isinstance(ts_val, str):
                ts_val = float(ts_val)
            
            if isinstance(ts_val, (int, float)):
                # Second-level timestamps (SteamDT uses seconds)
                dt = datetime.fromtimestamp(int(ts_val))
            else:
                continue
            
            records.append({
                "date": dt,
                "open": open_val,
                "close": close_val,
                "high": high_val,
                "low": low_val,
                "volume": 0,  # SteamDT kline doesn't include volume
            })
    
    if not records:
        print(f"Warning: failed to parse kline data for {ticker}")
        return pd.DataFrame()
    
    df = pd.DataFrame(records)
    
    # Apply trading_date filter if provided
    if trading_date:
        if isinstance(trading_date, str):
            trading_date = pd.to_datetime(trading_date)
        
        df = df[df["date"] <= trading_date]
        
        if df.empty:
            print(f"Warning: no data on or before {trading_date.date()} for {ticker}")
            return pd.DataFrame()
    
    df = df.sort_values("date").reset_index(drop=True)
    return df


def get_cs2_last_close_price(ticker="cs2_weapons", trading_date=None):
    """
    Get the latest close price for a CS2 item up to trading_date.
    For "today" mode, uses current market price from SteamDT.
    """
    # For current date (today), use the live price API
    today = datetime.now().date()
    if trading_date and hasattr(trading_date, 'date'):
        query_date = trading_date.date() if hasattr(trading_date, 'date') else trading_date
        if hasattr(query_date, 'date'):
            query_date = query_date.date()
    else:
        query_date = today
    
    if query_date >= today:
        live_price = _fetch_current_price(ticker)
        if live_price > 0:
            return live_price
    
    # Fallback: use kline data
    df = get_cs2_stock_daily_candles_df(ticker, trading_date)
    if df.empty:
        return 0.0
    
    return float(df.iloc[-1]["close"])


class CS2MarketAPI:
    """CS2 Market API wrapper - SteamDT backend, used by the Router."""
    
    def get_cs2_stock_daily_candles_df(self, ticker, trading_date):
        """Get CS2 daily OHLCV dataframe."""
        return get_cs2_stock_daily_candles_df(ticker=ticker, trading_date=trading_date)
    
    def get_cs2_last_close_price(self, ticker, trading_date):
        """Get CS2 last close price up to trading_date."""
        return get_cs2_last_close_price(ticker=ticker, trading_date=trading_date)
