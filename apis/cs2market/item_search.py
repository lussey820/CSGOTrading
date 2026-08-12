"""SteamDT 全量饰品基础信息 + 中/英文模糊搜索。

用于「扫码识别」流程:OCR 识别出饰品名(中文或英文)后,在这里
模糊匹配出标准的 marketHashName(英文)与中文名,再写入数据库。

数据源:GET https://open.steamdt.com/open/cs2/v1/base
该接口每天只能调用一次,返回全量饰品基础信息,这里做本地缓存。
"""

import json
import os
import time
import requests
from pathlib import Path
from typing import Dict, List, Optional

from util.logger import logger

STEAMDT_API_BASE = os.getenv("STEAMDT_API_BASE", "https://open.steamdt.com")
CACHE_PATH = Path(__file__).parent / "cs2_base_items.json"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 每天最多拉 1 次


def _load_cache() -> Optional[List[Dict]]:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("items") and time.time() - float(data.get("ts", 0)) < CACHE_TTL_SECONDS:
            return data["items"]
    except Exception as e:
        logger.error(f"item_search: cache load failed: {e}")
    return None


def _save_cache(items: List[Dict]):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"ts": time.time(), "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"item_search: cache save failed: {e}")


def load_base_items(force: bool = False) -> List[Dict]:
    """加载全量饰品基础信息(带本地缓存,每天最多请求一次接口)。"""
    if not force:
        cached = _load_cache()
        if cached is not None:
            return cached
    url = f"{STEAMDT_API_BASE}/open/cs2/v1/base"
    headers = {"Authorization": f"Bearer {os.getenv('STEAMDT_API_KEY', '')}"}
    try:
        resp = requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        logger.error(f"item_search: fetch base items failed: {e}")
        raise RuntimeError(f"SteamDT 全量饰品数据获取失败: {e}")
    if not body.get("success"):
        msg = body.get("errorMsg") or body.get("errorCodeStr") or "未知错误"
        logger.error(f"item_search: base items error: {msg}")
        raise RuntimeError(f"SteamDT 返回错误: {msg}")
    items = body.get("data") or []
    if not items:
        raise RuntimeError("SteamDT 返回的饰品列表为空")
    _save_cache(items)
    logger.info(f"item_search: cached {len(items)} base items")
    return items


def search_items(query: str, limit: int = 8) -> List[Dict]:
    """按中文名/英文名模糊搜索饰品。

    Args:
        query: OCR 识别出的饰品名(中文或英文均可)
        limit: 最多返回的候选数

    Returns:
        按匹配度排序的候选列表,每项 {marketHashName, name}。
        精确匹配优先,包含匹配次之。
    """
    q = (query or "").strip()
    if not q:
        return []
    items = load_base_items()
    ql = q.lower()
    scored: List[tuple] = []
    for it in items:
        mhn = str(it.get("marketHashName") or "")
        name = str(it.get("name") or "")
        if not mhn:
            continue
        score = 0
        if mhn.lower() == ql or name == q:
            score = 100.0
        elif ql in mhn.lower():
            score = 60.0 + (1.0 - len(q) / max(len(mhn), 1)) * 30.0
        elif q in name:
            score = 50.0 + (1.0 - len(q) / max(len(name), 1)) * 30.0
        if score > 0:
            scored.append((score, {"marketHashName": mhn, "name": name}))
    scored.sort(key=lambda x: -x[0])
    return [s[1] for s in scored[:limit]]
