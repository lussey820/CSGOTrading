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
}


def get_run_status() -> Dict:
    with _run_lock:
        return dict(_run_state)


def run_ai_account(items: Optional[List[str]] = None) -> Dict:
    """全自动跑一轮 AI 虚拟账户。items 缺省时取关注列表。"""
    with _run_lock:
        if _run_state["running"]:
            return {"ok": False, "error": "AI 账户运行中"}
        _run_state.update(
            running=True,
            started_at=datetime.now().isoformat(),
            finished_at=None,
            last_ok=None,
            last_error=None,
        )

    try:
        tickers = items if items is not None else load_watchlist()
        if not tickers:
            with _run_lock:
                _run_state.update(running=False, finished_at=datetime.now().isoformat(),
                                  last_ok=False, last_error="关注列表为空")
            return {"ok": False, "error": "关注列表为空,请先在关注列表添加饰品"}

        today = datetime.now().strftime("%Y-%m-%d")
        with open(AI_ACCOUNT_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["tickers"] = tickers
        temp_path = PROJECT_ROOT / "config" / "_ai_account.yaml"
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

        results, success = [], 0
        try:
            for item in tickers:
                try:
                    run_single_experiment("config/_ai_account.yaml", today, use_local_db=True)
                    results.append({"item": item, "ok": True})
                    success += 1
                except Exception as e:
                    logger.error(f"ai_account: analysis failed for {item}: {e}")
                    results.append({"item": item, "ok": False, "error": str(e)})
        finally:
            temp_path.unlink(missing_ok=True)

        with _run_lock:
            _run_state.update(
                running=False,
                finished_at=datetime.now().isoformat(),
                last_ok=success > 0,
                last_error=None if success > 0 else "全部失败",
            )
        return {
            "ok": True,
            "total": len(tickers),
            "success": success,
            "failed": len(tickers) - success,
            "results": results,
        }
    except Exception as e:
        logger.error(f"ai_account: run failed: {e}", exc_info=True)
        with _run_lock:
            _run_state.update(running=False, finished_at=datetime.now().isoformat(),
                              last_ok=False, last_error=str(e))
        return {"ok": False, "error": str(e)}


def run_ai_account_background() -> bool:
    """后台线程运行;返回是否成功启动(已运行则 False)。"""
    with _run_lock:
        if _run_state["running"]:
            return False
    threading.Thread(target=run_ai_account, daemon=True).start()
    return True


if __name__ == "__main__":
    import json
    print(json.dumps(run_ai_account(), ensure_ascii=False, indent=2, default=str))
