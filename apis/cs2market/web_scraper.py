"""
SteamDT web page scraper - fetches trade volume & market data not available via Open API.

SteamDT Open API kline endpoint explicitly excludes trade volume data.
This module scrapes the SSR-embedded __NUXT_DATA__ from the public trend page,
which contains: Day3 trade volume, turnover rate, survive count, multi-platform
prices, price diffs, and analysis tags.

No API key required - reads server-side rendered HTML.
"""

import os
import re
import json
import time
import logging
import requests
from typing import Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

STEAMDT_WEB_BASE = "https://www.steamdt.com"
_REQUEST_TIMEOUT = 15
_RATE_LIMIT_SECONDS = 3  # min interval between requests

_last_request_time = 0.0

# 内存价格缓存:同一饰品在 TTL 内直接复用,避免每次加载都重复抓取 SteamDT。
# 页面加载(库存/AI 持仓/决策对照)是主要受益者;缓存线程安全要求不高,
# 单进程 Flask 下足够。TTL 可通过环境变量 PRICE_CACHE_TTL 调整(秒)。
PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL", "300"))
_PRICE_CACHE: Dict[str, tuple] = {}  # market_hash_name -> (ts, result)


def _price_cache_get(name: str) -> Optional[dict]:
    """返回未过期的价格缓存,否则 None。"""
    hit = _PRICE_CACHE.get(name)
    if hit and time.time() - hit[0] < PRICE_CACHE_TTL:
        return hit[1]
    return None


def _price_cache_set(name: str, result: Optional[dict]):
    if result is None:
        return
    _PRICE_CACHE[name] = (time.time(), result)
    # 轻量清理:条目过多时剔除过期项,防止无限增长
    if len(_PRICE_CACHE) > 5000:
        cutoff = time.time() - PRICE_CACHE_TTL
        for k in [k for k, v in _PRICE_CACHE.items() if v[0] < cutoff]:
            _PRICE_CACHE.pop(k, None)


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.time()


def _fetch_page_html(market_hash_name: str, locale: str = "en") -> Optional[str]:
    """Fetch the SteamDT trend page HTML for an item."""
    _rate_limit()
    url = f"{STEAMDT_WEB_BASE}/{locale}/cs2/{quote(market_hash_name)}?tab=trend&type=charts"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        logger.error(f"Failed to fetch SteamDT page for {market_hash_name}: {e}")
        return None


def _parse_nuxt_data(html: str) -> Optional[list]:
    """Extract and parse the __NUXT_DATA__ JSON array from SSR HTML."""
    m = re.search(r'__NUXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    if not m:
        logger.warning("No __NUXT_DATA__ found in HTML")
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse __NUXT_DATA__: {e}")
        return None


def _resolve_nuxt_payload(data: list) -> dict:
    """
    Resolve Nuxt SSR payload (flat array with index references) into nested dict.
    data[2] is the root data object containing all API responses.
    """
    def resolve(idx, depth=0, seen=None):
        if seen is None:
            seen = set()
        if idx in seen or depth > 10 or idx >= len(data):
            return None
        seen = seen | {idx}
        val = data[idx]
        if isinstance(val, dict):
            return {
                k: (resolve(v, depth + 1, seen) if isinstance(v, int) and v < len(data) else v)
                for k, v in val.items()
            }
        elif isinstance(val, list):
            return [
                resolve(i, depth + 1, seen) if isinstance(i, int) and i < len(data) else i
                for i in val
            ]
        else:
            return val

    # data[2] is the main payload (ShallowReactive wrapper)
    root = resolve(2)
    if isinstance(root, list) and len(root) >= 2 and isinstance(root[1], dict):
        return root[1]
    return {}


def _extract_day3_trade_num(analysis_tags: list) -> Optional[int]:
    """Extract Day3 trade count from analysisTags HTML span."""
    if not analysis_tags:
        return None
    for tag in analysis_tags:
        if isinstance(tag, dict) and tag.get("key") == "Day3_Trade_Num":
            style = tag.get("style", "")
            m = re.search(r"(\d+)</span>", style)
            if m:
                return int(m.group(1))
    return None


def fetch_item_market_data(market_hash_name: str, use_cache: bool = True) -> Optional[dict]:
    """
    Fetch comprehensive market data for a CS2 item from SteamDT trend page.

    带 TTL 内存缓存(PRICE_CACHE_TTL 秒):同一饰品在缓存期内直接返回,
    避免页面加载反复抓取(SteamDT 每个请求有 3s 限速)。
    use_cache=False 可绕过缓存(需要最新价的场景,如卖出结算)。

    Returns dict with fields:
        - survive_num: str (total existing count)
        - turnover_rate: float (turnover rate)
        - transaction_count: str (transaction count)
        - day3_trade_num: Optional[int] (3-day total trade volume across platforms)
        - sell_price: float (lowest sell price)
        - diff_1day/7day/30day/6month: float (price change %)
        - selling_price_list: list[dict] (per-platform prices)
        - consignment_best: float
        - purchase_best: float
        - cash_ratio: float
        - analysis_tags: list[dict]
        - related_items: list[dict] (other wear conditions)
    """
    if use_cache:
        cached = _price_cache_get(market_hash_name)
        if cached is not None:
            return cached

    html = _fetch_page_html(market_hash_name)
    if not html:
        return None

    data = _parse_nuxt_data(html)
    if not data:
        return None

    payload = _resolve_nuxt_payload(data)

    # Find the item detail object (has 'surviveNum' key)
    item_detail = None
    related_list = None
    for key, val in payload.items():
        if not isinstance(val, dict):
            continue
        resp = val.get("data")
        if not isinstance(resp, dict):
            continue
        if "surviveNum" in resp and "sellingPriceList" in resp:
            item_detail = resp
        elif "relatedList" in resp:
            related_list = resp.get("relatedList")

    if not item_detail:
        logger.warning(f"Item detail not found in SSR data for {market_hash_name}")
        return None

    day3_trade = _extract_day3_trade_num(item_detail.get("analysisTags", []))

    result = {
        "market_hash_name": market_hash_name,
        "item_id": item_detail.get("itemId"),
        "name": item_detail.get("name"),
        "survive_num": item_detail.get("surviveNum"),
        "turnover_rate": item_detail.get("turnoverRate"),
        "turnover_rate_is_rise": item_detail.get("turnoverRateIsRise"),
        "holders_num": item_detail.get("holdersNum"),
        "volume_ratio": item_detail.get("volumeRatio"),
        "transaction_count": item_detail.get("transactionCount"),
        "day3_trade_num": day3_trade,
        "sell_price": item_detail.get("sellPrice"),
        "diff_1day": item_detail.get("diff1Day"),
        "diff_7day": item_detail.get("diff7Day"),
        "diff_30day": item_detail.get("diff30Day"),
        "diff_6month": item_detail.get("diff6Month"),
        "diff_1day_price": item_detail.get("diff1DayPrice"),
        "diff_7day_price": item_detail.get("diff7DayPrice"),
        "diff_30day_price": item_detail.get("diff30DayPrice"),
        "diff_6month_price": item_detail.get("diff6MonthPrice"),
        "increase_price": item_detail.get("increasePrice"),
        "consignment_best": item_detail.get("consignmentBest"),
        "purchase_best": item_detail.get("purchaseBest"),
        "purchase_stable": item_detail.get("purchaseStable"),
        "cash_ratio": item_detail.get("cashRatio"),
        "selling_price_list": item_detail.get("sellingPriceList", []),
        "analysis_tags": item_detail.get("analysisTags", []),
        "related_items": related_list or [],
    }
    logger.info(
        f"Scraped {market_hash_name}: day3_trade={day3_trade}, "
        f"survive={result['survive_num']}, turnover={result['turnover_rate']}"
    )
    if use_cache:
        _price_cache_set(market_hash_name, result)
    return result


def fetch_item_chinese_name(market_hash_name: str) -> Optional[str]:
    """Fetch the official Chinese name for a CS2 item from SteamDT zh page.

    SteamDT zh locale renders localized item names (e.g. "法玛斯 | ZX81 彩色 (崭新出厂)")
    in the SSR payload, keyed by the same marketHashName. This avoids naive
    translation - we use the platform's own official Chinese name.

    Returns the Chinese name string, or None on failure.
    """
    html = _fetch_page_html(market_hash_name, locale="zh")
    if not html:
        return None

    data = _parse_nuxt_data(html)
    if not data:
        return None

    payload = _resolve_nuxt_payload(data)

    # Locate the item detail object; its `name` field is the Chinese name.
    def _find_cn_name(obj):
        if isinstance(obj, dict):
            if (
                "name" in obj
                and "marketHashName" in obj
                and obj.get("marketHashName") == market_hash_name
                and isinstance(obj.get("name"), str)
                and re.search(r"[\u4e00-\u9fff]", obj["name"])
            ):
                return obj["name"]
            for v in obj.values():
                r = _find_cn_name(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = _find_cn_name(v)
                if r:
                    return r
        return None

    return _find_cn_name(payload)


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "FAMAS | ZX Spectron (Factory New)"
    result = fetch_item_market_data(name)
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Failed to fetch data")
