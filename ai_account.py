"""AI 端虚拟账户运行器:关注列表 → 分析 → 决策 → 自动记账(全自动)。

分析池 = 关注列表(config/watchlist.yaml)。
决策管线复用 run_single_experiment(technical/vision 分析 → 风控 → 决策,
每个饰品天然多次独立 LLM 请求)。卖出决策由 workflow auto_settle 自动结算。
"""

import threading
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from run import run_single_experiment
from watchlist import load_watchlist
from util.logger import logger

PROJECT_ROOT = Path(__file__).parent
AI_ACCOUNT_CONFIG = PROJECT_ROOT / "config" / "ai_account.yaml"

_run_lock = threading.Lock()
_run_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_ok": None,
    "last_error": None,
    # 进度:按饰品逐个分析,items_done/items_total 供前端进度条使用
    "items_total": 0,
    "items_done": 0,
    "current_item": None,
}


def get_run_status() -> Dict:
    with _run_lock:
        return dict(_run_state)


def run_ai_account(items: Optional[List[str]] = None) -> Dict:
    """全自动跑一轮 AI 虚拟账户。items 缺省时取关注列表。

    本金动态同步:每次运行时用用户库存总成本覆盖 config 中的 cashflow,
    保证 AI 端与用户端本金一致,盈亏对比才有相同基准。
    """
    with _run_lock:
        if _run_state["running"]:
            return {"ok": False, "error": "AI 账户运行中"}
        _run_state.update(
            running=True,
            started_at=datetime.now().isoformat(),
            finished_at=None,
            last_ok=None,
            last_error=None,
            items_total=0,
            items_done=0,
            current_item=None,
        )

    try:
        tickers = items if items is not None else load_watchlist()
        if not tickers:
            with _run_lock:
                _run_state.update(running=False, finished_at=datetime.now().isoformat(),
                                  last_ok=False, last_error="关注列表为空")
            return {"ok": False, "error": "关注列表为空,请先在关注列表添加饰品"}
        with _run_lock:
            _run_state["items_total"] = len(tickers)

        today = datetime.now().strftime("%Y-%m-%d")
        with open(AI_ACCOUNT_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["tickers"] = tickers

        # 本金动态同步 = 用户库存总成本
        try:
            from inventory import list_items
            inv_cost = sum(it["buy_price"] * it["shares"] for it in list_items(with_market=False))
            if inv_cost > 0:
                cfg["cashflow"] = round(inv_cost, 2)
                logger.info(f"ai_account: cashflow synced to inventory cost ¥{cfg['cashflow']}")
        except Exception as e:
            logger.warning(f"ai_account: cashflow sync failed, keep config value: {e}")

        temp_path = PROJECT_ROOT / "config" / "_ai_account.yaml"
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

        results, success = [], 0
        try:
            for item in tickers:
                with _run_lock:
                    _run_state["current_item"] = item
                try:
                    run_single_experiment("config/_ai_account.yaml", today, use_local_db=True)
                    results.append({"item": item, "ok": True})
                    success += 1
                except Exception as e:
                    logger.error(f"ai_account: analysis failed for {item}: {e}")
                    results.append({"item": item, "ok": False, "error": str(e)})
                with _run_lock:
                    _run_state["items_done"] += 1
        finally:
            temp_path.unlink(missing_ok=True)

        summary = {
            "ok": True,
            "total": len(tickers),
            "success": success,
            "failed": len(tickers) - success,
            "results": results,
        }
        failed_names = [r["item"] for r in results if not r.get("ok")]
        with _run_lock:
            _run_state.update(
                running=False,
                finished_at=datetime.now().isoformat(),
                last_ok=success > 0,
                last_error=None if success > 0 else "全部失败",
            )
        # 运行结果推送(失败必推,成功仅推概要)
        _notify_result(summary, failed_names)
        return summary
    except Exception as e:
        logger.error(f"ai_account: run failed: {e}", exc_info=True)
        with _run_lock:
            _run_state.update(running=False, finished_at=datetime.now().isoformat(),
                              last_ok=False, last_error=str(e))
        _notify_result({"ok": False, "error": str(e)}, [], crashed=True)
        return {"ok": False, "error": str(e)}


def _notify_result(summary: Dict, failed_names: List[str], crashed: bool = False) -> None:
    """运行结束后推送结果到微信(Server酱);失败必推。"""
    try:
        from notify import send_wechat
        if crashed:
            send_wechat(
                "AI 持仓运行失败",
                f"AI 账户本轮运行异常中止:\n\n{summary.get('error', '未知错误')}\n\n请打开 AI 持仓页查看详情。",
            )
            return
        if summary.get("failed", 0) > 0:
            title = f"AI 持仓运行完成:成功 {summary['success']}/{summary['total']}"
            detail = "\n".join(f"- {n}: 分析失败" for n in failed_names[:10])
            send_wechat(title, f"有 {summary['failed']} 个饰品分析失败:\n\n{detail}")
        elif summary.get("ok") and summary.get("total", 0) > 0:
            # 全成功:推送完整决策报告(总资产/现金 + 买卖/观望 + 理由),而非只有成功提示
            from notify import generate_report
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                report = generate_report("ai-account", today)
            except Exception as e:
                logger.error(f"ai_account: generate report failed: {e}")
                report = "AI 端虚拟账户已完成一轮分析,可打开 AI 持仓页查看最新决策。"
            send_wechat(f"AI 持仓分析 {today}", report)
    except Exception as e:
        logger.error(f"ai_account: notify failed: {e}")


def run_ai_account_background() -> bool:
    """后台线程运行;返回是否成功启动(已运行则 False)。"""
    with _run_lock:
        if _run_state["running"]:
            return False
    threading.Thread(target=run_ai_account, daemon=True).start()
    return True


def seed_from_inventory(items: Optional[List[str]] = None) -> Dict:
    """将用户端库存选中的饰品导入为 AI 端虚拟账户持仓(合并语义)。

    - 选中的库存饰品写入 AI 持仓(同名覆盖数量/成本/买入批次);AI 原有其他持仓保留。
    - AI 现金相应扣减选中成本,等价于"AI 用账户里的钱购入了这些饰品";
      现金不足则截断为 0(不出现负现金)。
    - 全选导入 = 现金归 0,与旧的"全部转持仓"行为一致。

    Args:
        items: 可选,要导入的饰品名列表;None 表示导入全部库存。

    Returns:
        {"ok": True, "seeded": n, "total_cost": x, "positions": {...}} 或 {"ok": False, ...}
    """
    import json as _json
    from inventory import list_items
    from util.cs2_db_helper import get_cs2_db, cs2_db_initialize

    # 读取用户库存(不加市场价,更快)
    inv_items = list_items(with_market=False)
    if not inv_items:
        return {"ok": False, "error": "库存为空,没有可复制的饰品"}
    if items is not None:
        item_set = set(items)
        inv_items = [it for it in inv_items if it["item_name"] in item_set]
        if not inv_items:
            return {"ok": False, "error": "指定的饰品在库存中不存在"}

    # 确保数据库已初始化
    if get_cs2_db() is None:
        cs2_db_initialize(use_local_db=True)
    db = get_cs2_db()

    # 定位 AI 端 config;不存在则用 ai_account.yaml 创建
    config_id = db.get_config_id_by_name("ai-account")
    if not config_id:
        try:
            with open(AI_ACCOUNT_CONFIG, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            return {"ok": False, "error": f"读取 ai_account.yaml 失败: {e}"}
        cfg["tickers"] = [it["item_name"] for it in inv_items]
        config_id = db.create_config(cfg)
        if not config_id:
            return {"ok": False, "error": "创建 AI 账户配置失败"}

    # 取最新 portfolio;没有则新建(现金 0)
    today = datetime.now()
    portfolio = db.get_latest_portfolio(config_id)
    if not portfolio:
        portfolio = db.create_portfolio(config_id, 0.0, today)
        if not portfolio:
            return {"ok": False, "error": "创建 AI 持仓失败"}

    # AI 端现有持仓与现金(合并语义基础)
    existing_positions = portfolio.get("positions") or {}
    if isinstance(existing_positions, str):
        try:
            existing_positions = _json.loads(existing_positions)
        except (TypeError, ValueError):
            existing_positions = {}
    existing_cash = float(portfolio.get("cashflow") or 0.0)

    # 构建选中的库存持仓:数量=用户数量,成本=用户买入价,价值=成本×数量
    # 同时记录买入批次(7 天交易 CD 依据),同一饰品多行买入时聚合
    positions = {}
    total_cost = 0.0
    for it in inv_items:
        name = it["item_name"]
        shares = int(it.get("shares") or 0)
        buy_price = float(it.get("buy_price") or 0)
        if shares <= 0 or buy_price <= 0:
            continue
        value = round(buy_price * shares, 2)
        pos = positions.setdefault(name, {"value": 0.0, "shares": 0, "avg_cost": 0.0, "lots": []})
        buy_date = str(it.get("buy_date") or "")[:10] or today.isoformat()[:10]
        pos["shares"] += shares
        pos["value"] = round(pos["value"] + value, 2)
        pos["lots"].append({"date": buy_date, "shares": shares})
        if pos["shares"] > 0:
            pos["avg_cost"] = round(
                (pos["avg_cost"] * (pos["shares"] - shares) + buy_price * shares) / pos["shares"], 2
            )
        total_cost += value

    if not positions:
        return {"ok": False, "error": "库存中没有有效持仓"}

    # 合并:选中的覆盖/添加进 AI 持仓,AI 原有其他持仓保留
    merged = dict(existing_positions)
    merged.update(positions)
    # 现金扣减选中成本,等价于 AI 用现金购入;不足则截断为 0
    new_cash = max(0.0, round(existing_cash - total_cost, 2))

    trading_date = portfolio.get("trading_date") or today.isoformat()
    try:
        trading_dt = datetime.fromisoformat(trading_date)
    except ValueError:
        trading_dt = today
    ok = db.update_portfolio(
        config_id,
        {"cashflow": new_cash, "positions": merged},
        trading_dt,
    )
    if not ok:
        return {"ok": False, "error": "更新 AI 持仓失败"}

    logger.info(
        f"ai_account: seeded {len(positions)} position(s) from inventory "
        f"(cash {existing_cash:.2f} -> {new_cash:.2f}, total {len(merged)})"
    )
    return {
        "ok": True,
        "seeded": len(positions),
        "total_cost": round(total_cost, 2),
        "cashflow": new_cash,
        "positions": merged,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_ai_account(), ensure_ascii=False, indent=2, default=str))
