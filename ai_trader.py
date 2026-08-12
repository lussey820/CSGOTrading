"""AI trader - track realized P&L from AI Sell decisions.

When the portfolio_manager outputs a Sell decision, we treat it as the AI
exiting the position at the current SteamDT real-time price. Realized P&L:
    realized_pnl = (sell_price - buy_price) * shares - fee
    fee          = sell_price * shares * 2%   (2% sell fee, per PORTFOLIO_PROMPT)

Records are stored in cs2_ai_trade. The total realized P&L is the sum of
realized_pnl across all Sell trades.

This module is invoked after a decision is made (e.g. after
run_single_experiment), not during the pipeline itself, to keep the
pipeline's simulated portfolio semantics unchanged.
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from database.cs2_sqlite_setup import CS2_DB_PATH
from apis.cs2market.web_scraper import fetch_item_market_data
from inventory import list_items, _extract_current_price
from util.logger import logger

TRANSACTION_FEE_RATE = 0.02  # 2% sell fee (matches portfolio_manager.py)


def _conn():
    conn = sqlite3.connect(CS2_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------- record realized P&L from a decision -----------------

def record_sell_from_decision(
    decision_id: str,
    item_name: str,
    shares: int,
    trade_date: str,
    buy_price: Optional[float] = None,
    sell_price: Optional[float] = None,
) -> Optional[Dict]:
    """Record an AI trade row for a Sell decision.

    Args:
        decision_id: the cs2_decision.id that triggered this trade
        item_name: the CS2 item
        shares: number of units sold (decision.quantity)
        trade_date: YYYY-MM-DD
        buy_price: optional override; if None, looked up from inventory
        sell_price: optional override; if None, fetched from SteamDT

    Returns: the inserted row as dict, or None on failure.
    """
    if shares <= 0:
        logger.warning(f"ai_trader: shares<=0, skipping sell record for {item_name}")
        return None

    # Resolve buy_price from inventory if not provided
    if buy_price is None:
        inv_items = list_items(with_market=False)
        buy_price = next(
            (it["buy_price"] for it in inv_items if it["item_name"] == item_name),
            None,
        )
    if buy_price is None or buy_price <= 0:
        logger.error(f"ai_trader: no buy_price for {item_name}, skipping")
        return None

    # Resolve sell_price from SteamDT if not provided
    if sell_price is None:
        try:
            md = fetch_item_market_data(item_name)
        except Exception as e:
            logger.error(f"ai_trader: fetch sell price failed for {item_name}: {e}")
            return None
        sell_price = _extract_current_price(md)
    if sell_price is None or sell_price <= 0:
        logger.error(f"ai_trader: no sell_price for {item_name}, skipping")
        return None

    gross = (sell_price - buy_price) * shares
    fee = round(sell_price * shares * TRANSACTION_FEE_RATE, 2)
    realized_pnl = round(gross - fee, 2)

    trade_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO cs2_ai_trade
                    (id, item_name, action, sell_price, buy_price, shares,
                     fee, realized_pnl, decision_id, trade_date, created_at)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id, item_name, sell_price, buy_price, shares,
                    fee, realized_pnl, decision_id, trade_date, now,
                ),
            )
            # also persist realized_pnl on the decision row for convenience
            conn.execute(
                "UPDATE cs2_decision SET realized_pnl = ? WHERE id = ?",
                (realized_pnl, decision_id),
            )
            conn.commit()
        logger.info(
            f"ai_trader: recorded sell {item_name} x{shares} "
            f"@ {sell_price} (buy {buy_price}) → pnl {realized_pnl}"
        )
        return {
            "id": trade_id,
            "item_name": item_name,
            "action": "sell",
            "sell_price": sell_price,
            "buy_price": buy_price,
            "shares": shares,
            "fee": fee,
            "realized_pnl": realized_pnl,
            "decision_id": decision_id,
            "trade_date": trade_date,
        }
    except Exception as e:
        logger.error(f"ai_trader: insert failed: {e}")
        return None


def auto_settle_sell(
    item_name: str,
    shares: int,
    sell_price: float,
    trade_date: str,
    avg_cost: float,
) -> Optional[Dict]:
    """AI 端:卖出决策落地后自动结算已实现盈亏(成本取虚拟持仓加权成本)。

    Args:
        item_name: 饰品名
        shares: 实际卖出数量
        sell_price: 结算价(决策时的实时价)
        trade_date: YYYY-MM-DD
        avg_cost: 虚拟持仓的加权平均成本

    Returns: record_sell_from_decision 的结果,失败返回 None。
    """
    if shares <= 0 or sell_price is None or sell_price <= 0:
        logger.warning(f"ai_trader: auto_settle skipped for {item_name} (shares={shares}, price={sell_price})")
        return None
    # 关联最近的 Sell 决策(用于回写 realized_pnl)
    decision_id = None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT id FROM cs2_decision WHERE item_name = ? AND action = 'sell' "
                "ORDER BY updated_at DESC LIMIT 1",
                (item_name,),
            ).fetchone()
            if row:
                decision_id = row["id"]
    except Exception as e:
        logger.error(f"ai_trader: auto_settle decision lookup failed for {item_name}: {e}")
    return record_sell_from_decision(
        decision_id=decision_id,
        item_name=item_name,
        shares=shares,
        trade_date=trade_date,
        buy_price=avg_cost,
        sell_price=sell_price,
    )


# ----------------- query AI trader state -----------------

def _virtual_holdings() -> List[Dict]:
    """读 AI 虚拟账户(exp_name=ai-account)最新持仓,拉实时价算未实现盈亏。

    返回元素:item_name, item_name_cn, shares, buy_price(=avg_cost),
    current_price, unrealized_pnl, unrealized_pnl_pct。
    """
    try:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT p.positions FROM cs2_portfolio p
                JOIN cs2_config c ON c.id = p.config_id
                WHERE c.exp_name = 'ai-account'
                ORDER BY p.updated_at DESC LIMIT 1
                """
            ).fetchone()
    except Exception as e:
        logger.error(f"ai_trader: load virtual portfolio failed: {e}")
        row = None
    if not row:
        return []

    import json
    try:
        positions = json.loads(row["positions"])
    except (TypeError, ValueError):
        positions = {}

    holdings = []
    for name, pos in positions.items():
        shares = int(pos.get("shares") or 0)
        if shares <= 0:
            continue
        avg_cost = float(pos.get("avg_cost") or 0.0)
        cur = None
        try:
            md = fetch_item_market_data(name)
            cur = _extract_current_price(md)
        except Exception as e:
            logger.error(f"ai_trader: price fetch failed for {name}: {e}")
        unreal = round((cur - avg_cost) * shares, 2) if cur is not None else None
        unreal_pct = (
            round((cur - avg_cost) / avg_cost * 100.0, 2)
            if cur is not None and avg_cost > 0 else None
        )
        holdings.append(
            {
                "item_name": name,
                "item_name_cn": name,
                "shares": shares,
                "buy_price": avg_cost,
                "current_price": cur,
                "unrealized_pnl": unreal,
                "unrealized_pnl_pct": unreal_pct,
            }
        )

    # 中文名映射(缺失回退英文)
    cn_map = {it["item_name"]: (it.get("item_name_cn") or it["item_name"]) for it in list_items(with_market=False)}
    for h in holdings:
        h["item_name_cn"] = cn_map.get(h["item_name"], h["item_name"])
    return holdings


def get_ai_trader_summary() -> Dict:
    """Return the AI trader overview: per-item trades + cumulative P&L."""
    try:
        with _conn() as conn:
            trades = conn.execute(
                """
                SELECT id, item_name, action, sell_price, buy_price, shares,
                       fee, realized_pnl, decision_id, trade_date, created_at
                FROM cs2_ai_trade
                ORDER BY trade_date DESC, created_at DESC
                """
            ).fetchall()
    except Exception as e:
        logger.error(f"ai_trader: list trades failed: {e}")
        trades = []

    # 中文名映射:从库存表取官方中文名(缺失时回退英文名)
    inv_items = list_items(with_market=False)
    cn_map = {
        it["item_name"]: (it.get("item_name_cn") or it["item_name"])
        for it in inv_items
    }

    trade_list = []
    for r in trades:
        trade_list.append(
            {
                "id": r["id"],
                "item_name": r["item_name"],
                "item_name_cn": cn_map.get(r["item_name"], r["item_name"]),
                "action": r["action"],
                "sell_price": float(r["sell_price"]) if r["sell_price"] is not None else None,
                "buy_price": float(r["buy_price"]) if r["buy_price"] is not None else None,
                "shares": int(r["shares"]) if r["shares"] is not None else 0,
                "fee": float(r["fee"]) if r["fee"] is not None else 0.0,
                "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else 0.0,
                "decision_id": r["decision_id"],
                "trade_date": r["trade_date"][:10] if r["trade_date"] else "",
                "created_at": r["created_at"],
            }
        )

    # per-item aggregation
    per_item: Dict[str, Dict] = {}
    total_realized = 0.0
    total_fee = 0.0
    for t in trade_list:
        name = t["item_name"]
        agg = per_item.setdefault(
            name,
            {
                "item_name": name,
                "item_name_cn": cn_map.get(name, name),
                "trade_count": 0,
                "total_shares_sold": 0,
                "total_realized_pnl": 0.0,
                "total_fee": 0.0,
                "last_trade_date": "",
            },
        )
        agg["trade_count"] += 1
        agg["total_shares_sold"] += t["shares"]
        agg["total_realized_pnl"] += t["realized_pnl"]
        agg["total_fee"] += t["fee"]
        if t["trade_date"] > agg["last_trade_date"]:
            agg["last_trade_date"] = t["trade_date"]
        total_realized += t["realized_pnl"]
        total_fee += t["fee"]

    # round aggregated values
    for agg in per_item.values():
        agg["total_realized_pnl"] = round(agg["total_realized_pnl"], 2)
        agg["total_fee"] = round(agg["total_fee"], 2)
    per_item_list = sorted(per_item.values(), key=lambda x: x["item_name"])

    # AI 虚拟账户持仓 + 未实现盈亏(虚拟账户,非真实库存)
    holdings = _virtual_holdings()
    total_unrealized = 0.0
    for h in holdings:
        total_unrealized += h["unrealized_pnl"] or 0.0

    return {
        "total_realized_pnl": round(total_realized, 2),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "total_pnl": round(total_realized + total_unrealized, 2),
        "total_fee_paid": round(total_fee, 2),
        "trade_count": len(trade_list),
        "per_item": per_item_list,
        "trades": trade_list,
        "holdings": holdings,
    }


# ----------------- CLI -----------------

def _cli():
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python ai_trader.py summary           # show AI trader P&L")
        print(
            "  python ai_trader.py record-sell <decision_id> <item_name> "
            "<shares> <trade_date> [buy_price] [sell_price]"
        )
        return

    cmd = sys.argv[1].lower()
    if cmd == "summary":
        print(json.dumps(get_ai_trader_summary(), ensure_ascii=False, indent=2, default=str))
    elif cmd == "record-sell" and len(sys.argv) >= 6:
        decision_id = sys.argv[2]
        item_name = sys.argv[3]
        shares = int(sys.argv[4])
        trade_date = sys.argv[5]
        buy_price = float(sys.argv[6]) if len(sys.argv) > 6 else None
        sell_price = float(sys.argv[7]) if len(sys.argv) > 7 else None
        row = record_sell_from_decision(
            decision_id, item_name, shares, trade_date, buy_price, sell_price
        )
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str) if row else "failed")
    else:
        print("Unknown command. See usage with no args.")


if __name__ == "__main__":
    _cli()
