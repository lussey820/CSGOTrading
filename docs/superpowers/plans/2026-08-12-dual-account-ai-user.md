# AI 端 / 用户端 双账户架构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把系统拆成两个独立端口——AI 端(虚拟账户,关注列表作投资池,分析→决策→自动记账全自动)和用户端(今日建议,真实库存+成本,用户手工记账),并保持每次决策为多个独立 LLM 请求。

**Architecture:** 两端共用同一套决策管线(technical/vision 分析师 → 风控 → portfolio manager,已天然是多次 LLM 请求)。区别在账户来源与结算方式:

- **AI 端**:配置 `config/ai_account.yaml`(exp_name=ai-account, cashflow=10000, `auto_settle: true`),tickers 从关注列表(watchlist)注入,不灌真实库存。决策后 `update_portfolio_ticker` 自动记账,卖出决策自动结算已实现盈亏(扣 2% 手续费)。

- **用户端**:现有今日建议流程(seed_from_inventory=true),新增把真实成本(buy_price)作为持仓成本灌入,决策 prompt 持 仓成本,用户手工记账。

- **AI持仓页**:显示 AI 端虚拟账户的持仓(未实现盈亏)+ 已实现盈亏 + 交易记录,全部来自虚拟账户与 cs2_ai_trade,不再用真实库存。

**Tech Stack:** Python 3.13 + Flask + SQLite(assets/cs2.db)+ pydantic + Jinja2。

## Global Constraints

- 所有金额单位为人民币 ¥(SteamDT 平台价即为人民币)。

- 每个饰品的决策流程必须保持多请求:technical 分析 1 次、vision 分析 1 次、风控 1 次、最终决策 1 次(现状已满足,不得合并)。

- 旧配置字段必须兼容:新字段(avg_cost / auto_settle)用 `.get(key, default)` 读取,老数据缺失时兜底为 0/False。

- 关注列表(watchlist)是 AI 端唯一投资池;今日建议(用户端)继续用真实库存作为分析池。

- 禁止改动 daily.py 的 watchlist→微信推送流程。

- 修改 SQLite 表结构时,沿用 `ALTER TABLE ... ADD COLUMN` + `except sqlite3.OperationalError: pass` 的幂等写法(本次实际不需要加表列,positions JSON 内嵌 avg_cost)。

---

### Task 1: Position 增加 avg_cost 持仓成本字段

**Files:**

- Modify: `graph/schema.py:39-48`

- Verify: 临时脚本验证序列化

**Interfaces:**

- Produces: `Position` 新增字段 `avg_cost: float = 0.0`(加权平均持仓成本),`model_dump()` 会包含该字段,cs2_portfolio.positions JSON 随之携带。

- [ ] **Step 1: 修改 Position**

```python
class Position(BaseModel):
    """Position for a single ticker"""
    value: float = Field(
        default=0.0,
        description="Monetary value for the position."
    )
    shares: int = Field(
        default=0,
        description="Shares for the position."
    )
    avg_cost: float = Field(
        default=0.0,
        description="加权平均持仓成本(每份),用于计算浮盈浮亏与卖出结算。"
    )
```

- [ ] **Step 2: 验证**

```bash
python -c "from graph.schema import Position; p=Position(shares=2,value=890,avg_cost=445); print(p.model_dump()); assert p.avg_cost==445; print('OK')"
```

Expected: `{'value': 890.0, 'shares': 2, 'avg_cost': 445.0}` 后输出 `OK`。

- [ ] **Step 3: Commit**

```bash
git add graph/schema.py
git commit -m "feat: add avg_cost to Position for holding cost tracking"
```

---

### Task 2: 虚拟持仓成本记账 + 用户端灌真实成本

**Files:**

- Modify: `graph/workflow.py:37-49`(seed_from_inventory 段)、`graph/workflow.py:164-204`(update_portfolio_ticker)

**Interfaces:**

- Consumes: Task 1 的 `Position.avg_cost`

- Produces: `update_portfolio_ticker` 在 BUY 时维护加权平均成本;`seed_from_inventory` 时把库存 `buy_price` 写入 `avg_cost`。

- [ ] **Step 1: seed_from_inventory 写入成本**

把 `graph/workflow.py:37-49` 的灌入块改为(替换 `pos.value = ...` 一行后新增):

```python
        # 今日建议模式:用真实库存持仓覆盖模拟组合,让决策引擎看到真实持仓(数量+成本)
        if config.get("seed_from_inventory", False):
            from inventory import list_items
            for it in list_items(with_market=False):
                name = it["item_name"]
                shares = int(it.get("shares") or 0)
                pos = self.init_portfolio.positions.setdefault(name, Position())
                pos.shares = shares
                pos.value = round(float(it.get("buy_price") or 0) * shares, 2)
                pos.avg_cost = float(it.get("buy_price") or 0) if shares > 0 else 0.0
            logger.info(
                "Seeded portfolio positions from real inventory: "
                + ", ".join(f"{k}={v.shares}@{v.avg_cost}" for k, v in self.init_portfolio.positions.items())
            )
```

- [ ] **Step 2: BUY 加权平均成本**

把 `graph/workflow.py` 的 BUY 分支改为:

```python
        if action == Action.BUY:
            # There is no transaction fee for purchase
            max_affordable_shares = int(portfolio.cashflow // price) if price > 0 else 0
            actual_shares = min(shares, max_affordable_shares)

            old_shares = portfolio.positions[ticker].shares
            old_cost = portfolio.positions[ticker].avg_cost
            portfolio.positions[ticker].shares = old_shares + actual_shares
            # 加权平均成本
            if old_shares + actual_shares > 0:
                portfolio.positions[ticker].avg_cost = round(
                    (old_shares * old_cost + actual_shares * price) / (old_shares + actual_shares), 2
                )
            portfolio.cashflow -= price * actual_shares
```

- [ ] **Step 3: 验证(临时脚本** **`_test_cost.py`)**

```python
import sys
from graph.schema import Portfolio, Position

p = Portfolio(id="x", cashflow=10000.0, positions={"AK": Position(shares=1, value=190, avg_cost=190)})
# 模拟 BUY 加权成本:1 @190 + 1 @210 = 2 @200
pos = p.positions["AK"]
old_s, old_c, price, add = pos.shares, pos.avg_cost, 210.0, 1
pos.shares = old_s + add
pos.avg_cost = round((old_s * old_c + add * price) / (old_s + add), 2)
assert pos.shares == 2 and pos.avg_cost == 200.0, (pos.shares, pos.avg_cost)
print("OK", pos.model_dump())
```

Run: `python _test_cost.py` → Expected: `OK {'value': 0.0, 'shares': 2, 'avg_cost': 200.0}`,随后删除该脚本。

- [ ] **Step 4: Commit**

```bash
git add graph/workflow.py
git commit -m "feat: track weighted avg cost in portfolio positions"
```

---

### Task 3: 决策 prompt 携带持仓成本

**Files:**

- Modify: `llm/prompt.py:145-170`(PORTFOLIO_PROMPT)、`llm/prompt.py:172-194`(PORTFOLIO_PROMPT_NO_FEE)

- Modify: `agents/portfolio_manager.py:85-104`

**Interfaces:**

- Consumes: Task 2 的 `portfolio.positions[ticker].avg_cost`

- Produces: `portfolio_agent` 在构建 prompt 时计算 `avg_cost` 并传给两个 prompt 模板。

- [ ] **Step 1: 修改 PORTFOLIO_PROMPT**

```python
Current Price: {current_price}
Holding Shares: {current_shares}
Average Cost (持仓成本): {avg_cost}
Tradable Shares: {tradable_shares}

Trading friction: selling fee {transaction_fee_rate_pct:.2f}% (applies to sells only).

Rules:
- If tradable_shares > 0: you may buy (no fee on buy).
- If tradable_shares < 0: you may sell; ensure expected downside risk outweighs sell fee.
- If tradable_shares ≈ 0 or expected gain < sell-fee impact: choose Hold.
- Compare current price against your average cost: if current price < avg_cost you hold a floating loss; factor this into Buy/Hold/Sell.
- Ensure expected profit after (sell) fees is positive; otherwise Hold.
```

- [ ] **Step 2: 修改 PORTFOLIO_PROMPT_NO_FEE**

```python
Current Price: {current_price}
Holding Shares: {current_shares}
Average Cost (持仓成本): {avg_cost}
Tradable Shares: {tradable_shares}

Rules:
- If tradable_shares > 0: you may buy.
- If tradable_shares < 0: you may sell.
- If tradable_shares ≈ 0: choose Hold.
- Compare current price against your average cost: if current price < avg_cost you hold a floating loss; factor this into your decision.
```

- [ ] **Step 3: portfolio_manager 计算并传入 avg_cost**

把 `agents/portfolio_manager.py:85-104` 改为(在 `calculate_ticker_shares` 之后、`enable_transaction_fee` 分支之前插入):

```python
    decision_memory = db.get_decision_memory(exp_name, ticker, thresholds["decision_memory_limit"])
    current_shares, tradable_shares = calculate_ticker_shares(portfolio, current_price, ticker, position_risk.optimal_position_ratio)

    # 持仓成本(加权平均),无持仓时为 0
    pos = portfolio.positions.get(ticker)
    avg_cost = pos.avg_cost if pos else 0.0

    # make trading decision
    if enable_transaction_fee:
        prompt = PORTFOLIO_PROMPT.format(
            decision_memory=decision_memory,
            current_price=current_price,
            current_shares=current_shares,
            avg_cost=avg_cost,
            tradable_shares=tradable_shares,
            transaction_fee_rate=TRANSACTION_FEE_RATE,
            transaction_fee_rate_pct=TRANSACTION_FEE_RATE * 100,
        )
    else:
        # Use prompt without transaction fee mention
        prompt = PORTFOLIO_PROMPT_NO_FEE.format(
            decision_memory=decision_memory,
            current_price=current_price,
            current_shares=current_shares,
            avg_cost=avg_cost,
            tradable_shares=tradable_shares,
        )
```

- [ ] **Step 4: 验证**

```bash
python -m py_compile llm/prompt.py agents/portfolio_manager.py; echo "COMPILE OK"
```

Expected: `COMPILE OK`。

```bash
python -c "from llm.prompt import PORTFOLIO_PROMPT; s=PORTFOLIO_PROMPT.format(decision_memory='[]', current_price=207.9, current_shares=1, avg_cost=445.0, tradable_shares=-1, transaction_fee_rate=0.02, transaction_fee_rate_pct=2.0); assert 'Average Cost (持仓成本): 445.0' in s; print('FORMAT OK')"
```

Expected: `FORMAT OK`。

- [ ] **Step 5: Commit**

```bash
git add llm/prompt.py agents/portfolio_manager.py
git commit -m "feat: pass holding avg cost into portfolio decision prompt"
```

---

### Task 4: AI 端卖出自动结算

**Files:**

- Modify: `ai_trader.py`(新增 `auto_settle_sell`,置于 `record_sell_from_decision` 之后)

- Modify: `graph/workflow.py:24`(**init** 存 self.auto_settle)、`graph/workflow.py:178-196`(SELL 分支)

**Interfaces:**

- Consumes: Task 1 的 `avg_cost`;`record_sell_from_decision(decision_id, item_name, shares, trade_date, buy_price, sell_price)`

- Produces: `auto_settle_sell(item_name, shares, sell_price, trade_date, avg_cost) -> Optional[Dict]`,由 workflow 在卖出决策落地后调用。

- [ ] **Step 1: ai_trader.py 新增 auto_settle_sell**

在 `record_sell_from_decision` 函数之后插入:

```python
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
```

- [ ] **Step 2: workflow\.py 存 auto_settle 标志**

在 `AgentWorkflow.__init__` 中,`self.enable_transaction_fee = config.get('enable_transaction_fee', True)` 之后新增:

```python
        # AI 端:卖出决策后自动结算已实现盈亏
        self.auto_settle = config.get('auto_settle', False)
```

- [ ] **Step 3: SELL 分支自动结算**

把 `graph/workflow.py` 的 SELL 分支(`elif action == Action.SELL:` 整块)改为:

```python
        elif action == Action.SELL:
            # safety limit: ensure no negative position
            max_sellable_shares = portfolio.positions[ticker].shares
            actual_shares = min(shares, max_sellable_shares)

            portfolio.positions[ticker].shares -= actual_shares
            # Apply transaction fee only if enabled
            if enable_transaction_fee:
                portfolio.cashflow += price * actual_shares * (1 - TRANSACTION_FEE_RATE)
            else:
                portfolio.cashflow += price * actual_shares

            # AI 端:卖出落地即自动结算已实现盈亏(扣 2% 手续费)
            if getattr(self, "auto_settle", False) and actual_shares > 0:
                from ai_trader import auto_settle_sell
                trade_date = str(self.trading_date)[:10] if self.trading_date else None
                auto_settle_sell(
                    item_name=ticker,
                    shares=actual_shares,
                    sell_price=price,
                    trade_date=trade_date,
                    avg_cost=portfolio.positions[ticker].avg_cost,
                )

            # log limited sell order
            if actual_shares < shares:
                fee_info = f"fee_rate={TRANSACTION_FEE_RATE:.2%}" if enable_transaction_fee else "no fee"
                logger.warning(
                    f"Limited sell order for {ticker}: requested {shares}, actual {actual_shares} "
                    f"({fee_info}, max: {max_sellable_shares})"
                )
```

- [ ] **Step 4: 验证(临时脚本** **`_test_settle.py`)**

```python
from ai_trader import auto_settle_sell
import sqlite3
from database.cs2_sqlite_setup import CS2_DB_PATH

# 构造一条测试决策(仅用于验证关联,不污染真实决策)
conn = sqlite3.connect(CS2_DB_PATH)
conn.execute("DELETE FROM cs2_ai_trade WHERE item_name = '__TEST__'")
conn.commit()
conn.close()

row = auto_settle_sell("__TEST__", 2, 210.0, "2026-08-12", 200.0)
print("auto_settle row:", row)
assert row is not None and row["shares"] == 2 and abs(row["realized_pnl"] - (20*2 - 210*2*0.02)) < 0.01, row
print("SETTLE OK")
```

Run: `python _test_settle.py` → Expected: 输出 auto_settle row 与 `SETTLE OK`;随后删除脚本并清理测试数据:

```bash
python -c "import sqlite3; from database.cs2_sqlite_setup import CS2_DB_PATH; c=sqlite3.connect(CS2_DB_PATH); c.execute(\"DELETE FROM cs2_ai_trade WHERE item_name='__TEST__'\"); c.commit(); c.close(); print('cleaned')"
```

Expected: `cleaned`。

- [ ] **Step 5: Commit**

```bash
git add ai_trader.py graph/workflow.py
git commit -m "feat: auto settle realized PnL on AI account sell decisions"
```

---

### Task 5: AI 端运行器 + 配置

**Files:**

- Create: `config/ai_account.yaml`

- Create: `ai_account.py`

- Verify: 临时脚本 dry-run

**Interfaces:**

- Consumes: `run_single_experiment(config_path, trading_date, use_local_db=True)`、`watchlist.load_watchlist()`

- Produces:
  - `run_ai_account(items=None) -> Dict`(同步跑一轮,返回 `{ok, total, success, failed, results}`)

  - `run_ai_account_background() -> bool`(后台线程启动,返回是否成功)

  - `get_run_status() -> Dict`(运行状态)

- [ ] **Step 1: 创建 config/ai_account.yaml**

```yaml
# AI 端虚拟账户配置 - 全自动分析→决策→自动记账
exp_name: "ai-account"

cashflow: 10000

# tickers 由 ai_account.py 从 config/watchlist.yaml(关注列表)动态注入
tickers: []

# 技术分析 + 视觉K线图分析(SteamDT 截图 → qwen-vl-plus)
workflow_analysts:
  - technical
  - vision

llm:
  provider: "DeepSeek"
  model: "deepseek-chat"

planner_mode: false
multi_item_mode: true
enable_transaction_fee: true

# AI 端:卖出决策后自动结算已实现盈亏
auto_settle: true
```

- [ ] **Step 2: 创建 ai_account.py**

```python
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
```

- [ ] **Step 3: 验证**

```bash
python -m py_compile ai_account.py; echo "COMPILE OK"
```

Expected: `COMPILE OK`。

```bash
python -c "import yaml; cfg=yaml.safe_load(open('config/ai_account.yaml', encoding='utf-8')); assert cfg['exp_name']=='ai-account' and cfg['auto_settle'] is True; print('CONFIG OK', cfg['exp_name'])"
```

Expected: `CONFIG OK ai-account`。

- [ ] **Step 4: Commit**

```bash
git add config/ai_account.yaml ai_account.py
git commit -m "feat: add AI virtual account runner with watchlist investment pool"
```

---

### Task 6: AI持仓页展示虚拟账户 + webui 定时/接口/按钮

**Files:**

- Modify: `ai_trader.py`(新增 `_virtual_holdings`,改造 `get_ai_trader_summary` holdings 段)

- Modify: `webui.py`(新增 ai-account 路由 + 定时器)

- Modify: `templates/ai_trader.html`(加"运行AI账户"按钮与状态)

**Interfaces:**

- Consumes: Task 5 的 `ai_account.get_run_status() / run_ai_account_background()`

- Produces: `_virtual_holdings() -> List[Dict]`(AI 账户持仓+未实现盈亏);webui 路由 `POST /api/ai-account/run`、`GET /api/ai-account/status`。

- [ ] **Step 1: ai_trader.py 新增 \_virtual_holdings**

在 `get_ai_trader_summary` 之前插入:

```python
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
```

- [ ] **Step 2: 改造 get_ai_trader_summary 的 holdings 段**

把 `get_ai_trader_summary` 中:

```python
    # 中文名映射:从库存表取官方中文名(缺失时回退英文名)
    inv_items = list_items(with_market=True)
    cn_map = {
        it["item_name"]: (it.get("item_name_cn") or it["item_name"])
        for it in inv_items
    }
```

改为(去掉实时价拉取,只用于中文名映射):

```python
    # 中文名映射:从库存表取官方中文名(缺失时回退英文名)
    inv_items = list_items(with_market=False)
    cn_map = {
        it["item_name"]: (it.get("item_name_cn") or it["item_name"])
        for it in inv_items
    }
```

并把:

```python
    # merge with current inventory unrealized P&L
    holdings = []
    total_unrealized = 0.0
    for it in inv_items:
        unreal = it.get("total_pnl") or 0.0
        total_unrealized += unreal
        holdings.append(
            {
                "item_name": it["item_name"],
                "item_name_cn": it.get("item_name_cn") or it["item_name"],
                "shares": it["shares"],
                "buy_price": it["buy_price"],
                "current_price": it.get("current_price"),
                "unrealized_pnl": unreal,
                "unrealized_pnl_pct": it.get("total_pnl_pct"),
            }
        )
```

改为:

```python
    # AI 虚拟账户持仓 + 未实现盈亏(虚拟账户,非真实库存)
    holdings = _virtual_holdings()
    total_unrealized = 0.0
    for h in holdings:
        total_unrealized += h["unrealized_pnl"] or 0.0
```

- [ ] **Step 3: 验证**

```bash
python -m py_compile ai_trader.py; echo "COMPILE OK"
```

Expected: `COMPILE OK`。

```bash
python -c "from ai_trader import _virtual_holdings; hs=_virtual_holdings(); print('virtual holdings:', [(h['item_name'], h['shares'], h['buy_price']) for h in hs])"
```

Expected: 输出虚拟账户持仓(可能为空列表,取决于是否已运行过 AI 端)。

- [ ] **Step 4: webui.py 新增路由与定时器**

在 webui.py 顶部 import 区(ai_trader_mod 附近)新增:

```python
import ai_account as ai_account_mod
```

在 AI-trader API 区之后新增:

```python
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
```

在 `def _schedule_ai_account()` 附近新增定时器(webui 的 main 启动逻辑里调用一次):

```python
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
```

并在 webui 启动处(调用 STARTUP_WARMUP 附近)新增 `_schedule_ai_account()`。

- [ ] **Step 5: ai_trader.html 加运行按钮**

在 `templates/ai_trader.html` 的页头按钮区(现有 `#btn-refresh` 附近)改为:

```html
<div class="flex gap-3">
  <button id="btn-ai-run" class="btn">运行 AI 账户</button>
  <button id="btn-refresh" class="btn btn-ghost">刷新</button>
</div>
```

并在该页 scripts 区新增:

```javascript
document.getElementById("btn-ai-run").addEventListener("click", async () => {
  const btn = document.getElementById("btn-ai-run");
  btn.disabled = true;
  btn.textContent = "运行中...";
  try {
    const r = await fetchJSON("/api/ai-account/run", {
      method: "POST",
      body: "{}",
    });
    showToast(
      r.ok ? "AI 账户分析已启动(后台运行)" : r.error || "启动失败",
      r.ok ? "success" : "error",
    );
  } catch (e) {
    showToast("启动失败: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "运行 AI 账户";
  }
});
```

(若该页面已有 `fetchJSON` 与 `showToast` 助手函数,直接复用;没有则在 scripts 顶部补齐。)

- [ ] **Step 6: 验证**

```bash
python -m py_compile webui.py; echo "COMPILE OK"
```

Expected: `COMPILE OK`。

启动 webui 后验证:

```bash
curl.exe -s -X POST http://127.0.0.1:5000/api/ai-account/run
curl.exe -s http://127.0.0.1:5000/api/ai-account/status
```

Expected: 第一次返回 `{"ok": true, "status": "started"}`,第二次返回含 `running/started_at` 的状态 JSON(若关注列表为空,run 接口返回错误信息)。

- [ ] **Step 7: Commit**

```bash
git add ai_trader.py webui.py templates/ai_trader.html
git commit -m "feat: show AI virtual account in AI trader page, add scheduler and run API"
```

---

## Self-Review 结果

- **Spec 覆盖**:AI端全自动(分析→决策→自动记账→卖出自动结算)= Task 2/4/5;用户端今日建议含真实持仓+成本 = Task 2/3(seed avg_cost + prompt);AI持仓页显示虚拟账户 = Task 6;关注列表作为 AI 端投资池 = Task 5;多请求架构 = 现有管线,Global Constraints 声明不改动。

- **占位符扫描**:无 TODO/TBD,所有任务含完整代码与验证命令。

- **类型一致性**:`Position.avg_cost`、`auto_settle_sell` 签名、`_virtual_holdings` 返回字段在后续任务中引用一致;`record_sell_from_decision` 已支持传入 `buy_price` 故 AI 端不误查库存。

- **遗留风险**:`get_ai_trader_summary` 的 `cn_map` 改用 `list_items(with_market=False)`,不再拉实时价,AI持仓页未实现盈亏完全来自 `_virtual_holdings`(虚拟账户),符合需求。
