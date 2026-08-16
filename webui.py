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
from apis.ocr import recognize_item_names
from apis.cs2market.item_search import search_items
from util.logger import logger
import inventory as inventory_mod
import today_advisor as advisor_mod
import ai_trader as ai_trader_mod
import ai_account as ai_account_mod

PROJECT_ROOT = Path(__file__).parent
LIVE_EXP_NAME = "live-advisor"

ENV_FILE = PROJECT_ROOT / ".env"

# 设置页展示的可配置 API Key(env 变量名 -> 展示信息)
SETTING_KEYS = [
    {
        "env": "STEAMDT_API_KEY",
        "name": "SteamDT 开放平台",
        "desc": "饰品价格、K 线、全量名称数据(图像识别匹配依赖它)",
        "home": "https://open.steamdt.com",
    },
    {
        "env": "DASHSCOPE_API_KEY",
        "name": "阿里云百炼 DashScope",
        "desc": "图像识别(OCR)与截图看图分析",
        "home": "https://bailian.console.aliyun.com/",
    },
    {
        "env": "DEEPSEEK_API_KEY",
        "name": "DeepSeek",
        "desc": "AI 决策分析(今日建议 / AI 持仓)",
        "home": "https://platform.deepseek.com/api_keys",
    },
]

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    load_dotenv = None

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


@app.route("/settings")
def settings_page():
    return render_template("settings.html", setting_keys=SETTING_KEYS)


@app.route("/comparison")
def comparison_page():
    return render_template("comparison.html")


# ----------------- Settings APIs -----------------

def _mask_secret(value: str) -> str:
    """脱敏展示:保留前 4 后 4,中间打星。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _read_env_value(key: str) -> str:
    """优先读运行时 os.environ(含新保存的值),再回退到 .env 文件。"""
    v = os.environ.get(key)
    if v:
        return v
    try:
        from dotenv import dotenv_values
        return (dotenv_values(ENV_FILE) or {}).get(key, "") or ""
    except Exception:
        return ""


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """返回当前各 API Key 的配置状态(脱敏)。"""
    result = []
    for k in SETTING_KEYS:
        val = _read_env_value(k["env"])
        result.append(
            {
                "env": k["env"],
                "name": k["name"],
                "configured": bool(val),
                "masked": _mask_secret(val) if val else "",
            }
        )
    return jsonify({"ok": True, "settings": result})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """保存 API Key 到 .env 文件并写入运行时环境变量。

    body: {"keys": {"DASHSCOPE_API_KEY": "sk-...", ...}}
    值为空字符串表示不改动该项;显式传 None 表示清空。
    """
    data = request.get_json(force=True) or {}
    keys = data.get("keys") or {}
    if not isinstance(keys, dict):
        return jsonify({"ok": False, "error": "keys 必须是对象"}), 400

    valid_envs = {k["env"] for k in SETTING_KEYS}
    unknown = [e for e in keys if e not in valid_envs]
    if unknown:
        return jsonify({"ok": False, "error": f"未知配置项: {', '.join(unknown)}"}), 400

    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    def _upsert_line(existing: list, key: str, value: str) -> list:
        new_key_prefix = f"{key}="
        out = []
        found = False
        for line in existing:
            stripped = line.strip()
            if stripped.startswith(new_key_prefix) or stripped.startswith(f"{key} "):
                if not found:
                    out.append(f"{key}={value}" if value else f"{key}=")
                    found = True
                continue
            out.append(line)
        if not found:
            out.append(f"{key}={value}" if value else f"{key}=")
        return out

    saved = []
    for k in SETTING_KEYS:
        env = k["env"]
        if env not in keys:
            continue
        new_val = keys[env]
        if new_val is None:
            # 清空该项
            os.environ.pop(env, None)
        else:
            new_val = str(new_val).strip()
            if new_val:
                os.environ[env] = new_val
            else:
                # 空字符串:不改动 .env,但保留 os.environ 已有值
                continue
        lines = _upsert_line(lines, env, new_val or "")
        saved.append(env)

    try:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.error(f"settings save: write .env failed: {e}")
        return jsonify({"ok": False, "error": f"写入 .env 失败: {e}"}), 500

    return jsonify({"ok": True, "saved": saved})


# ----------------- JSON APIs -----------------

# 价格后台预热:页面首次加载时先秒渲染(无价),再后台抓价格写入缓存,
# 前端轮询 /api/prices/status 完成后重新带价加载。
_price_warmup_lock = threading.Lock()
_price_warmup_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "current": None,
}


@app.route("/api/prices/warmup", methods=["POST"])
def api_prices_warmup():
    """后台预热价格:对 names 逐个抓取并写入价格缓存(3s 限速在后台进行)。"""
    data = request.get_json(silent=True) or {}
    names = data.get("names") or []
    names = [str(n).strip() for n in names if str(n) and str(n).strip()]
    if not names:
        return jsonify({"ok": False, "error": "names required"}), 400
    with _price_warmup_lock:
        if _price_warmup_state["running"]:
            return jsonify(
                {"ok": True, "running": True, "status": dict(_price_warmup_state)}
            )
        _price_warmup_state.update(running=True, done=0, total=len(names), current=None)

    def _work():
        from apis.cs2market.web_scraper import fetch_item_market_data
        try:
            for i, name in enumerate(names, 1):
                with _price_warmup_lock:
                    _price_warmup_state["current"] = name
                try:
                    fetch_item_market_data(name)
                except Exception as e:
                    logger.warning(f"price warmup: {name} failed: {e}")
                with _price_warmup_lock:
                    _price_warmup_state["done"] = i
        finally:
            with _price_warmup_lock:
                _price_warmup_state["running"] = False
                _price_warmup_state["current"] = None

    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"ok": True, "running": True, "status": dict(_price_warmup_state)})


@app.route("/api/prices/status", methods=["GET"])
def api_prices_status():
    with _price_warmup_lock:
        return jsonify(dict(_price_warmup_state))


@app.route("/api/scan/recognize", methods=["POST"])
def api_scan_recognize():
    """图像识别:上传饰品图片 → OCR 识别所有饰品名 → SteamDT 搜索匹配候选。

    body: multipart/form-data,字段 image=图片文件
    返回 {ok, queries: [识别出的名称], results: [{query, candidates}]}
    """
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择要识别的图片"}), 400
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"ok": False, "error": "图片内容为空"}), 400
    if len(image_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "图片过大(超过 10MB)"}), 400
    mime = (file.mimetype or "").lower()
    if not mime.startswith("image/"):
        return jsonify({"ok": False, "error": "仅支持图片文件(PNG/JPG/WebP 等)"}), 400
    try:
        queries = recognize_item_names(image_bytes, mime=mime)
    except Exception as e:
        logger.error(f"scan: OCR failed: {e}")
        return jsonify({"ok": False, "error": f"文字识别失败: {e}"}), 500
    if not queries:
        return jsonify({"ok": False, "error": "未识别到饰品名称,请换一张更清晰的图片"}), 422
    results = []
    for q in queries:
        try:
            candidates = search_items(q)
        except Exception as e:
            logger.error(f"scan: search failed for {q}: {e}")
            candidates = []
        results.append({"query": q, "candidates": candidates})
    return jsonify({"ok": True, "queries": queries, "results": results})


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


@app.route("/api/watchlist/add-many", methods=["POST"])
def api_watchlist_add_many():
    """批量添加关注饰品(图像识别多选场景,一次 HTTP 请求减少调用)。

    body: {"names": ["name1", "name2", ...]}
    逐个校验市场数据;已存在则跳过。返回 {added, existing, failed}。
    """
    data = request.get_json(force=True) or {}
    names = data.get("names") or []
    names = [str(n).strip() for n in names if str(n) and str(n).strip()]
    if not names:
        return jsonify({"ok": False, "error": "names required"}), 400
    tickers = load_watchlist()
    added, failed, existing = [], [], 0
    for name in names:
        if name in tickers:
            existing += 1
            continue
        try:
            if not verify_item(name):
                failed.append({"name": name, "error": "market data not found"})
                continue
        except Exception as e:
            failed.append({"name": name, "error": f"verify failed: {e}"})
            continue
        tickers.append(name)
        added.append(name)
    if added:
        save_watchlist(tickers)
    return jsonify(
        {"ok": True, "added": added, "existing": existing, "failed": failed}
    )


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
    # 买入自动记用户操作日志
    try:
        from user_trades import add_trade as add_user_trade
        add_user_trade(
            item_name=name,
            item_name_cn=row.get("item_name_cn") or "",
            action="buy",
            shares=shares,
            price=buy_price,
            fee=0.0,
            realized_pnl=None,
            trade_date=buy_date,
        )
    except Exception as e:
        logger.error(f"inventory add: write user trade log failed: {e}")
    return jsonify({"ok": True, "item": row})


@app.route("/api/inventory/batch", methods=["POST"])
def api_inventory_batch():
    """批量添加库存(图像识别多选场景,一次 HTTP 请求减少调用)。

    body: {"items": [{"item_name", "shares", "buy_price", "buy_date", "notes"}, ...]}
    逐个校验市场数据并写入;返回 {added, failed}。
    """
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "items required"}), 400
    added, failed = [], []
    for it in items:
        name = (it.get("item_name") or it.get("name") or "").strip()
        notes = it.get("notes") or ""
        try:
            shares = int(it.get("shares"))
            buy_price = float(it.get("buy_price"))
            buy_date = str(it.get("buy_date") or "").strip()
        except (TypeError, ValueError):
            failed.append({"item_name": name or "(空)", "error": "shares/buy_price 必须是数字"})
            continue
        if not name or shares <= 0 or buy_price <= 0 or not buy_date:
            failed.append({"item_name": name or "(空)", "error": "缺少名称/数量/价格/日期"})
            continue
        try:
            if not verify_item(name):
                failed.append({"item_name": name, "error": "market data not found"})
                continue
        except Exception as e:
            failed.append({"item_name": name, "error": f"verify failed: {e}"})
            continue
        row = inventory_mod.add_item(name, shares, buy_price, buy_date, notes)
        if not row:
            failed.append({"item_name": name, "error": "insert failed"})
            continue
        try:
            from user_trades import add_trade as add_user_trade
            add_user_trade(
                item_name=name,
                item_name_cn=row.get("item_name_cn") or "",
                action="buy",
                shares=shares,
                price=buy_price,
                fee=0.0,
                realized_pnl=None,
                trade_date=buy_date,
            )
        except Exception as e:
            logger.error(f"inventory batch: write user trade log failed: {e}")
        added.append(
            {
                "item_name": name,
                "item_name_cn": row.get("item_name_cn") or name,
                "shares": shares,
                "buy_price": buy_price,
            }
        )
    return jsonify(
        {"ok": True, "added": added, "failed": failed, "added_count": len(added)}
    )


@app.route("/api/inventory/<item_id>/sell", methods=["POST"])
def api_inventory_sell(item_id):
    """售出库存饰品(支持部分卖出)。

    body: {"shares": n, "sell_price": p}
    手续费固定 2%,到手价 = 售出总额 - 手续费,
    已实现盈亏 = 到手价 - 买入成本 × 售出数量。
    售出后扣减库存,数量归零则删除该条记录,并写入用户操作日志。
    """
    data = request.get_json(force=True) or {}
    shares = data.get("shares")
    sell_price = data.get("sell_price")
    if shares is None or sell_price is None:
        return jsonify({"ok": False, "error": "shares and sell_price required"}), 400
    try:
        shares = int(shares)
        sell_price = float(sell_price)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "shares/sell_price must be numeric"}), 400
    if shares <= 0 or sell_price <= 0:
        return jsonify({"ok": False, "error": "shares/sell_price must be > 0"}), 400

    # 查库存记录
    items = inventory_mod.list_items(with_market=False)
    item = next((it for it in items if it["id"] == item_id), None)
    if not item:
        return jsonify({"ok": False, "error": "inventory item not found"}), 404
    if shares > item["shares"]:
        return jsonify(
            {"ok": False, "error": f"售出数量超过持有数量(持有 {item['shares']} 件)"}
        ), 400

    # 手续费固定 2%:手续费 = 售出总额 × 2%;到手价 = 售出总额 - 手续费
    total = round(sell_price * shares, 2)
    fee = round(total * 0.02, 2)
    net = round(total - fee, 2)
    buy_cost = item["buy_price"] * shares
    realized_pnl = round(net - buy_cost, 2)

    remaining = item["shares"] - shares
    if remaining > 0:
        ok = inventory_mod.update_item(item_id, shares=remaining)
    else:
        ok = inventory_mod.remove_item(item_id)
    if not ok:
        return jsonify({"ok": False, "error": "update inventory failed"}), 500

    # 售出自动记用户操作日志
    try:
        from user_trades import add_trade as add_user_trade
        add_user_trade(
            item_name=item["item_name"],
            item_name_cn=item.get("item_name_cn") or "",
            action="sell",
            shares=shares,
            price=sell_price,
            fee=fee,
            realized_pnl=realized_pnl,
        )
    except Exception as e:
        logger.error(f"inventory sell: write user trade log failed: {e}")

    return jsonify(
        {
            "ok": True,
            "sold": {
                "item_name": item["item_name"],
                "item_name_cn": item.get("item_name_cn") or "",
                "shares": shares,
                "sell_price": sell_price,
                "fee": fee,
                "net": net,
                "realized_pnl": realized_pnl,
                "remaining": remaining,
            },
        }
    )


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
    """Return AI trader P&L summary (realized + unrealized).

    Query param `?no-market=1` skips realtime price fetch (faster).
    """
    with_market = request.args.get("no-market") != "1"
    return jsonify(ai_trader_mod.get_ai_trader_summary(with_market=with_market))


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


@app.route("/api/ai-account/seed-inventory", methods=["POST"])
def api_ai_account_seed_inventory():
    """将用户端库存一键复制为 AI 端虚拟账户持仓。

    body: {"items": ["AK-47 | Redline (Field-Tested)"]} 可选,缺省复制全部库存。
    复制后 AI 可基于这些持仓自主决策卖出或持有。
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    result = ai_account_mod.seed_from_inventory(items=items)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error", "复制失败")}), 400
    return jsonify(result)


# ----------------- Comparison (决策对照) APIs -----------------

_COMPARISON_CACHE_TTL = 60  # 秒:计算完成后短期内直接复用缓存,避免重复抓价


@app.route("/api/comparison", methods=["GET"])
def api_comparison():
    """AI 端 vs 用户端:净利润曲线 + 操作对照表。

    实时价格抓取较慢(SteamDT 有 3s 限速),改为后台线程计算:
    - 首次/缓存过期 → 触发后台计算,返回 running + progress 供前端进度条
    - 运行中 → 返回进度
    - 完成 → 返回结果
    """
    from comparison import get_comparison_state, run_comparison_background

    st = get_comparison_state()
    if not st["running"]:
        fresh = False
        if st["result"] and st["finished_at"]:
            try:
                ft = datetime.fromisoformat(st["finished_at"])
                fresh = (datetime.now() - ft).total_seconds() < _COMPARISON_CACHE_TTL
            except (ValueError, TypeError):
                fresh = False
        if not fresh:
            run_comparison_background()
            st = get_comparison_state()

    if st["running"]:
        return jsonify(
            {
                "ok": True,
                "running": True,
                "progress": {
                    "done": st["done"] or 0,
                    "total": st["total"] or 0,
                    "current": st["current"],
                },
            }
        )
    if st["error"]:
        return jsonify({"ok": False, "error": st["error"]}), 500
    result = dict(st["result"] or {})
    result["ok"] = True
    return jsonify(result)


# AI 账户调度:webui 启动后 60s 自动跑一轮(每次打开即刷新决策)。
# AI_ACCOUNT_AUTO_INTERVAL 秒 > 0 时,在上一轮结束后再排下一轮(默认 0=关闭定时)。
AI_ACCOUNT_INTERVAL_SECONDS = int(os.getenv("AI_ACCOUNT_AUTO_INTERVAL", "0"))
_AI_ACCOUNT_FIRST_DELAY_SECONDS = 60       # 启动后 60 秒先跑一次


def _schedule_ai_account():
    """启动后自动跑一轮 AI 端虚拟账户;可选开启"上轮结束后再排下一轮"的定时。"""

    def _tick():
        try:
            ai_account_mod.run_ai_account_background()
        except Exception as e:
            logger.error(f"ai_account scheduler: {e}")
        finally:
            # 仅在上一轮结束后才排下一轮,避免运行超时被静默跳过
            if AI_ACCOUNT_INTERVAL_SECONDS > 0:
                threading.Timer(AI_ACCOUNT_INTERVAL_SECONDS, _tick).start()

    threading.Timer(_AI_ACCOUNT_FIRST_DELAY_SECONDS, _tick).start()
    if AI_ACCOUNT_INTERVAL_SECONDS > 0:
        logger.info(
            f"ai_account scheduler: auto-interval enabled "
            f"({AI_ACCOUNT_INTERVAL_SECONDS}s after each run)"
        )


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
