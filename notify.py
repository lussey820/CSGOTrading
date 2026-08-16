"""Notification module - generate advice report and push to WeChat."""

import os
import sqlite3
import json
import time
from datetime import datetime
import requests
from database.cs2_sqlite_setup import CS2_DB_PATH
from util.logger import logger


def _post_with_retry(url, max_attempts=3, **kwargs):
    """POST 请求带重试,应对偶发网络超时。全部失败则抛出最后一次异常。"""
    last = None
    for attempt in range(max_attempts):
        try:
            return requests.post(url, timeout=15, **kwargs)
        except Exception as e:
            last = e
            if attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last


def send_wechat(title, content):
    """Push message to WeChat via Server酱 or pushplus.

    Reads PUSH_PROVIDER and PUSH_TOKEN from env.
    Returns True on success.
    """
    provider = os.getenv("PUSH_PROVIDER", "serverchan").lower()
    token = os.getenv("PUSH_TOKEN", "")
    if not token or token == "YOUR_TOKEN_HERE":
        logger.error("PUSH_TOKEN not set in .env, skip push")
        return False
    if provider == "pushplus":
        return _send_pushplus(token, title, content)
    return _send_serverchan(token, title, content)


def _send_serverchan(sendkey, title, content):
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        resp = _post_with_retry(url, data={"title": title, "desp": content})
        data = resp.json()
        if data.get("code") == 0:
            logger.info("Server酱 push success")
            return True
        logger.error(f"Server酱 push failed: {data.get('message', data)}")
        return False
    except Exception as e:
        logger.error(f"Server酱 push error: {e}")
        return False


def _send_pushplus(token, title, content):
    url = "https://www.pushplus.plus/send"
    try:
        resp = _post_with_retry(
            url,
            json={"token": token, "title": title, "content": content, "template": "markdown"},
        )
        data = resp.json()
        if data.get("code") == 200:
            logger.info("pushplus push success")
            return True
        logger.error(f"pushplus push failed: {data.get('msg', data)}")
        return False
    except Exception as e:
        logger.error(f"pushplus push error: {e}")
        return False


def generate_report(exp_name, trading_date):
    """Generate markdown advice report for a trading date.

    Reads decisions and portfolio state from SQLite.
    Returns a markdown string.
    """
    conn = sqlite3.connect(CS2_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. config_id by exp_name
    cursor.execute("SELECT id FROM cs2_config WHERE exp_name = ?", (exp_name,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return f"Experiment '{exp_name}' not found"
    config_id = row["id"]

    # 2. portfolio by config_id + trading_date
    cursor.execute(
        "SELECT id, cashflow, total_assets, positions FROM cs2_portfolio "
        "WHERE config_id = ? AND DATE(trading_date) = DATE(?) "
        "ORDER BY updated_at DESC LIMIT 1",
        (config_id, trading_date),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return f"No portfolio data for {exp_name} on {trading_date}"
    portfolio_id = row["id"]
    cash = row["cashflow"]
    total_assets = row["total_assets"]
    positions = json.loads(row["positions"]) if row["positions"] else {}

    # 3. decisions by portfolio_id(每饰品取最新一条,避免同一轮/多轮重复)
    cursor.execute(
        """
        SELECT d.item_name, d.action, d.quantity, d.price, d.justification
        FROM cs2_decision d
        WHERE d.portfolio_id = ?
          AND d.id = (
              SELECT d2.id FROM cs2_decision d2
              WHERE d2.portfolio_id = ?
                AND d2.item_name = d.item_name
              ORDER BY d2.updated_at DESC
              LIMIT 1
          )
        ORDER BY d.item_name
        """,
        (portfolio_id, portfolio_id),
    )
    decisions = cursor.fetchall()
    conn.close()

    # Build report
    lines = []
    lines.append(f"# CS2 交易建议 {trading_date}")
    lines.append("")
    lines.append(f"**总资产**: ¥{total_assets:.2f} | **现金**: ¥{cash:.2f}")
    lines.append("")

    buy_items, sell_items, hold_items = [], [], []
    for d in decisions:
        entry = (d["item_name"], d["action"], d["quantity"], d["price"], d["justification"])
        if d["action"] == "BUY":
            buy_items.append(entry)
        elif d["action"] == "SELL":
            sell_items.append(entry)
        else:
            hold_items.append(entry)

    lines.append("## 今日建议")
    lines.append("")
    if buy_items:
        lines.append("### 买入建议")
        for name, _, qty, price, just in buy_items:
            lines.append(f"- **{name}**: 买 {qty} 件 @ ¥{price:.2f}")
            lines.append(f"  - {just}")
        lines.append("")
    if sell_items:
        lines.append("### 卖出建议")
        for name, _, qty, price, just in sell_items:
            lines.append(f"- **{name}**: 卖 {qty} 件 @ ¥{price:.2f}")
            lines.append(f"  - {just}")
        lines.append("")
    if hold_items:
        lines.append("### 观望")
        for name, _, _, price, just in hold_items:
            lines.append(f"- **{name}** @ ¥{price:.2f}")
            lines.append(f"  - {just}")
        lines.append("")

    # Current holdings
    active = {k: v for k, v in positions.items() if v.get("shares", 0) > 0}
    if active:
        lines.append("## 当前持仓")
        lines.append("")
        for name, data in active.items():
            shares = data.get("shares", 0)
            value = data.get("value", 0)
            lines.append(f"- {name}: {shares} 件 (价值 ¥{value:.2f})")
        lines.append("")

    lines.append("---")
    lines.append("_本建议由多智能体 LLM 系统生成，仅供参考，不构成投资指令_")

    return "\n".join(lines)
