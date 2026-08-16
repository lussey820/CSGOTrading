# 每日交易建议推送系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 CSGOTrading 回测框架改造为每日 20:00 自动运行分析、生成买卖建议并推送到微信的决策建议系统（纯建议，不自动下单）。

**Architecture:** 新增三个独立模块（watchlist.py 标的管理 / notify.py 报告与推送 / daily.py 定时入口），复用现有 run.py 的 run\_single\_experiment 和 SteamDT 实时数据 API，不改动核心 agent 工作流。用户通过 watchlist.py 增删监控饰品，daily.py 每天合并 watchlist 到临时配置后调用现有 workflow，跑完从 SQLite 读取当日决策生成 Markdown 报告并推送。

**Tech Stack:** Python 3.13, schedule (定时调度), requests (微信推送), PyYAML, SteamDT API, SQLite

## Global Constraints  

- Python 3.13.9 运行环境，依赖见 requirements.txt
- STEAMDT\_API\_KEY 已在 .env 配置（数据源实时可用）
- DEEPSEEK\_API\_KEY 已在 .env 配置（LLM 可用）
- 微信推送需用户提供 PUSH\_TOKEN（Server酱 sendkey 或 pushplus token）
- 不修改现有 agents/ graph/ apis/ database/ 目录下的核心代码
- 标的验证使用 SteamDT K线 API（返回非空即有效）
- 项目无测试框架（无 pytest/tests 目录），验证方式为运行命令检查输出

## File Structure

- Create: `watchlist.py` — 标的管理 CLI（add/remove/list/clear），验证饰品后写入 config/watchlist.yaml
- Create: `notify.py` — 报告生成 + 微信推送（Server酱/pushplus 双支持）
- Create: `daily.py` — 每日定时入口（--now 立即跑 / --daemon 常驻调度）
- Create: `config/watchlist.yaml` — 监控标的列表
- Create: `config/live.yaml` — 实盘建议模式配置
- Modify: `requirements.txt` — 增加 schedule 依赖
- Modify: `.env.example` — 增加 PUSH\_PROVIDER / PUSH\_TOKEN 配置项

***

### Task 1: 标的管理模块 (watchlist.py)

**Files:**

- Create: `watchlist.py`
- Create: `config/watchlist.yaml`

**Interfaces:**

- Consumes: `apis.cs2market.api.get_cs2_stock_daily_candles_df(ticker, trading_date)` — 现有函数，trading\_date=None 时返回全部历史K线，非空df说明饰品有效
- Produces: `config/watchlist.yaml` 格式为 `{"tickers": ["name1", "name2"]}`，供 daily.py 读取
- [ ] **Step 1: 创建 config/watchlist.yaml 空列表**

```yaml
tickers: []
```

- [ ] **Step 2: 创建 watchlist.py**

```python
"""Watchlist management CLI - add/remove/list monitored CS2 items."""

import sys
import os
import yaml
from pathlib import Path
from apis.cs2market.api import get_cs2_stock_daily_candles_df

WATCHLIST_PATH = Path(__file__).parent / "config" / "watchlist.yaml"


def load_watchlist():
    if not WATCHLIST_PATH.exists():
        return []
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tickers", [])


def save_watchlist(tickers):
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"tickers": tickers}, f, allow_unicode=True, sort_keys=False)


def verify_item(name):
    """Verify item exists on Steam market by fetching its kline data."""
    df = get_cs2_stock_daily_candles_df(ticker=name, trading_date=None)
    return not df.empty


def add(name):
    tickers = load_watchlist()
    if name in tickers:
        print(f"Already in watchlist: {name}")
        return
    print(f"Verifying {name} ...")
    if not verify_item(name):
        print(f"Verification failed: no market data for '{name}'")
        print("Check the exact market_hash_name on Steam Community Market.")
        return
    tickers.append(name)
    save_watchlist(tickers)
    print(f"Added: {name}")


def remove(name):
    tickers = load_watchlist()
    if name not in tickers:
        print(f"Not in watchlist: {name}")
        return
    tickers.remove(name)
    save_watchlist(tickers)
    print(f"Removed: {name}")


def list_items():
    tickers = load_watchlist()
    if not tickers:
        print('Watchlist is empty. Add items: python watchlist.py add "Item Name"')
        return
    print(f"Watchlist ({len(tickers)} items):")
    for i, t in enumerate(tickers, 1):
        print(f"  {i}. {t}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python watchlist.py add "Item Market Hash Name"')
        print('  python watchlist.py remove "Item Market Hash Name"')
        print('  python watchlist.py list')
        print('  python watchlist.py clear')
        return
    cmd = sys.argv[1].lower()
    if cmd == "add" and len(sys.argv) >= 3:
        add(sys.argv[2])
    elif cmd == "remove" and len(sys.argv) >= 3:
        remove(sys.argv[2])
    elif cmd == "list":
        list_items()
    elif cmd == "clear":
        save_watchlist([])
        print("Watchlist cleared")
    else:
        print("Invalid command. Usage: python watchlist.py [add|remove|list|clear] [name]")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 验证 list 命令**

Run: `python watchlist.py list`
Expected: 输出 `Watchlist is empty. Add items: ...`

- [ ] **Step 4: 验证 add 命令（真实饰品）**

Run: `python watchlist.py add "AK-47 | Redline (Field-Tested)"`
Expected: 输出 `Verifying ...` 然后 `Added: AK-47 | Redline (Field-Tested)`

- [ ] **Step 5: 再次验证 list**

Run: `python watchlist.py list`
Expected: 输出 `Watchlist (1 items):` 含刚添加的饰品

- [ ] **Step 6: Commit**

```bash
git add watchlist.py config/watchlist.yaml
git commit -m "feat: add watchlist management CLI for monitoring CS2 items"
```

***

### Task 2: 微信推送模块 (notify.py 推送部分)

**Files:**

- Create: `notify.py`
- Modify: `.env.example` — 增加推送配置项
- Modify: `requirements.txt` — 增加 schedule（为 Task 4 预装）

**Interfaces:**

- Consumes: `os.getenv("PUSH_PROVIDER")` / `os.getenv("PUSH_TOKEN")`
- Produces: `send_wechat(title, content) -> bool` — 供 daily.py 调用
- [ ] **Step 1: 在 .env.example 末尾追加推送配置**

在 `.env.example` 末尾追加：

```
#### WeChat Push ####
# Provider: serverchan (Server酱) or pushplus
PUSH_PROVIDER=serverchan
# Server酱 sendkey or pushplus token
PUSH_TOKEN=YOUR_TOKEN_HERE
```

- [ ] **Step 2: 在 requirements.txt 增加 schedule 依赖**

在 Utilities 段末尾追加：

```
schedule>=1.2.0
```

- [ ] **Step 3: 安装新依赖**

Run: `pip install schedule`
Expected: `Successfully installed schedule-1.x.x`

- [ ] **Step 4: 创建 notify.py（推送部分）**

```python
"""Notification module - generate advice report and push to WeChat."""

import os
import sqlite3
import json
from datetime import datetime
import requests
from database.cs2_sqlite_setup import CS2_DB_PATH
from util.logger import logger


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
        resp = requests.post(url, data={"title": title, "desp": content}, timeout=15)
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
        resp = requests.post(url, json={"token": token, "title": title, "content": content, "template": "markdown"}, timeout=15)
        data = resp.json()
        if data.get("code") == 200:
            logger.info("pushplus push success")
            return True
        logger.error(f"pushplus push failed: {data.get('msg', data)}")
        return False
    except Exception as e:
        logger.error(f"pushplus push error: {e}")
        return False
```

- [ ] **Step 5: 验证推送函数可 import**

Run: `python -c "from notify import send_wechat; print('import ok')"`
Expected: 输出 `import ok`

- [ ] **Step 6: 验证无 token 时安全跳过**

Run: `python -c "from notify import send_wechat; print(send_wechat('test','test'))"`
Expected: 输出 `False`（因为 .env 里 PUSH\_TOKEN 未设置）

- [ ] **Step 7: Commit**

```bash
git add notify.py .env.example requirements.txt
git commit -m "feat: add WeChat push module (Server酱/pushplus)"
```

***

### Task 3: 建议报告生成 (notify.py 报告部分)

**Files:**

- Modify: `notify.py` — 追加 generate\_report 函数

**Interfaces:**

- Consumes: SQLite 表 cs2\_config / cs2\_portfolio / cs2\_decision（现有 schema，参考 view\.py 查询模式）
- Produces: `generate_report(exp_name, trading_date) -> str` — 返回 Markdown 格式建议文案，供 daily.py 调用
- [ ] **Step 1: 在 notify.py 末尾追加 generate\_report 函数**

在 `notify.py` 文件末尾追加：

```python
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

    # 3. decisions by portfolio_id
    cursor.execute(
        "SELECT item_name, action, quantity, price, justification "
        "FROM cs2_decision WHERE portfolio_id = ? ORDER BY item_name",
        (portfolio_id,),
    )
    decisions = cursor.fetchall()
    conn.close()

    # Build report
    lines = []
    lines.append(f"# CS2 交易建议 {trading_date}")
    lines.append("")
    lines.append(f"**总资产**: ${total_assets:.2f} | **现金**: ${cash:.2f}")
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
            lines.append(f"- **{name}**: 买 {qty} 件 @ ${price:.2f}")
            lines.append(f"  - {just}")
        lines.append("")
    if sell_items:
        lines.append("### 卖出建议")
        for name, _, qty, price, just in sell_items:
            lines.append(f"- **{name}**: 卖 {qty} 件 @ ${price:.2f}")
            lines.append(f"  - {just}")
        lines.append("")
    if hold_items:
        lines.append("### 观望")
        for name, _, _, price, just in hold_items:
            lines.append(f"- **{name}** @ ${price:.2f}")
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
            lines.append(f"- {name}: {shares} 件 (价值 ${value:.2f})")
        lines.append("")

    lines.append("---")
    lines.append("_本建议由多智能体 LLM 系统生成，仅供参考，不构成投资指令_")

    return "\n".join(lines)
```

- [ ] **Step 2: 验证 generate\_report 可 import**

Run: `python -c "from notify import generate_report; print('import ok')"`
Expected: 输出 `import ok`

- [ ] **Step 3: 验证对已有实验生成报告**

Run: `python -c "from notify import generate_report; print(generate_report('T-ds', '2026-07-26')[:200])"`
Expected: 输出以 `# CS2 交易建议 2026-07-26` 开头的报告片段（复用库里已有 T-ds 实验数据）

- [ ] **Step 4: Commit**

```bash
git add notify.py
git commit -m "feat: add advice report generation from DB decisions"
```

***

### Task 4: 每日运行入口 (daily.py + config/live.yaml)

**Files:**

- Create: `config/live.yaml`
- Create: `daily.py`

**Interfaces:**

- Consumes: `run.run_single_experiment(config_path, trading_date, use_local_db)` — 现有函数，复用全部 workflow 逻辑
- Consumes: `notify.generate_report` / `notify.send_wechat`
- Consumes: `util.cs2_db_helper.cs2_db_initialize` / `get_cs2_db` — 检查今日是否已跑
- Produces: `daily.py --now`（立即跑一次）和 `daily.py --daemon`（常驻 20:00 调度）
- [ ] **Step 1: 创建 config/live.yaml**

```yaml
# Live advisor configuration - used by daily.py
exp_name: "live-advisor"

cashflow: 10000

# tickers 由 daily.py 从 config/watchlist.yaml 动态注入到临时配置
tickers: []

# 仅技术分析（不依赖 Reddit/Steam CSV 预抓取，SteamDT 实时数据即可运行）
workflow_analysts:
  - technical

llm:
  provider: "DeepSeek"
  model: "deepseek-chat"

planner_mode: false
multi_item_mode: true
enable_transaction_fee: true
```

- [ ] **Step 2: 创建 daily.py**

```python
"""Daily advisor entry - run analysis at 20:00 and push advice to WeChat.

Usage:
  python daily.py --now       # Run analysis immediately and push
  python daily.py --daemon     # Run as scheduler daemon (20:00 daily)
"""

import sys
import os
import argparse
import yaml
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from run import run_single_experiment
from notify import generate_report, send_wechat
from util.cs2_db_helper import cs2_db_initialize, get_cs2_db
from util.logger import logger

load_dotenv()

PROJECT_ROOT = Path(__file__).parent
LIVE_CONFIG = PROJECT_ROOT / "config" / "live.yaml"
WATCHLIST = PROJECT_ROOT / "config" / "watchlist.yaml"


def load_watchlist_tickers():
    if not WATCHLIST.exists():
        return []
    with open(WATCHLIST, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tickers", [])


def is_already_run_today(exp_name):
    """Check if today's analysis already exists in DB."""
    cs2_db_initialize(use_local_db=True)
    db = get_cs2_db()
    config_id = db.get_config_id_by_name(exp_name)
    if not config_id:
        return False
    latest = db.get_latest_trading_date(config_id)
    if latest and latest.date() == datetime.now().date():
        return True
    return False


def run_today():
    """Run analysis for today and push advice."""
    today = datetime.now().strftime("%Y-%m-%d")
    tickers = load_watchlist_tickers()
    if not tickers:
        logger.warning("Watchlist is empty, nothing to analyze")
        print('Watchlist is empty. Add items first: python watchlist.py add "Item Name"')
        return False

    # Load live config and inject watchlist tickers
    with open(LIVE_CONFIG, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["tickers"] = tickers
    exp_name = cfg["exp_name"]

    # Skip analysis if already done today
    if is_already_run_today(exp_name):
        logger.info(f"Today's analysis already done, pushing report only")
    else:
        # Write merged config to temp file for run_single_experiment
        temp_path = PROJECT_ROOT / "config" / "_live_today.yaml"
        with open(temp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        try:
            logger.info(f"Running daily analysis for {today} with {len(tickers)} items")
            run_single_experiment("config/_live_today.yaml", today, use_local_db=True)
        finally:
            temp_path.unlink(missing_ok=True)

    # Generate and push report
    report = generate_report(exp_name, today)
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60 + "\n")
    send_wechat(f"CS2交易建议 {today}", report)
    logger.info("Daily advisor finished")
    return True


def main():
    parser = argparse.ArgumentParser(description="Daily CS2 trading advisor")
    parser.add_argument("--now", action="store_true", help="Run analysis immediately and push")
    parser.add_argument("--daemon", action="store_true", help="Run as scheduler daemon (20:00 daily)")
    args = parser.parse_args()

    if args.now:
        run_today()
    elif args.daemon:
        import schedule
        import time
        schedule.every().day.at("20:00").do(run_today)
        logger.info("Scheduler started, will run at 20:00 every day. Ctrl+C to stop.")
        print("调度已启动，每天 20:00 自动运行分析并推送建议。Ctrl+C 退出。")
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 验证 --now 在 watchlist 为空时安全提示**

Run: `python watchlist.py clear` 然后 `python daily.py --now`
Expected: 输出 `Watchlist is empty. Add items first: ...`

- [ ] **Step 4: 添加一个饰品并立即运行**

Run: `python watchlist.py add "Glove Case"` 然后 `python daily.py --now`
Expected: 调用 SteamDT 获取数据、跑 LLM 分析、打印报告并尝试推送（无 token 则 push 返回 False）

- [ ] **Step 5: 重复运行验证幂等（不重复分析）**

Run: `python daily.py --now`（再次）
Expected: 日志输出 `Today's analysis already done, pushing report only`，直接生成报告不重复调 LLM

- [ ] **Step 6: 验证 daemon 模式可启动（Ctrl+C 退出）**

Run: `python daily.py --daemon`
Expected: 输出 `调度已启动，每天 20:00 自动运行...`，进程常驻，Ctrl+C 退出

- [ ] **Step 7: Commit**

```bash
git add daily.py config/live.yaml
git commit -m "feat: add daily advisor entry with 20:00 scheduler and WeChat push"
```

***

## Self-Review

**1. Spec coverage:**

- 每日定时 → Task 4 daily.py --daemon (schedule 20:00)
- 纯建议报告 → Task 3 generate\_report 输出 Markdown，不自动下单
- 微信推送 → Task 2 send\_wechat (Server酱/pushplus)
- 可配置标的 → Task 1 watchlist.py add/remove/list
- 用户选饰品后查询信息存config → Task 1 verify\_item 调 SteamDT 验证后写 watchlist.yaml
- 晚上20:00 → Task 4 schedule.every().day.at("20:00")

**2. Placeholder scan:** 无 TBD/TODO，每个代码步骤含完整可运行代码。

**3. Type consistency:**

- `send_wechat(title: str, content: str) -> bool` — Task 2 定义，Task 4 调用签名一致
- `generate_report(exp_name: str, trading_date: str) -> str` — Task 3 定义，Task 4 调用签名一致
- `run_single_experiment(config_path: str, trading_date: str, use_local_db: bool)` — 现有函数，Task 4 调用一致
- watchlist.yaml 的 `tickers` 字段名在 watchlist.py 和 daily.py 中一致

***

## 使用流程（部署后）

1. 配置推送：编辑 `.env`，设置 `PUSH_PROVIDER=serverchan` 和 `PUSH_TOKEN=你的sendkey`
2. 添加监控标的：`python watchlist.py add "AK-47 | Redline (Field-Tested)"`
3. 启动常驻调度：`python daily.py --daemon`
4. 每天 20:00 收到微信推送的买卖建议
5. 手动触发：`python daily.py --now`

