"""用户端操作日志 - 按月分文件 JSON 存储。

每次「买入 / 售出」记录一行,字段与 AI 端 cs2_ai_trade 对齐:
    item_name / action(buy|sell) / shares / price / fee / realized_pnl / trade_date / created_at

文件: user_trades/YYYY-MM.json (按月分文件,放在项目目录内)
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from util.logger import logger

PROJECT_ROOT = Path(__file__).parent
TRADES_DIR = PROJECT_ROOT / "user_trades"

_lock = threading.Lock()

TRADE_FIELDS = (
    "item_name",
    "item_name_cn",
    "action",
    "shares",
    "price",
    "fee",
    "realized_pnl",
    "trade_date",
    "created_at",
)


def _month_file(month: str) -> Path:
    """month: YYYY-MM。目录在项目内,按月分文件。"""
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    return TRADES_DIR / f"{month}.json"


def _load_month(month: str) -> List[Dict]:
    p = _month_file(month)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"user_trades: load {p} failed: {e}")
        return []


def _save_month(month: str, rows: List[Dict]) -> bool:
    p = _month_file(month)
    try:
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"user_trades: save {p} failed: {e}")
        return False


def add_trade(
    item_name: str,
    action: str,
    shares: int,
    price: float,
    fee: float = 0.0,
    realized_pnl: Optional[float] = None,
    item_name_cn: Optional[str] = None,
    trade_date: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Optional[Dict]:
    """追加一条用户操作日志(买入/售出)。

    Args:
        item_name: Steam market hash name
        action: 'buy' 或 'sell'
        shares: 数量(正数)
        price: 成交单价(买入价或售出价)
        fee: 手续费(买入默认 0,售出 = 售出总额 × 2%)
        realized_pnl: 已实现盈亏(售出时 = 到手价 - 买入成本;买入为 None)
        item_name_cn: 中文名(可选,展示用)
        trade_date: YYYY-MM-DD,默认今天
        created_at: ISO 时间,默认 now
    """
    if action not in ("buy", "sell"):
        logger.error(f"user_trades: bad action '{action}'")
        return None
    if shares <= 0 or price <= 0:
        logger.error(f"user_trades: shares/price must be > 0 (got {shares}/{price})")
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    trade_date = trade_date or today
    created_at = created_at or datetime.now().isoformat()

    row = {
        "item_name": item_name,
        "item_name_cn": item_name_cn or "",
        "action": action,
        "shares": int(shares),
        "price": round(float(price), 2),
        "fee": round(float(fee), 2),
        "realized_pnl": round(float(realized_pnl), 2) if realized_pnl is not None else None,
        "trade_date": trade_date,
        "created_at": created_at,
    }

    month = trade_date[:7]  # YYYY-MM
    with _lock:
        rows = _load_month(month)
        rows.append(row)
        if not _save_month(month, rows):
            return None
    logger.info(
        f"user_trades: recorded {action} {item_name} x{shares} @ {price} "
        f"(fee={fee}, pnl={realized_pnl})"
    )
    return row


def list_trades(month: Optional[str] = None, item_name: Optional[str] = None) -> List[Dict]:
    """读取用户操作日志。

    Args:
        month: YYYY-MM;None 表示读取所有已有月份文件,按时间升序
        item_name: 按饰品名过滤(精确匹配)
    """
    if month:
        months = [month]
    else:
        if not TRADES_DIR.exists():
            return []
        months = sorted(
            p.stem for p in TRADES_DIR.glob("*.json") if len(p.stem) == 7
        )
    out: List[Dict] = []
    for m in months:
        rows = _load_month(m)
        if item_name:
            rows = [r for r in rows if r.get("item_name") == item_name]
        out.extend(rows)
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


def list_month_months() -> List[str]:
    """返回已有日志的所有月份(YYYY-MM),倒序。"""
    if not TRADES_DIR.exists():
        return []
    return sorted(
        (p.stem for p in TRADES_DIR.glob("*.json") if len(p.stem) == 7),
        reverse=True,
    )


def total_realized_pnl() -> float:
    """全部已实现盈亏(所有 sell 记录 realized_pnl 之和)。"""
    return round(
        sum(r.get("realized_pnl") or 0.0 for r in list_trades() if r.get("action") == "sell"),
        2,
    )
