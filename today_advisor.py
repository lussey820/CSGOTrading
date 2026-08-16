"""Today's advisor - screenshot warmup + trigger analysis for inventory items.

Flow:
    1. warmup_screenshots():
        - 读 cs2_screenshot_cache 表,5 小时内已 done 且文件存在的饰品跳过。
        - 其余饰品走并行截图(默认 3 并发,共享一个浏览器)。
        - 结果以 UUID 为 id 写入/更新 cs2_screenshot_cache 表。
        - 短时间内重启后端会命中缓存,无需再次预热;超过 5 小时则重新截图覆盖。
    2. run_today_advisor(): 对每个库存饰品调 run_single_experiment,
       portfolio_manager 的 Buy/Sell/Hold 映射为持仓语义:
           Buy  + 持仓>0 → "补仓"
           Sell          → "离场"
           Hold          → "继续持有"
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from apis.cs2market.chart_screenshot import (
    DEFAULT_SCREENSHOT_DIR,
    capture_chart_screenshots,
    capture_chart_screenshots_batch,
    _safe_filename,
)
from database.cs2_sqlite_setup import CS2_DB_PATH
from inventory import list_item_names
from util.logger import logger
from util import screenshot_cache as sc

load_dotenv()

PROJECT_ROOT = Path(__file__).parent
LIVE_CONFIG = PROJECT_ROOT / "config" / "live.yaml"

# ----------------- 可调参数 -----------------
# TTL 统一在 util/screenshot_cache 中定义,这里只暴露给旧接口。
WARMUP_TTL_SECONDS = sc.WARMUP_TTL_SECONDS
WARMUP_CONCURRENCY = int(os.getenv("WARMUP_CONCURRENCY", "3"))
WARMUP_TIMEOUT_MS = 30000
WARMUP_RENDER_DELAY_MS = 2000
# --------------------------------------------

# 内存状态,给前端轮询使用
_warmup_lock = threading.Lock()
_warmup_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "items_total": 0,
    "items_done": 0,
    "items_skipped": 0,
    "current_item": None,
    "result": None,
    "error": None,
}


# ----------------- cache wrappers -----------------

# 统一 TTL 定义在 util/screenshot_cache 中,这里只导出别名保持旧代码可读。
_read_cache_row = sc.read_cache_row
_is_cache_fresh = sc.is_cache_fresh
_upsert_cache_row = sc.upsert_cache_row


def _conn():
    """SQLite 连接,供 get_today_advisor_results 查询 cs2_decision / cs2_signal。"""
    import sqlite3

    conn = sqlite3.connect(CS2_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------- warmup -----------------

def _paths_for(item_name: str) -> Dict[str, str]:
    safe = _safe_filename(item_name)
    return {
        "kline": str(DEFAULT_SCREENSHOT_DIR / f"{safe}_kline.png"),
        "line": str(DEFAULT_SCREENSHOT_DIR / f"{safe}_line.png"),
    }


def warmup_screenshots(
    items: Optional[List[str]] = None,
    force: bool = False,
) -> Dict:
    """预热库存饰品的 K 线 + 走势图截图。

    - 5 小时内已有 done 缓存且文件存在 → 跳过
    - 其余走并行批量截图(默认并发 3)
    - 结果写 cs2_screenshot_cache

    Args:
        items: 要预热的饰品列表;None 则从库存读
        force: True 则忽略缓存,全部重新截图

    Returns:
        {total, done, skipped, failed, items: {name: "done"|"skipped"|"error"}}
    """
    if items is None:
        items = list_item_names()

    with _warmup_lock:
        if _warmup_state["running"]:
            return {"ok": False, "error": "warmup already running"}
        _warmup_state.update(
            running=True,
            started_at=datetime.now().isoformat(),
            finished_at=None,
            items_total=len(items),
            items_done=0,
            items_skipped=0,
            current_item=None,
            result=None,
            error=None,
        )

    try:
        DEFAULT_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

        # 1. 划分需要截图 vs 可跳过
        to_capture: List[str] = []
        per_item: Dict[str, str] = {}
        for item in items:
            row = None if force else _read_cache_row(item)
            if _is_cache_fresh(row):
                per_item[item] = "skipped"
                _warmup_state["items_skipped"] += 1
            else:
                to_capture.append(item)
                per_item[item] = "pending"

        logger.info(
            f"warmup: total={len(items)} skip={len(items) - len(to_capture)} "
            f"to_capture={len(to_capture)} concurrency={WARMUP_CONCURRENCY}"
        )

        # 2. 并行截图
        if to_capture:
            # 先把 pending 项落库,便于状态接口读到
            for item in to_capture:
                _upsert_cache_row(item, status="pending")

            results = capture_chart_screenshots_batch(
                to_capture,
                output_dir=str(DEFAULT_SCREENSHOT_DIR),
                concurrency=WARMUP_CONCURRENCY,
                timeout_ms=WARMUP_TIMEOUT_MS,
                render_delay_ms=WARMUP_RENDER_DELAY_MS,
            )

            for item in to_capture:
                _warmup_state["current_item"] = item
                res = results.get(item) or {}
                kline = res.get("kline")
                line = res.get("line")
                ok = bool(kline or line)
                if ok:
                    per_item[item] = "done"
                    _upsert_cache_row(
                        item, status="done",
                        kline_path=kline, line_path=line,
                        error_msg=None,
                    )
                else:
                    per_item[item] = "error"
                    _upsert_cache_row(
                        item, status="error",
                        error_msg="screenshot capture returned no images",
                    )
                _warmup_state["items_done"] += 1
        else:
            # 全命中缓存,把 per_item 里 skipped 计为已完成
            _warmup_state["items_done"] = len(items)

        done_count = sum(1 for v in per_item.values() if v in ("done", "skipped"))
        failed_count = sum(1 for v in per_item.values() if v == "error")

        summary = {
            "ok": True,
            "total": len(items),
            "done": done_count,
            "captured": len(to_capture),
            "skipped": len(items) - len(to_capture),
            "failed": failed_count,
            "items": per_item,
        }
        _warmup_state.update(
            running=False,
            finished_at=datetime.now().isoformat(),
            current_item=None,
            result=summary,
        )
        logger.info(
            f"warmup finished: done={done_count} captured={len(to_capture)} "
            f"skipped={summary['skipped']} failed={failed_count}"
        )
        return summary
    except Exception as e:
        logger.error(f"warmup failed: {e}", exc_info=True)
        _warmup_state.update(
            running=False,
            finished_at=datetime.now().isoformat(),
            error=str(e),
        )
        return {"ok": False, "error": str(e)}


def warmup_screenshots_background(
    items: Optional[List[str]] = None,
    force: bool = False,
) -> bool:
    """后台线程触发预热。如果已经在跑就返回 False。"""
    with _warmup_lock:
        if _warmup_state["running"]:
            return False
    threading.Thread(
        target=warmup_screenshots, args=(items, force), daemon=True
    ).start()
    return True


def get_warmup_state() -> Dict:
    with _warmup_lock:
        return dict(_warmup_state)


def warmup_status_summary() -> Dict:
    """给 UI 用的摘要:结合 DB 缓存和内存运行状态。"""
    items = list_item_names()
    done = pending = errors = skipped = 0
    per_item = []
    for item in items:
        row = _read_cache_row(item)
        fresh = _is_cache_fresh(row)
        if fresh:
            status = "done"
            done += 1
        elif row and row.get("status") == "error":
            status = "error"
            errors += 1
        elif _warmup_state["running"] and row and row.get("status") == "pending":
            status = "pending"
            pending += 1
        else:
            # 还没预热过(或缓存过期)
            status = "pending"
            pending += 1
        per_item.append({
            "item": item,
            "status": status,
            "updated_at": (row or {}).get("updated_at"),
        })

    # 若缓存仍在 TTL 内则 ready=True;运行中也算"还没就绪"
    ready = (
        pending == 0
        and errors == 0
        and len(items) > 0
        and not _warmup_state["running"]
    )
    return {
        "total": len(items),
        "done": done,
        "pending": pending,
        "errors": errors,
        "skipped": skipped,
        "items": per_item,
        "ready": ready,
        "running": _warmup_state["running"],
        "ttl_seconds": WARMUP_TTL_SECONDS,
        "state": get_warmup_state(),
    }


# 兼容旧接口
def load_warmup_status() -> Dict[str, str]:
    """Legacy JSON status loader (kept for back-compat)."""
    return {it["item"]: it["status"] for it in warmup_status_summary()["items"]}


def _is_screenshot_fresh(item_name: str) -> bool:
    """Legacy helper: file-mtime based check. Now delegates to DB cache."""
    return _is_cache_fresh(_read_cache_row(item_name))


# ----------------- run advisor -----------------

_run_lock = threading.Lock()
_run_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "phase": None,  # "warmup" | "analysis" | None
    "current_item": None,
    "items_done": 0,
    "items_total": 0,
    "result": None,
    "error": None,
}


def _reset_run_state(total: int):
    _run_state.update(
        running=True,
        started_at=datetime.now().isoformat(),
        finished_at=None,
        phase=None,
        current_item=None,
        items_done=0,
        items_total=total,
        result=None,
        error=None,
    )


def _finish_run_state(ok: bool, error: Optional[str] = None):
    _run_state.update(
        running=False,
        finished_at=datetime.now().isoformat(),
        phase=None,
        current_item=None,
        result="success" if ok else "error",
        error=error,
    )


def _map_decision_to_holding(action: str, shares_held: int) -> str:
    """Buy+持仓>0→补仓, Buy+0→建仓, Sell→离场, Hold→继续持有"""
    a = (action or "").strip().lower()
    if a == "buy":
        return "补仓" if shares_held > 0 else "建仓"
    if a == "sell":
        return "离场"
    return "继续持有"


def run_today_advisor(items: Optional[List[str]] = None) -> Dict:
    import yaml
    from run import run_single_experiment

    if items is None:
        items = list_item_names()
    today = datetime.now().strftime("%Y-%m-%d")

    with _run_lock:
        if _run_state["running"]:
            return {"ok": False, "error": "advisor already running"}
        _reset_run_state(len(items))

    results = []
    success_count = 0
    try:
        # 1. warmup phase (uses cache + parallel)
        _run_state["phase"] = "warmup"
        warmup_res = warmup_screenshots(items)
        logger.info(
            f"today_advisor: warmup captured={warmup_res.get('captured')} "
            f"skipped={warmup_res.get('skipped')} failed={warmup_res.get('failed')}"
        )

        # 2. analysis phase
        _run_state["phase"] = "analysis"
        with open(LIVE_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["tickers"] = items
        # 用真实库存持仓覆盖模拟组合,让决策引擎看到真实持仓
        cfg["seed_from_inventory"] = True
        temp_path = PROJECT_ROOT / "config" / "_today_advisor.yaml"
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        try:
            for item in items:
                _run_state["current_item"] = item
                try:
                    run_single_experiment(
                        "config/_today_advisor.yaml", today, use_local_db=True
                    )
                    results.append({"item": item, "ok": True})
                    success_count += 1
                except Exception as e:
                    logger.error(f"today_advisor: analysis failed for {item}: {e}")
                    results.append({"item": item, "ok": False, "error": str(e)})
                _run_state["items_done"] += 1
        finally:
            temp_path.unlink(missing_ok=True)

        _finish_run_state(ok=True)
        return {
            "ok": True,
            "total": len(items),
            "success": success_count,
            "failed": len(items) - success_count,
            "results": results,
        }
    except Exception as e:
        logger.error(f"today_advisor: run failed: {e}", exc_info=True)
        _finish_run_state(ok=False, error=str(e))
        return {"ok": False, "error": str(e)}


def run_today_advisor_background():
    with _run_lock:
        if _run_state["running"]:
            return False
    threading.Thread(target=run_today_advisor, daemon=True).start()
    return True


def get_run_status() -> Dict:
    with _run_lock:
        return dict(_run_state)


# ----------------- read today's decisions for inventory items -----------------

def get_today_advisor_results() -> Dict:
    """Return today's latest decisions + signals for inventory items,
    with holding-semantics mapping applied.

    只取用户端(live-advisor)的决策,不含 AI 端虚拟账户(ai-account),
    并取每个饰品最新的一条,避免同饰品多条决策导致展示反复变化。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    items = list_item_names()
    from inventory import list_items
    inv_by_name = {it["item_name"]: it for it in list_items(with_market=False)}
    try:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.item_name, d.action, d.quantity, d.price,
                       d.justification, d.trading_date, d.updated_at,
                       (SELECT s.signal FROM cs2_signal s
                        WHERE s.portfolio_id = d.portfolio_id
                          AND s.item_name = d.item_name
                        ORDER BY s.updated_at DESC LIMIT 1) AS latest_signal
                FROM cs2_decision d
                JOIN cs2_portfolio p ON p.id = d.portfolio_id
                JOIN cs2_config c ON c.id = p.config_id
                WHERE c.exp_name = 'live-advisor'
                  AND DATE(d.trading_date) = DATE(?)
                  AND d.item_name IN (
                      SELECT DISTINCT item_name FROM cs2_inventory
                  )
                  AND d.id = (
                      -- 同一饰品取最新一条决策
                      SELECT d2.id FROM cs2_decision d2
                      JOIN cs2_portfolio p2 ON p2.id = d2.portfolio_id
                      JOIN cs2_config c2 ON c2.id = p2.config_id
                      WHERE c2.exp_name = 'live-advisor'
                        AND d2.item_name = d.item_name
                        AND DATE(d2.trading_date) = DATE(?)
                      ORDER BY d2.updated_at DESC
                      LIMIT 1
                  )
                ORDER BY d.item_name
                """,
                (today, today),
            ).fetchall()
    except Exception as e:
        logger.error(f"today_advisor: get_results failed: {e}")
        rows = []

    decisions = []
    for r in rows:
        item_name = r["item_name"]
        shares_held = inv_by_name.get(item_name, {}).get("shares", 0)
        holding = _map_decision_to_holding(r["action"] or "", shares_held)
        decisions.append(
            {
                "id": r["id"],
                "item_name": item_name,
                "item_name_cn": (
                    inv_by_name.get(item_name, {}).get("item_name_cn") or item_name
                ),
                "action": r["action"],
                "holding_action": holding,
                "shares": r["quantity"],
                "price": float(r["price"]) if r["price"] is not None else None,
                "justification": r["justification"],
                "signal": r["latest_signal"],
                "trading_date": (
                    r["trading_date"][:10] if r["trading_date"] else today
                ),
            }
        )

    decided_items = {d["item_name"] for d in decisions}
    pending = [
        {
            "item_name": name,
            "item_name_cn": inv_by_name[name].get("item_name_cn") or name,
            "shares": inv_by_name[name].get("shares", 0),
        }
        for name in items
        if name not in decided_items
    ]
    return {
        "date": today,
        "decisions": decisions,
        "pending": pending,
        "run_status": get_run_status(),
        "warmup": warmup_status_summary(),
    }


# ----------------- CLI -----------------

def _cli():
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python today_advisor.py warmup [--force]  # 并行截图(命中缓存则跳过)")
        print("  python today_advisor.py status            # 查看缓存/预热状态")
        print("  python today_advisor.py run               # warmup + 分析")
        print("  python today_advisor.py results           # 查看今日决策")
        return

    cmd = sys.argv[1].lower()
    if cmd == "warmup":
        force = "--force" in sys.argv
        t0 = time.time()
        res = warmup_screenshots(force=force)
        res["elapsed_seconds"] = round(time.time() - t0, 2)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif cmd == "status":
        print(json.dumps(warmup_status_summary(), ensure_ascii=False, indent=2, default=str))
    elif cmd == "run":
        res = run_today_advisor()
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif cmd == "results":
        print(json.dumps(get_today_advisor_results(), ensure_ascii=False, indent=2, default=str))
    else:
        print("Unknown command. See usage with no args.")


if __name__ == "__main__":
    _cli()
