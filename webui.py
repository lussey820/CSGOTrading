"""Local Web Dashboard for CS2 Trading Advisor.

Routes:
  /            Dashboard home (latest report + overview)
  /watchlist   Watchlist management
  /positions   Positions & net value curve
  /api/...     JSON APIs for AJAX

Run:
  python webui.py              # default http://127.0.0.1:5000
  python webui.py --port 8080  # custom port
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from database.cs2_sqlite_setup import CS2_DB_PATH
from watchlist import load_watchlist, save_watchlist, verify_item
from apis.cs2market.chart_screenshot import DEFAULT_SCREENSHOT_DIR, _safe_filename
from apis.ocr import recognize_item_name
from apis.cs2market.item_search import search_items
from util.logger import logger
import inventory as inventory_mod
import today_advisor as advisor_mod
import ai_trader as ai_trader_mod
import ai_account as ai_account_mod

PROJECT_ROOT = Path(__file__).parent
LIVE_EXP_NAME = "live-advisor"

app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))

# Global state for tracking background analysis runs
_run_lock = threading.Lock()
_run_state = {"running": False, "started_at": None, "result": None, "error": None}


# ----------------- DB helpers -----------------

def _conn():
    # isolation_level=None => autocommit; closing() ensures conn.close() on exit
    # (avoids sqlite3 with-stmt issuing COMMIT that could trip IntegrityError).
    conn = sqlite3.connect(CS2_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _get_config_id(exp_name=LIVE_EXP_NAME):
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT id FROM cs2_config WHERE exp_name = ?", (exp_name,)
        ).fetchone()
        return row["id"] if row else None


def _list_experiments():
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT exp_name, updated_at, llm_provider, llm_model, has_planner "
            "FROM cs2_config ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _portfolio_history(exp_name=LIVE_EXP_NAME):
    """Return list of {date, cash, total_assets, positions} ordered by date."""
    config_id = _get_config_id(exp_name)
    if not config_id:
        return []
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT trading_date, cashflow, total_assets, positions "
            "FROM cs2_portfolio WHERE config_id = ? ORDER BY trading_date ASC",
            (config_id,),
        ).fetchall()
        return [
            {
                "date": r["trading_date"][:10] if r["trading_date"] else "",
                "cash": float(r["cashflow"]),
                "total_assets": float(r["total_assets"]),
                "positions": json.loads(r["positions"]) if r["positions"] else {},
            }
            for r in rows
        ]


def _latest_portfolio(exp_name=LIVE_EXP_NAME):
    config_id = _get_config_id(exp_name)
    if not config_id:
        return None
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT id, trading_date, cashflow, total_assets, positions "
            "FROM cs2_portfolio WHERE config_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (config_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "date": row["trading_date"][:10] if row["trading_date"] else "",
            "cash": float(row["cashflow"]),
            "total_assets": float(row["total_assets"]),
            "positions": json.loads(row["positions"]) if row["positions"] else {},
        }


def _decisions_for_portfolio(portfolio_id):
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT item_name, action, quantity, price, justification "
            "FROM cs2_decision WHERE portfolio_id = ? ORDER BY item_name",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _signals_for_portfolio(portfolio_id):
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT item_name, analyst, signal, justification "
            "FROM cs2_signal WHERE portfolio_id = ? "
            "ORDER BY item_name, analyst",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _portfolio_for_date(date_str, exp_name=LIVE_EXP_NAME):
    """Return portfolio row matching given date (YYYY-MM-DD)."""
    config_id = _get_config_id(exp_name)
    if not config_id:
        return None
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT id, trading_date, cashflow, total_assets, positions "
            "FROM cs2_portfolio WHERE config_id = ? AND DATE(trading_date) = DATE(?) "
            "ORDER BY updated_at DESC LIMIT 1",
            (config_id, date_str),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "date": row["trading_date"][:10] if row["trading_date"] else "",
            "cash": float(row["cashflow"]),
            "total_assets": float(row["total_assets"]),
            "positions": json.loads(row["positions"]) if row["positions"] else {},
        }


def _report_history(exp_name=LIVE_EXP_NAME, limit=30):
    """List available report dates (desc)."""
    config_id = _get_config_id(exp_name)
    if not config_id:
        return []
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT DATE(trading_date) AS d "
            "FROM cs2_portfolio WHERE config_id = ? "
            "ORDER BY d DESC LIMIT ?",
            (config_id, int(limit)),
        ).fetchall()
        return [r["d"] for r in rows]


# ----------------- Background analysis -----------------

def _run_analysis_background():
    """Run daily.run_today() in a background thread; update _run_state."""
    from daily import run_today
    from util.logger import logger

    with _run_lock:
        if _run_state["running"]:
            return
        _run_state.update(
            running=True, started_at=datetime.now().isoformat(),
            result=None, error=None,
        )

    def _work():
        try:
            ok = run_today()
            with _run_lock:
                _run_state["result"] = "success" if ok else "no-action"
                _run_state["running"] = False
            logger.info("Background analysis finished")
        except Exception as e:
            logger.error(f"Background analysis failed: {e}")
            with _run_lock:
                _run_state["error"] = str(e)
                _run_state["running"] = False

    threading.Thread(target=_work, daemon=True).start()


# ----------------- Routes -----------------

@app.route("/")
def dashboard():
    # 仪表盘已删除,默认跳转到我的库存
    return redirect(url_for("inventory_page"))


@app.route("/watchlist")
def watchlist_page():
    return render_template("watchlist.html", watchlist=load_watchlist())


@app.route("/positions")
def positions_page():
    # 持仓与净值页面基于真实库存展示(不再使用实验模拟资金)
    try:
        inv_items = inventory_mod.list_items(with_market=True)
        inv_total_value = sum(
            (it.get("current_price") or 0) * it["shares"]
            for it in inv_items if it.get("current_price") is not None
        )
        inv_total_cost = sum(it["buy_price"] * it["shares"] for it in inv_items)
        inv_total_pnl = sum((it.get("total_pnl") or 0) for it in inv_items)
    except Exception as e:
        logger.error(f"positions_page: inventory value failed: {e}")
        inv_items = []
        inv_total_value = None
        inv_total_cost = None
        inv_total_pnl = None
    return render_template(
        "positions.html",
        inv_items=inv_items,
        inv_total_value=inv_total_value,
        inv_total_cost=inv_total_cost,
        inv_total_pnl=inv_total_pnl,
        inv_count=len(inv_items),
    )


@app.route("/inventory")
def inventory_page():
    return render_template("inventory.html")


@app.route("/today-advisor")
def today_advisor_page():
    return render_template("today_advisor.html")


@app.route("/ai-trader")
def ai_trader_page():
    return render_template("ai_trader.html")


# ----------------- JSON APIs -----------------

@app.route("/api/scan/recognize", methods=["POST"])
def api_scan_recognize():
    """扫码识别:上传饰品图片 → 阿里 OCR 识别名称 → SteamDT 搜索匹配候选。

    body: multipart/form-data,字段 image=图片文件
    """
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择要识别的图片"}), 400
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"ok": False, "error": "图片内容为空"}), 400
    if len(image_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "图片过大(超过 10MB)"}), 400
    try:
        query = recognize_item_name(image_bytes)
    except Exception as e:
        logger.error(f"scan: OCR failed: {e}")
        return jsonify({"ok": False, "error": f"文字识别失败: {e}"}), 500
    if not query:
        return jsonify({"ok": False, "error": "未识别到饰品名称,请换一张更清晰的图片"}), 422
    try:
        candidates = search_items(query)
    except Exception as e:
        logger.error(f"scan: search failed: {e}")
        return jsonify({"ok": False, "query": query, "error": f"搜索失败: {e}"}), 502
    return jsonify({"ok": True, "query": query, "candidates": candidates})


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    return jsonify({"tickers": load_watchlist()})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    tickers = load_watchlist()
    if name in tickers:
        # Idempotent: already in watchlist, return success
        return jsonify({"ok": True, "already_existed": True, "tickers": tickers})
    # Verify item exists on Steam market
    try:
        if not verify_item(name):
            return jsonify(
                {"ok": False, "error": "market data not found, check item name"}
            ), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"verify failed: {e}"}), 500
    tickers.append(name)
    save_watchlist(tickers)
    return jsonify({"ok": True, "tickers": tickers})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    tickers = load_watchlist()
    if name not in tickers:
        return jsonify({"ok": False, "error": "not in watchlist"}), 404
    tickers.remove(name)
    save_watchlist(tickers)
    return jsonify({"ok": True, "tickers": tickers})


@app.route("/api/watchlist/verify", methods=["POST"])
def api_watchlist_verify():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        valid = verify_item(name)
        return jsonify({"ok": True, "valid": valid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/run-analysis", methods=["POST"])
def api_run_analysis():
    with _run_lock:
        if _run_state["running"]:
            return jsonify(
                {"ok": False, "error": "analysis already running", "status": "running"}
            ), 409
    _run_analysis_background()
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/run-status", methods=["GET"])
def api_run_status():
    with _run_lock:
        return jsonify(
            {
                "running": _run_state["running"],
                "started_at": _run_state["started_at"],
                "result": _run_state["result"],
                "error": _run_state["error"],
            }
        )


@app.route("/api/portfolio/history", methods=["GET"])
def api_portfolio_history():
    history = _portfolio_history(LIVE_EXP_NAME)
    return jsonify(
        {
            "dates": [h["date"] for h in history],
            "total_assets": [h["total_assets"] for h in history],
            "cash": [h["cash"] for h in history],
        }
    )


@app.route("/api/portfolio/latest", methods=["GET"])
def api_portfolio_latest():
    latest = _latest_portfolio(LIVE_EXP_NAME)
    if not latest:
        return jsonify({"ok": False, "error": "no data"}), 404
    decisions = _decisions_for_portfolio(latest["id"])
    signals = _signals_for_portfolio(latest["id"])
    active = {
        k: v for k, v in latest["positions"].items() if v.get("shares", 0) > 0
    }
    return jsonify(
        {
            "ok": True,
            "date": latest["date"],
            "cash": latest["cash"],
            "total_assets": latest["total_assets"],
            "active_positions": active,
            "decisions": decisions,
            "signals": signals,
        }
    )


@app.route("/api/report/<date>", methods=["GET"])
def api_report(date):
    """Return report data (portfolio + decisions + signals) for a specific date."""
    portfolio = _portfolio_for_date(date, LIVE_EXP_NAME)
    if not portfolio:
        return jsonify({"ok": False, "error": "no data for date"}), 404
    decisions = _decisions_for_portfolio(portfolio["id"])
    signals = _signals_for_portfolio(portfolio["id"])
    active = {
        k: v for k, v in portfolio["positions"].items() if v.get("shares", 0) > 0
    }
    return jsonify(
        {
            "ok": True,
            "date": portfolio["date"],
            "cash": portfolio["cash"],
            "total_assets": portfolio["total_assets"],
            "active_positions": active,
            "decisions": decisions,
            "signals": signals,
        }
    )


@app.route("/api/reports", methods=["GET"])
def api_reports():
    return jsonify({"dates": _report_history()})


@app.route("/chart-image/<path:item_name>", methods=["GET"])
def chart_image(item_name):
    """Serve the most recent chart screenshot PNG for an item.

    Query param `type` selects the chart view: 'kline' (default) or 'line'.
    The screenshot is captured by the vision analyst agent at analysis time
    and stored under assets/screenshots/<safe>_kline.png or <safe>_line.png.
    """
    chart_type = (request.args.get("type") or "kline").strip().lower()
    if chart_type not in ("kline", "line"):
        chart_type = "kline"
    safe = _safe_filename(item_name)
    candidate = DEFAULT_SCREENSHOT_DIR / f"{safe}_{chart_type}.png"
    if not candidate.exists():
        abort(404, description=f"{chart_type} chart screenshot not found")
    return send_from_directory(
        str(DEFAULT_SCREENSHOT_DIR), candidate.name, mimetype="image/png"
    )


# ----------------- Inventory APIs -----------------

@app.route("/api/inventory", methods=["GET"])
def api_inventory_list():
    """List inventory items with realtime P&L.

    Query param `?no-market=1` skips realtime price fetch (faster).
    """
    with_market = request.args.get("no-market") != "1"
    items = inventory_mod.list_items(with_market=with_market)
    # summary
    total_cost = sum(it["buy_price"] * it["shares"] for it in items)
    total_value = sum(
        (it.get("current_price") or 0) * it["shares"]
        for it in items if it.get("current_price") is not None
    )
    total_pnl = sum((it.get("total_pnl") or 0) for it in items)
    total_yesterday_pnl = sum((it.get("yesterday_pnl") or 0) for it in items)
    return jsonify({
        "ok": True,
        "items": items,
        "count": len(items),
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_yesterday_pnl": round(total_yesterday_pnl, 2),
    })


@app.route("/api/inventory", methods=["POST"])
def api_inventory_add():
    data = request.get_json(force=True) or {}
    name = (data.get("item_name") or data.get("name") or "").strip()
    shares = data.get("shares")
    buy_price = data.get("buy_price")
    buy_date = data.get("buy_date")
    notes = data.get("notes", "") or ""
    if not name:
        return jsonify({"ok": False, "error": "item_name required"}), 400
    if shares is None:
        return jsonify({"ok": False, "error": "shares required"}), 400
    if buy_price is None:
        return jsonify({"ok": False, "error": "buy_price required"}), 400
    if not buy_date:
        return jsonify({"ok": False, "error": "buy_date required (YYYY-MM-DD)"}), 400
    try:
        shares = int(shares)
        buy_price = float(buy_price)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "shares/buy_price must be numeric"}), 400

    # Verify item exists on Steam market (reuse watchlist verifier)
    try:
        if not verify_item(name):
            return jsonify({"ok": False, "error": "market data not found, check item name"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"verify failed: {e}"}), 500

    row = inventory_mod.add_item(name, shares, buy_price, buy_date, notes)
    if not row:
        return jsonify({"ok": False, "error": "insert failed"}), 500
    return jsonify({"ok": True, "item": row})


@app.route("/api/inventory/<item_id>", methods=["DELETE"])
def api_inventory_delete(item_id):
    ok = inventory_mod.remove_item(item_id)
    if not ok:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/inventory/<item_id>", methods=["PUT", "PATCH"])
def api_inventory_update(item_id):
    data = request.get_json(force=True) or {}
    kwargs = {}
    for k in ("shares", "buy_price", "buy_date", "notes"):
        if k in data:
            v = data[k]
            if k == "shares":
                v = int(v)
            elif k == "buy_price":
                v = float(v)
            kwargs[k] = v
    if not kwargs:
        return jsonify({"ok": False, "error": "no fields to update"}), 400
    ok = inventory_mod.update_item(item_id, **kwargs)
    if not ok:
        return jsonify({"ok": False, "error": "update failed or not found"}), 404
    return jsonify({"ok": True})


# ----------------- Today Advisor APIs -----------------

@app.route("/api/today-advisor/status", methods=["GET"])
def api_today_advisor_status():
    """Return warmup progress + background run status."""
    return jsonify({
        "warmup": advisor_mod.warmup_status_summary(),
        "run": advisor_mod.get_run_status(),
    })


@app.route("/api/today-advisor/run", methods=["POST"])
def api_today_advisor_run():
    """Trigger today's advisor analysis in background (warmup + analysis)."""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    started = advisor_mod.run_today_advisor_background()
    if not started:
        return jsonify({"ok": False, "error": "advisor already running"}), 409
    return jsonify({"ok": True, "status": "started", "items": items})


@app.route("/api/today-advisor/warmup", methods=["POST"])
def api_today_advisor_warmup():
    """触发截图预热(后台执行,立即返回)。

    body: {"items": [...], "force": false}
    - items 为 None 时预热全部库存
    - force=true 忽略 5 小时缓存,强制重新截图
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    force = bool(data.get("force"))
    started = advisor_mod.warmup_screenshots_background(items=items, force=force)
    if not started:
        return jsonify({"ok": False, "error": "warmup already running"}), 409
    return jsonify({"ok": True, "status": "started", "force": force})


@app.route("/api/today-advisor", methods=["GET"])
def api_today_advisor_results():
    """Return today's decisions + signals for inventory items."""
    return jsonify(advisor_mod.get_today_advisor_results())


# ----------------- AI Trader APIs -----------------

@app.route("/api/ai-trader", methods=["GET"])
def api_ai_trader():
    """Return AI trader P&L summary (realized + unrealized)."""
    return jsonify(ai_trader_mod.get_ai_trader_summary())


@app.route("/api/ai-trader/record-sell", methods=["POST"])
def api_ai_trader_record_sell():
    """Manually record a sell trade for an AI decision.

    Body: {decision_id, item_name, shares, trade_date, buy_price?, sell_price?}
    buy_price/sell_price are auto-resolved if omitted.
    """
    data = request.get_json(force=True) or {}
    required = ("decision_id", "item_name", "shares", "trade_date")
    for f in required:
        if not data.get(f):
            return jsonify({"ok": False, "error": f"{f} required"}), 400
    try:
        shares = int(data["shares"])
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "shares must be numeric"}), 400
    buy_price = data.get("buy_price")
    sell_price = data.get("sell_price")
    if buy_price is not None:
        buy_price = float(buy_price)
    if sell_price is not None:
        sell_price = float(sell_price)
    row = ai_trader_mod.record_sell_from_decision(
        decision_id=data["decision_id"],
        item_name=data["item_name"],
        shares=shares,
        trade_date=data["trade_date"],
        buy_price=buy_price,
        sell_price=sell_price,
    )
    if not row:
        return jsonify({"ok": False, "error": "record failed"}), 500
    return jsonify({"ok": True, "trade": row})


# ----------------- AI Account (AI端虚拟账户) APIs -----------------

@app.route("/api/ai-account/run", methods=["POST"])
def api_ai_account_run():
    """手动触发 AI 端虚拟账户运行(后台)。"""
    started = ai_account_mod.run_ai_account_background()
    if not started:
        return jsonify({"ok": False, "error": "AI 账户已在运行"}), 409
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/ai-account/status", methods=["GET"])
def api_ai_account_status():
    return jsonify(ai_account_mod.get_run_status())


AI_ACCOUNT_INTERVAL_SECONDS = 6 * 60 * 60  # 每 6 小时
_AI_ACCOUNT_FIRST_DELAY_SECONDS = 60       # 启动后 60 秒先跑一次


def _schedule_ai_account():
    """定时运行 AI 端虚拟账户(启动延迟 60s 后跑第一次,之后每 6h 一次)。"""

    def _tick():
        try:
            ai_account_mod.run_ai_account_background()
        except Exception as e:
            logger.error(f"ai_account scheduler: {e}")
        finally:
            threading.Timer(AI_ACCOUNT_INTERVAL_SECONDS, _tick).start()

    threading.Timer(_AI_ACCOUNT_FIRST_DELAY_SECONDS, _tick).start()


# ----------------- Main -----------------

STARTUP_WARMUP_TIMEOUT = 5 * 60  # 5 分钟


def _startup_warmup_blocking():
    """启动时阻塞预热,直到全部完成或超时。

    - 命中 5 小时内缓存 → 瞬间返回
    - 否则并行截图,最多等 5 分钟
    - 5 分钟内全部完成但有任一失败 → 退出进程(失败即退出)
    - 超时未全部完成 → 打印警告并继续启动,未完成的饰品按 error 处理
    """
    summary = advisor_mod.warmup_status_summary()
    if summary.get("ready"):
        logger.info(
            f"startup warmup: cache fresh ({summary['done']}/{summary['total']} done), "
            "skipping"
        )
        return

    logger.info(
        "startup warmup: blocking until screenshots ready "
        f"(timeout={STARTUP_WARMUP_TIMEOUT}s)"
    )
    t0 = time.time()
    result = advisor_mod.warmup_screenshots()
    elapsed = time.time() - t0
    state = advisor_mod.get_warmup_state()

    if state.get("running"):
        # 这个分支不应该发生,因为 warmup_screenshots 是同步的
        logger.warning("startup warmup: still running after synchronous call, continuing")
        return

    if state.get("error"):
        logger.error(f"startup warmup crashed: {state['error']}")
        sys.exit(1)

    failed = result.get("failed", 0)
    total = result.get("total", 0)
    done = result.get("done", 0)

    logger.info(
        f"startup warmup finished in {elapsed:.1f}s: "
        f"total={total} done={done} failed={failed}"
    )

    if failed > 0:
        logger.error(
            f"startup warmup failed for {failed} item(s); refusing to start"
        )
        sys.exit(1)


def _startup_warmup_with_timeout():
    """Run blocking warmup in a daemon thread with a timeout.

    If the timeout expires before warmup finishes, the server starts anyway
    (those items will be marked error by the still-running thread). This is a
    safety valve so a hung browser doesn't block startup forever.
    """
    summary = advisor_mod.warmup_status_summary()
    if summary.get("ready"):
        logger.info(
            f"startup warmup: cache fresh ({summary['done']}/{summary['total']} done), "
            "skipping"
        )
        return

    logger.info(
        "startup warmup: waiting up to "
        f"{STARTUP_WARMUP_TIMEOUT}s for screenshots"
    )
    t0 = time.time()
    thread = threading.Thread(target=advisor_mod.warmup_screenshots, daemon=True)
    thread.start()
    thread.join(timeout=STARTUP_WARMUP_TIMEOUT)

    state = advisor_mod.get_warmup_state()
    if thread.is_alive():
        logger.warning(
            "startup warmup: timeout reached, starting server with partial cache"
        )
        return

    elapsed = time.time() - t0
    result = state.get("result") or {}
    failed = result.get("failed", 0)
    total = result.get("total", 0)
    done = result.get("done", 0)
    error = state.get("error")

    if error:
        logger.error(f"startup warmup crashed after {elapsed:.1f}s: {error}")
        sys.exit(1)

    logger.info(
        f"startup warmup finished in {elapsed:.1f}s: "
        f"total={total} done={done} failed={failed}"
    )

    if failed > 0:
        logger.error(
            f"startup warmup failed for {failed} item(s); refusing to start"
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="CS2 Trading Advisor Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", type=int, default=5000, help="bind port")
    parser.add_argument("--debug", action="store_true", help="debug mode")
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="跳过启动时的截图预热(默认会阻塞等待预热完成)",
    )
    args = parser.parse_args()

    if not args.no_warmup:
        # 用户要求预热完才能启动前端,所以改为阻塞等待。
        # 这里用带超时的线程版本,防止某个浏览器卡死导致永远起不来。
        _startup_warmup_with_timeout()

    # AI 端虚拟账户:启动延迟 60s 后跑第一次,之后每 6h 定时全自动运行
    _schedule_ai_account()

    print(f"CS2 Trading Advisor Dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
