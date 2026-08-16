"""决策对照:AI 端虚拟账户 vs 用户端真实操作,净利润曲线 + 操作对照表。

数据源:
- AI 端:cs2_portfolio(每日净值快照,total_assets - cashflow = 净利润)、
        cs2_ai_trade(已实现卖出盈亏)
- 用户端:user_trades/*.json(买入/售出日志)、cs2_inventory(库存未实现盈亏)

净利润口径 = 已实现盈亏 + 未实现盈亏(总盈亏)。
"""

import json
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from database.cs2_sqlite_setup import CS2_DB_PATH
from util.logger import logger
from user_trades import list_trades, total_realized_pnl
from inventory import list_items, list_item_names

AI_ACCOUNT_EXP = "ai-account"

# ----------------- 后台计算状态(供前端进度条轮询) -----------------

_comparison_lock = threading.Lock()
_comparison_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "done": 0,
    "total": 0,
    "current": None,
    "error": None,
    "result": None,
}


def get_comparison_state() -> Dict:
    with _comparison_lock:
        return dict(_comparison_state)


def run_comparison_background() -> bool:
    """后台线程计算对照数据;已运行则返回 False,否则同步置 running 并启动。"""
    with _comparison_lock:
        if _comparison_state["running"]:
            return False
        _comparison_state.update(
            running=True,
            started_at=datetime.now().isoformat(),
            finished_at=None,
            done=0,
            total=0,
            current=None,
            error=None,
            result=None,
        )
    threading.Thread(target=_compute_comparison, daemon=True).start()
    return True


def _compute_comparison():
    def _progress(done: int, total: int, current: Optional[str]):
        with _comparison_lock:
            _comparison_state["done"] = done
            _comparison_state["total"] = total
            _comparison_state["current"] = current

    try:
        result = get_comparison(progress=_progress)
        with _comparison_lock:
            _comparison_state["result"] = result
            _comparison_state["running"] = False
            _comparison_state["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        logger.error(f"comparison: compute failed: {e}", exc_info=True)
        with _comparison_lock:
            _comparison_state["error"] = str(e)
            _comparison_state["running"] = False
            _comparison_state["finished_at"] = datetime.now().isoformat()


def _conn():
    conn = sqlite3.connect(CS2_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------- AI 端数据 -----------------

def _ai_config_id() -> object:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM cs2_config WHERE exp_name = ?", (AI_ACCOUNT_EXP,)
        ).fetchone()
        return row["id"] if row else None


def ai_daily_net_curve(unreal: Optional[float] = None) -> List[Dict]:
    """AI 端按天净利润曲线。

    AI 端本金口径会随「复制库存」而变(cashflow 不再等于本金),
    因此不能用 total_assets - cashflow。改用与用户端一致的口径:
    累计已实现盈亏(cs2_ai_trade),最后一天补当前未实现盈亏。
    unreal 可传入已算好的未实现盈亏,避免重复抓取价格。
    返回 [{date: YYYY-MM-DD, net: 净利润}] 按日期升序。
    """
    trades = ai_trades()
    if not trades:
        if unreal is None:
            unreal = ai_unrealized()
        if unreal != 0:
            today = datetime.now().strftime("%Y-%m-%d")
            return [{"date": today, "net": round(unreal, 2)}]
        return []
    cum: "OrderedDict[str, float]" = OrderedDict()
    running = 0.0
    for t in trades:
        d = t.get("trade_date") or ""
        if not d:
            continue
        running += float(t.get("realized_pnl") or 0.0)
        cum[d] = running
    if not cum:
        return []
    # 最后一天补未实现盈亏(与用户端口径一致:总盈亏 = 已实现 + 未实现)
    if unreal is None:
        unreal = ai_unrealized()
    final_date = list(cum.keys())[-1]
    out = []
    for d, v in cum.items():
        if d == final_date:
            v += unreal
        out.append({"date": d, "net": round(v, 2)})
    return out


def ai_trades() -> List[Dict]:
    """AI 端已执行卖出记录(cs2_ai_trade)。"""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT item_name, action, sell_price, buy_price, shares, fee, "
                "realized_pnl, trade_date FROM cs2_ai_trade "
                "ORDER BY trade_date DESC, created_at DESC"
            ).fetchall()
        return [
            {
                "item_name": r["item_name"],
                "action": r["action"],
                "price": float(r["sell_price"]) if r["sell_price"] is not None else None,
                "buy_price": float(r["buy_price"]) if r["buy_price"] is not None else None,
                "shares": int(r["shares"]) if r["shares"] else 0,
                "fee": float(r["fee"]) if r["fee"] is not None else 0.0,
                "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else 0.0,
                "trade_date": str(r["trade_date"])[:10] if r["trade_date"] else "",
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"comparison: ai trades failed: {e}")
        return []


def ai_unrealized(on_progress=None) -> float:
    """AI 端未实现盈亏:虚拟持仓 现价-成本 之和。on_progress 每抓完一个价回调。"""
    try:
        from ai_trader import _virtual_holdings
        holdings = _virtual_holdings(on_progress=on_progress)
        return round(sum(h.get("unrealized_pnl") or 0.0 for h in holdings), 2)
    except Exception as e:
        logger.error(f"comparison: ai unrealized failed: {e}")
        return 0.0


def _ai_position_names() -> List[str]:
    """AI 端有持仓(数量>0)的饰品名列表,用于预估价格抓取总数。"""
    config_id = _ai_config_id()
    if not config_id:
        return []
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT positions FROM cs2_portfolio WHERE config_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (config_id,),
            ).fetchone()
    except Exception as e:
        logger.error(f"comparison: ai positions failed: {e}")
        return []
    if not row or not row["positions"]:
        return []
    try:
        positions = json.loads(row["positions"])
    except (TypeError, ValueError):
        return []
    return [
        name for name, p in positions.items()
        if int(p.get("shares") or 0) > 0
    ]


def _inventory_row_count() -> int:
    try:
        with _conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM cs2_inventory"
            ).fetchone()["c"]
    except Exception as e:
        logger.error(f"comparison: inventory count failed: {e}")
        return 0


# ----------------- 用户端数据 -----------------

def user_trades_list() -> List[Dict]:
    """用户端操作日志(user_trades/*.json),按时间升序。"""
    return list_trades()


def user_unrealized(on_progress=None) -> float:
    """用户端未实现盈亏:库存市值 - 成本(含已售出已实现)。on_progress 每抓完一个价回调。"""
    try:
        items = list_items(with_market=True, progress=on_progress)
        unreal = 0.0
        for it in items:
            cur = it.get("current_price")
            if cur is not None:
                unreal += (cur - it["buy_price"]) * it["shares"]
        return round(unreal, 2)
    except Exception as e:
        logger.error(f"comparison: user unrealized failed: {e}")
        return 0.0


def user_daily_net_curve(unreal: Optional[float] = None) -> List[Dict]:
    """用户端按天净利润曲线。

    用户端没有每日库存快照,历史段用「累计已实现盈亏」,
    最后一天点补上「当前未实现盈亏」得到总盈亏口径。
    unreal 可传入已算好的未实现盈亏,避免重复抓取价格。
    返回 [{date, net}] 按日期升序。
    """
    trades = user_trades_list()
    if not trades:
        return []
    cum = OrderedDict()
    running = 0.0
    for t in trades:
        d = t.get("trade_date") or t.get("created_at", "")[:10]
        if not d:
            continue
        running += float(t.get("realized_pnl") or 0.0)
        cum[d] = running
    if not cum:
        return []
    # 最后一天补未实现
    if unreal is None:
        unreal = user_unrealized()
    final_date = list(cum.keys())[-1]
    out = []
    for d, v in cum.items():
        if d == final_date:
            v += unreal
        out.append({"date": d, "net": round(v, 2)})
    return out


# ----------------- 汇总 -----------------

def get_comparison(progress=None) -> Dict:
    """汇总两侧净利润 + 操作对照。

    Args:
        progress: 可选回调(done, total, current),随实时价格抓取进度上报。
    """
    ai_trade_list = ai_trades()
    user_trade_list = user_trades_list()

    # 价格抓取总数 = AI 持仓 + 用户库存,用于进度条
    ai_names = _ai_position_names()
    total = len(ai_names) + _inventory_row_count()
    done = 0

    def _on_price(name: str):
        nonlocal done
        done += 1
        if progress:
            try:
                progress(done, total, name)
            except Exception:
                pass

    ai_realized = round(sum(t["realized_pnl"] for t in ai_trade_list), 2)
    user_realized = round(
        sum(float(t.get("realized_pnl") or 0.0) for t in user_trade_list if t.get("action") == "sell"),
        2,
    )
    ai_unreal = ai_unrealized(on_progress=_on_price)
    user_unreal = user_unrealized(on_progress=_on_price)

    return {
        "ok": True,
        "ai": {
            "realized_pnl": ai_realized,
            "unrealized_pnl": ai_unreal,
            "total_pnl": round(ai_realized + ai_unreal, 2),
            "trade_count": len(ai_trade_list),
            "trades": ai_trade_list,
        },
        "user": {
            "realized_pnl": user_realized,
            "unrealized_pnl": user_unreal,
            "total_pnl": round(user_realized + user_unreal, 2),
            "trade_count": len([t for t in user_trade_list if t.get("action") == "sell"]),
            "trades": user_trade_list,
        },
        "curve": {
            "ai": ai_daily_net_curve(unreal=ai_unreal),
            "user": user_daily_net_curve(unreal=user_unreal),
        },
    }
