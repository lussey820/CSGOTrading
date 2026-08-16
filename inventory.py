"""CS2 inventory management - track user's real holdings + compute P&L.

Tables:
    cs2_inventory (id/item_name/shares/buy_price/buy_date/notes/created_at/updated_at)

P&L formulas:
    yesterday_pnl = (current_price - yesterday_price) * shares
    total_pnl     = (current_price - buy_price) * shares

Realtime/yesterday price source: apis.cs2market.web_scraper.fetch_item_market_data
    - diff_1day is the % change vs yesterday → yesterday_price = current / (1 + diff_1day/100)
    - current_price = sell_price (lowest sell price on SteamDT) or BUFF price from selling_price_list
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from database.cs2_sqlite_setup import CS2_DB_PATH
from apis.cs2market.web_scraper import fetch_item_market_data, fetch_item_chinese_name
from util.logger import logger


# ----------------- low-level DB helpers -----------------

def _conn():
    conn = sqlite3.connect(CS2_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# 平台价格可信度优先级(BUFF/Steam/C5/悠悠 等国内主流成交价优先)
_PLATFORM_PRIORITY = ["buff", "steam", "c5", "c5game", "youpin", "haloskins", "igxe", "uuys"]


def _platform_key(entry: dict) -> str:
    """Return a normalized platform identifier for a selling_price_list entry."""
    for k in ("platform", "platformCode", "name", "platformName"):
        v = entry.get(k)
        if v:
            return str(v).strip().lower()
    return ""


def _price_from_entry(entry: dict) -> Optional[float]:
    """Extract a positive price from a selling_price_list entry."""
    for key in ("price", "sellPrice", "sell", "sell_price"):
        val = entry.get(key)
        if val is None:
            continue
        try:
            v = float(val)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def _extract_current_price(market_data: Optional[dict]) -> Optional[float]:
    """Extract the best current price from SteamDT market data.

    Preference order:
        1. sell_price (SteamDT lowest sell) — only if it looks like a real price
           (reject values < $1 which are usually currency-conversion glitches).
        2. selling_price_list — pick the highest-priority platform with a valid
           positive price (BUFF → Steam → C5 → 悠悠 → ...).
        3. consignment_best / purchase_best — only if >= 1.0 (real skin prices
           are never pennies; tiny values here are known SteamDT glitches).
    """
    if not market_data:
        return None

    # 1. sell_price, with sanity floor
    sp = market_data.get("sell_price")
    if sp is not None:
        try:
            v = float(sp)
            if v >= 1.0:
                return v
        except (TypeError, ValueError):
            pass

    # 2. selling_price_list — first match by priority, then lowest valid price
    listings = market_data.get("selling_price_list", []) or []
    by_platform: Dict[str, float] = {}
    for entry in listings:
        if not isinstance(entry, dict):
            continue
        pkey = _platform_key(entry)
        price = _price_from_entry(entry)
        if price is None:
            continue
        # 同平台取最低有效卖价
        if pkey not in by_platform or price < by_platform[pkey]:
            by_platform[pkey] = price

    for preferred in _PLATFORM_PRIORITY:
        for pkey, price in by_platform.items():
            if preferred in pkey:
                return price

    # fallback: 任何平台的有效价,取最低
    if by_platform:
        return min(by_platform.values())

    # 3. consignment / purchase best, with sanity floor
    for key in ("consignment_best", "purchase_best"):
        v = market_data.get(key)
        if v is None:
            continue
        try:
            fv = float(v)
            if fv >= 1.0:
                return fv
        except (TypeError, ValueError):
            continue
    return None


def _get_market_prices(item_name: str) -> Dict:
    """Fetch current + yesterday price for an item.

    Returns:
        {
            "current_price": Optional[float],
            "yesterday_price": Optional[float],
            "diff_1day_pct": Optional[float],
            "raw": dict  # raw market data
        }
    """
    result = {
        "current_price": None,
        "yesterday_price": None,
        "diff_1day_pct": None,
        "raw": None,
    }
    try:
        md = fetch_item_market_data(item_name)
    except Exception as e:
        logger.error(f"inventory: fetch market data failed for {item_name}: {e}")
        return result
    if not md:
        return result
    result["raw"] = md
    cur = _extract_current_price(md)
    result["current_price"] = cur
    diff = md.get("diff_1day")
    if diff is not None:
        try:
            diff_pct = float(diff)
            result["diff_1day_pct"] = diff_pct
            # diff_1day is percentage change vs yesterday (e.g. 2.5 means +2.5%)
            # so yesterday_price = current / (1 + diff/100)
            if cur is not None and cur > 0:
                result["yesterday_price"] = cur / (1.0 + diff_pct / 100.0)
        except (TypeError, ValueError):
            pass
    return result


# ----------------- 中文名辅助 -----------------

def get_chinese_name(item_name: str) -> Optional[str]:
    """Fetch the official Chinese name for an item from SteamDT (zh page).

    Falls back to the English name if the Chinese name cannot be fetched,
    so callers always get a display string.
    """
    try:
        cn = fetch_item_chinese_name(item_name)
        if cn and cn.strip():
            return cn.strip()
    except Exception as e:
        logger.error(f"inventory: fetch Chinese name failed for {item_name}: {e}")
    return item_name


# ----------------- CRUD -----------------

def add_item(
    item_name: str,
    shares: int,
    buy_price: float,
    buy_date: str,
    notes: str = "",
) -> Optional[Dict]:
    """Add an item to the inventory.

    Args:
        item_name: Steam market hash name
        shares: quantity held
        buy_price: per-unit buy price
        buy_date: YYYY-MM-DD
        notes: optional note

    Returns: the inserted row as dict, or None on failure.
    """
    if shares <= 0:
        logger.error(f"inventory: shares must be > 0 (got {shares})")
        return None
    if buy_price <= 0:
        logger.error(f"inventory: buy_price must be > 0 (got {buy_price})")
        return None
    try:
        # validate buy_date format
        datetime.strptime(buy_date, "%Y-%m-%d")
    except ValueError:
        logger.error(f"inventory: bad buy_date '{buy_date}', expected YYYY-MM-DD")
        return None

    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # 获取官方中文名(写库);抓取失败则回退英文名
    item_name_cn = get_chinese_name(item_name)
    try:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO cs2_inventory
                    (id, item_name, item_name_cn, shares, buy_price, buy_date, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, item_name, item_name_cn, shares, buy_price, buy_date, notes, now, now),
            )
            conn.commit()
        logger.info(f"inventory: added {shares}x {item_name} ({item_name_cn}) @ {buy_price}")
        return {
            "id": item_id,
            "item_name": item_name,
            "item_name_cn": item_name_cn,
            "shares": shares,
            "buy_price": buy_price,
            "buy_date": buy_date,
            "notes": notes,
            "created_at": now,
            "updated_at": now,
        }
    except Exception as e:
        logger.error(f"inventory: add_item failed: {e}")
        return None


def remove_item(item_id: str) -> bool:
    """Remove an inventory row by id."""
    try:
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM cs2_inventory WHERE id = ?", (item_id,)
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"inventory: remove_item failed: {e}")
        return False


def update_item(
    item_id: str,
    shares: Optional[int] = None,
    buy_price: Optional[float] = None,
    buy_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> bool:
    """Update fields of an inventory row. Only non-None fields are updated."""
    fields = []
    params: List = []
    if shares is not None:
        if shares <= 0:
            logger.error(f"inventory: shares must be > 0 (got {shares})")
            return False
        fields.append("shares = ?")
        params.append(shares)
    if buy_price is not None:
        if buy_price <= 0:
            logger.error(f"inventory: buy_price must be > 0 (got {buy_price})")
            return False
        fields.append("buy_price = ?")
        params.append(buy_price)
    if buy_date is not None:
        try:
            datetime.strptime(buy_date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"inventory: bad buy_date '{buy_date}'")
            return False
        fields.append("buy_date = ?")
        params.append(buy_date)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if not fields:
        return False
    fields.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(item_id)
    try:
        with _conn() as conn:
            cur = conn.execute(
                f"UPDATE cs2_inventory SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"inventory: update_item failed: {e}")
        return False


def list_items(with_market: bool = True, progress=None) -> List[Dict]:
    """List inventory items with optional realtime P&L.

    Args:
        with_market: if True, fetch realtime prices + compute P&L per item.
            False returns raw DB rows (faster).
        progress: optional callback called with item_name after each
            realtime price fetch (only when with_market=True).

    Returns: list of dicts with keys:
        id, item_name, item_name_cn, shares, buy_price, buy_date, notes,
        created_at, updated_at,
        current_price (Optional), yesterday_price (Optional),
        diff_1day_pct (Optional),
        yesterday_pnl (Optional),  # (current - yesterday) * shares
        total_pnl (Optional),      # (current - buy) * shares
        total_pnl_pct (Optional),  # total_pnl / (buy * shares) * 100
    """
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cs2_inventory ORDER BY buy_date ASC, item_name ASC"
            ).fetchall()
    except Exception as e:
        logger.error(f"inventory: list_items failed: {e}")
        return []

    items = []
    for r in rows:
        item = dict(r)
        # 兼容:老数据可能没有 item_name_cn,回退英文名
        if not item.get("item_name_cn"):
            item["item_name_cn"] = item.get("item_name", "")
        # normalize numeric fields
        try:
            item["shares"] = int(item["shares"])
        except (TypeError, ValueError):
            item["shares"] = 0
        try:
            item["buy_price"] = float(item["buy_price"])
        except (TypeError, ValueError):
            item["buy_price"] = 0.0
        if item.get("buy_date"):
            # trim to YYYY-MM-DD for display
            try:
                item["buy_date"] = item["buy_date"][:10]
            except Exception:
                pass
        # default empty P&L fields
        item["current_price"] = None
        item["yesterday_price"] = None
        item["diff_1day_pct"] = None
        item["yesterday_pnl"] = None
        item["total_pnl"] = None
        item["total_pnl_pct"] = None
        if with_market:
            prices = _get_market_prices(item["item_name"])
            cur = prices["current_price"]
            yest = prices["yesterday_price"]
            item["current_price"] = cur
            item["yesterday_price"] = yest
            item["diff_1day_pct"] = prices["diff_1day_pct"]
            shares = item["shares"]
            if cur is not None and yest is not None:
                item["yesterday_pnl"] = round((cur - yest) * shares, 2)
            if cur is not None:
                cost = item["buy_price"] * shares
                pnl = (cur - item["buy_price"]) * shares
                item["total_pnl"] = round(pnl, 2)
                if cost > 0:
                    item["total_pnl_pct"] = round(pnl / cost * 100.0, 2)
            if progress is not None:
                try:
                    progress(item["item_name"])
                except Exception:
                    pass
        items.append(item)
    return items


def get_item(item_id: str) -> Optional[Dict]:
    try:
        with _conn() as conn:
            r = conn.execute(
                "SELECT * FROM cs2_inventory WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(r) if r else None
    except Exception as e:
        logger.error(f"inventory: get_item failed: {e}")
        return None


def list_item_names() -> List[str]:
    """Return distinct item names in the inventory (for advisor to iterate)."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT item_name FROM cs2_inventory ORDER BY item_name ASC"
            ).fetchall()
            return [r["item_name"] for r in rows]
    except Exception as e:
        logger.error(f"inventory: list_item_names failed: {e}")
        return []


def backfill_chinese_names(items: Optional[List[str]] = None) -> Dict:
    """为缺少 item_name_cn 的库存记录补全中文名(抓取 SteamDT zh 页)。

    Args:
        items: 需要处理的英文名列表;None 表示所有缺中文名的记录。

    Returns: {"updated": n, "failed": n, "items": {name: cn_or_None}}
    """
    if items is None:
        try:
            with _conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT item_name FROM cs2_inventory "
                    "WHERE item_name_cn IS NULL OR item_name_cn = ''"
                ).fetchall()
                items = [r["item_name"] for r in rows]
        except Exception as e:
            logger.error(f"inventory: backfill query failed: {e}")
            return {"updated": 0, "failed": 0, "items": {}}

    updated = 0
    failed = 0
    result: Dict[str, Optional[str]] = {}
    for name in items:
        cn = get_chinese_name(name)
        result[name] = cn if cn and cn != name else None
        if result[name]:
            try:
                with _conn() as conn:
                    conn.execute(
                        "UPDATE cs2_inventory SET item_name_cn = ?, updated_at = ? "
                        "WHERE item_name = ?",
                        (cn, datetime.now(timezone.utc).isoformat(), name),
                    )
                    conn.commit()
                updated += 1
            except Exception as e:
                logger.error(f"inventory: backfill update failed for {name}: {e}")
                failed += 1
        else:
            failed += 1
    logger.info(f"inventory: backfill chinese names updated={updated} failed={failed}")
    return {"updated": updated, "failed": failed, "items": result}


# ----------------- CLI -----------------

def _cli():
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage:")
        print('  python inventory.py add "Item Name" <shares> <buy_price> <buy_date> [notes]')
        print("  python inventory.py list [--no-market]")
        print('  python inventory.py remove <item_id>')
        print('  python inventory.py update <item_id> [--shares N] [--buy-price P] [--buy-date D] [--notes T]')
        return

    cmd = sys.argv[1].lower()
    if cmd == "add" and len(sys.argv) >= 6:
        name, shares, price, date = sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
        notes = sys.argv[6] if len(sys.argv) > 6 else ""
        row = add_item(name, shares, price, date, notes)
        if row:
            print(json.dumps(row, ensure_ascii=False, indent=2))
    elif cmd == "list":
        with_market = "--no-market" not in sys.argv
        items = list_items(with_market=with_market)
        print(json.dumps(items, ensure_ascii=False, indent=2, default=str))
    elif cmd == "remove" and len(sys.argv) >= 3:
        ok = remove_item(sys.argv[2])
        print("removed" if ok else "not found")
    elif cmd == "update" and len(sys.argv) >= 3:
        item_id = sys.argv[2]
        kwargs = {}
        i = 3
        while i < len(sys.argv) - 1:
            k = sys.argv[i]
            v = sys.argv[i + 1]
            if k == "--shares":
                kwargs["shares"] = int(v)
            elif k == "--buy-price":
                kwargs["buy_price"] = float(v)
            elif k == "--buy-date":
                kwargs["buy_date"] = v
            elif k == "--notes":
                kwargs["notes"] = v
            i += 2
        ok = update_item(item_id, **kwargs)
        print("updated" if ok else "failed")
    else:
        print("Invalid command. See usage with no args.")


if __name__ == "__main__":
    _cli()
