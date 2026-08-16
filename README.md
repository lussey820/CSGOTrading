# CSGOTrading:CS2 市场交易多智能体 LLM 框架

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![LangGraph](https://img.shields.io/badge/LangGraph-Enabled-green.svg)](https://github.com/langchain-ai/langgraph)

*基于大语言模型(LLM)的智能多智能体系统,用于《反恐精英 2》(CS2)饰品市场的自动化分析与交易决策*

</div>

---

## 📋 项目简介

**CSGOTrading** 将大语言模型(LLM)应用于 CS2 饰品交易领域。系统基于 LangGraph 构建层级智能体架构,包含专业分析师智能体(技术分析、视觉分析、情绪分析、流动性、事件驱动),由元规划器智能体统一协调。组合管理智能体综合多模态市场信号生成交易决策,并考虑交易成本与风控约束。

核心特性:

- **多智能体架构**:模块化设计,针对不同市场维度配置专业分析师智能体
- **动态智能体选择**:元规划器根据市场状况自适应选择相关分析师
- **多源数据集成**:无缝整合 CS2 市场数据、Steam 新闻与 Reddit 情绪
- **可配置工作流**:支持智能体工作流与直接 LLM 分析两种模式
- **风险感知的组合管理**:精细化的仓位管理并建模交易成本
- **Web 仪表盘**:库存管理、今日建议、AI 持仓盈亏、决策对照等可视化功能

---

## 🎯 系统概念

<div align="center">
<img src="figs/concept.jpg" alt="系统概念图" width="800"/>
</div>

---

## 🏗️ 系统架构

<div align="center">
<img src="figs/overallstructure.jpg" alt="总体架构图" width="800"/>
</div>

### 智能体说明

| 智能体 | 功能 | 输入 | 输出 |
|-------|----------|-------|--------|
| **规划器(Planner)** | 根据市场环境选择相关分析师 | 标的、可用分析师 | 选中的分析师列表 |
| **技术分析师** | 分析价格形态与趋势 | 历史价格数据 | 技术信号(BUY/SELL/HOLD) |
| **视觉分析师** | 解读 K 线截图 | 行情截图 | 视觉技术信号 |
| **情绪分析师** | 分析社区情绪 | Reddit 帖子、Steam 新闻 | 情绪得分与方向 |
| **流动性分析师** | 评估市场深度与成交量 | 订单簿、成交量 | 流动性评估 |
| **事件分析师** | 识别影响市场的事件 | 新闻、更新 | 事件影响分析 |
| **组合经理** | 执行风险感知的交易决策 | 分析师信号、组合状态 | 带仓位的交易动作 |

---

## ✨ 核心特性

### 🤖 多模态分析
- **技术分析**:价格行为、趋势识别、支撑/阻力位
- **视觉分析**:基于 K 线截图的视觉研判(SteamDT 截图 → 视觉大模型)
- **情绪分析**:从 Reddit 与 Steam 社区提取 NLP 情绪
- **流动性分析**:基于市场深度与成交量的评估
- **事件驱动分析**:游戏更新与新闻的事件影响评估

### 🧠 智能体协同
- **元规划器**:为每个标的动态选择最优分析师组合
- **模块化设计**:可扩展的智能体注册系统
- **灵活工作流**:同时支持智能体模式与直接 LLM 模式

### 💼 组合管理与风控
- **风控约束**:最大仓位比例限制与回撤保护
- **交易成本**:真实建模 2% 交易手续费
- **仓位计算**:多资产间的智能分配
- **补仓纪律**:逢低分批补仓(浮亏 ≥5% 触发,深亏 ≥30% 额度减半)
- **止损减仓**:深亏 + 破位止损、防御性减仓下限
- **7 天交易 CD**:Steam 市场 7 天冷却期,可卖份额按买入批次(FIFO)计算
- **状态持久化**:数据库完整记录组合历史

### 🔧 生产级基础设施
- **数据库**:本地 SQLite(零配置,单文件)
- **多 LLM 提供商**:DeepSeek、OpenAI、Anthropic、Gemini、Kimi、Qwen 等
- **价格缓存**:SteamDT 实时价 + 300 秒 TTL 内存缓存 + 页面后台预热
- **图像识别**:饰品图片一键识别入库(OCR)
- **通知推送**:Server酱 / pushplus 微信推送
- **完整日志**:智能体执行与决策全程记录

---

## 📦 安装

### 环境要求
- Python 3.8 或更高版本
- pip 包管理器
- (可选)Playwright 浏览器(用于行情截图)

### 安装步骤

1. **克隆仓库**
```bash
git clone https://github.com/IatomicreactorI/CSGOTrading.git
cd CSGOTrading
```

2. **创建虚拟环境**
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

3. **安装依赖**
```bash
pip install -r requirements.txt
```

4. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 填入你的 API Key:
# - DEEPSEEK_API_KEY(DeepSeek 模型)
# - OPENAI_API_KEY(OpenAI 模型)
# - ANTHROPIC_API_KEY(Claude 模型)
# - STEAMDT_TOKEN(SteamDT 行情)
# - PUSH_PROVIDER / PUSH_TOKEN(微信推送,可选)
```

5. **初始化数据库**
```bash
python database/cs2_sqlite_setup.py
```

---

## 🚀 快速开始

### Web 仪表盘(推荐)

一键启动 Web 仪表盘,包含库存管理、今日建议、AI 持仓、决策对照等全部功能:

```bash
python webui.py               # 启动 http://127.0.0.1:5000
python webui.py --debug       # 开发模式(改代码自动热加载)
python webui.py --no-warmup   # 跳过启动时的截图预热(首次进入今日建议会后台补)
python webui.py --port 8080   # 自定义端口
```

> 注意:启动时会先做截图预热(默认带超时),请先配置 `.env` 中的 LLM / SteamDT / 推送等密钥。

### 单日实验

使用默认配置运行单日实验:

```bash
python run.py --config TS-ds.yaml --start-date 2025-09-25 --end-date 2025-09-25
```

### 批量回测

运行多日回测:

```bash
python run.py \
  --config TS-ds.yaml \
  --start-date 2025-09-25 \
  --end-date 2025-10-27
```

### 配置说明

系统支持多种工作流配置:

- **Direct**:直接 LLM 分析(无分析师智能体)
- **T**:仅技术分析
- **TS**:技术 + 情绪分析
- **TSL**:技术 + 情绪 + 流动性
- **TSLE**:全部分析师(技术 + 情绪 + 流动性 + 事件)
- **TSrL**:技术 + 反向情绪 + 流动性

每种配置可搭配不同 LLM 提供商:

- `-ds`:DeepSeek
- `-gm`:Gemini
- `-gt`:GPT
- `-cd`:Claude
- `-km`:Kimi
- `-qw`:Qwen

示例:`TSLE-cd.yaml` 表示使用全部分析师 + Claude。

### 查看结果

```bash
# 查看指定实验的全部信息
python view.py TS-ds

# 查看组合
python view.py TS-ds portfolios

# 查看最新持仓
python view.py TS-ds positions

# 查看每日组合并导出 CSV
python view.py TS-ds daily

# 查看指定日期的组合
python view.py TS-ds daily 2025-09-26

# 导出思考过程 JSON
python view.py TS-ds thinking

# 查看数据摘要
python view.py TS-ds summary

# 列出所有实验
python view.py list
```

### 清理数据

```bash
# 清理实验数据
python clear.py --config-name TS-ds
```

### 每日定时推送

```bash
python daily.py --now       # 立即执行一次分析并推送
python daily.py --daemon     # 常驻调度(每天 20:00 自动分析并推送)
```

---

## 📦 Python 模块归类

项目根目录的 Python 文件按职责分为以下 5 类。

### 1. 服务入口

| 文件 | 职责 |
|------|------|
| `webui.py` | **主入口**:Flask Web 仪表盘(关注列表 / 我的库存 / 今日建议 / AI持仓 / 决策对照 / 持仓与净值 / API 设置),内置后台价格预热、截图预热、AI 账户定时调度 |
| `daily.py` | 每日定时入口:20:00 自动分析并推送到微信(`--now` 立即执行 / `--daemon` 常驻调度) |
| `run.py` | 单日 / 批量实验执行(原框架回测入口) |

### 2. 业务核心模块(被 `webui.py` 调用)

| 文件 | 职责 |
|------|------|
| `inventory.py` | 库存管理:增删改查、盈亏计算、实时价格抓取(支持 `no-market` 秒回) |
| `watchlist.py` | 关注列表管理(`config/watchlist.yaml`) |
| `today_advisor.py` | 今日建议:截图预热(warmup)→ 逐饰品分析 → 今日决策读取 |
| `ai_account.py` | AI 端虚拟账户:自动分析 + 记账、「复制我的库存」合并导入 |
| `ai_trader.py` | AI 端交易盈亏:已实现 / 未实现、7 天交易 CD 可卖份额计算 |
| `comparison.py` | 用户 vs AI 决策对照(后台计算 + 结果缓存) |
| `user_trades.py` | 用户端买卖操作日志(JSON 按月分文件存储) |
| `notify.py` | 报告生成 + 微信推送(Server酱 / pushplus) |

### 3. 实验与结果工具(原框架保留)

| 文件 | 职责 |
|------|------|
| `view.py` | 查看 / 导出实验结果、持仓、净值曲线 |
| `clear.py` | 清理实验数据 |

### 4. 临时调试脚本(下划线前缀,非主流程)

| 文件 | 职责 |
|------|------|
| `_check_hold.py` | 查看 AI 端最新 portfolio 状态 |
| `_del_check.py` | 查看 AI 端卖出记录与 portfolio(删除前确认) |
| `_del_trade.py` | 恢复 / 清理 AI 端数据 |

### 5. 包内模块(目录)

| 目录 | 职责 |
|------|------|
| `agents/` | 智能体:planner、portfolio_manager(含补仓 / 止损 / 7天CD 规则)、analysts(technical / vision 等) |
| `apis/` | 数据源:cs2market(SteamDT 实时价 + K线 + 截图 + 搜索)、steam、reddit、ocr(图像识别) |
| `graph/` | LangGraph 工作流:workflow(含 seed_from_inventory 覆盖语义)、schema、constants |
| `database/` | SQLite 数据层(interface / cs2_sqlite_helper / cs2_sqlite_setup) |
| `llm/` | LLM 集成:inference、provider、prompt(补仓 / 减仓规则段) |
| `util/` | 工具:logger(UTF-8 兼容)、screenshot_cache、cs2_db_helper、config |
| `templates/` `static/` | Web 前端(Jinja2 模板 + CSS) |

---

## 📊 配置说明

### 工作流配置(`config/`)

```yaml
exp_name: "TS-ds"  # 实验名称
cashflow: 10000    # 初始资金
tickers:           # 交易标的
  - "AK-47 | Redline (Field-Tested)"
  - "AWP | Asiimov (Field-Tested)"

llm:               # LLM 配置
  provider: "deepseek"
  model: "deepseek-chat"

planner_mode: true        # 启用元规划器
workflow_analysts:        # 可用分析师
  - technical
  - sentiment

enable_transaction_fee: true  # 包含交易成本
```

### 数据库配置

**本地 SQLite**(默认,无需额外配置):
```bash
python run.py --config TS-ds.yaml
```

数据存储于 `assets/cs2.db`,包含组合、决策、信号、库存、AI 交易等 9 张表。

---

## 🗂️ 项目结构

```
CSGOTrading/
├── agents/                   # 智能体实现
│   ├── planner.py           # 元规划器(动态选择分析师)
│   ├── portfolio_manager.py # 组合管理:买卖决策 + 补仓/止损/7天CD 规则
│   ├── registry.py          # 智能体注册中心
│   └── analysts/            # 分析师
│       ├── technical.py     # 技术分析
│       ├── vision.py        # 视觉 K 线图分析
│       ├── sentiment.py / sentiment_reverse.py
│       ├── liquidity.py
│       └── event.py
├── apis/                     # 数据源集成
│   ├── cs2market/           # CS2 市场:SteamDT 实时价(web_scraper,TTL 缓存)
│   │   │                     #   api.py / chart_screenshot.py / item_search.py
│   ├── steam/  reddit/      # Steam 新闻 / Reddit 情绪
│   ├── ocr.py               # 图像识别(饰品图片 → 名称)
│   ├── router.py            # API 路由
│   └── common_model.py      # 公共数据模型
├── database/                 # SQLite 数据层
│   ├── interface.py         # 抽象接口
│   ├── cs2_sqlite_helper.py
│   └── cs2_sqlite_setup.py  # 建表 + 索引
├── graph/                    # LangGraph 工作流
│   ├── workflow.py          # 主工作流(seed_from_inventory 覆盖语义)
│   ├── schema.py            # 状态定义(Position.lots / available_shares)
│   └── constants.py
├── llm/                      # LLM 集成
│   ├── inference.py         # LLM 调用
│   ├── provider.py          # Provider 配置
│   └── prompt.py            # Prompt 模板(补仓/减仓规则段)
├── util/                     # 工具
│   ├── logger.py            # UTF-8 兼容日志
│   ├── screenshot_cache.py  # 截图缓存(5h TTL)
│   ├── cs2_db_helper.py
│   └── config.py
├── templates/                # Web 页面(Jinja2)
│   ├── base.html / inventory.html / today_advisor.html
│   ├── ai_trader.html / comparison.html / positions.html
│   └── watchlist.html / settings.html
├── static/                   # 静态资源(css)
├── config/                   # 实验配置 + watchlist.yaml / live.yaml / ai_account.yaml
├── assets/                   # SQLite 数据库 + 截图
├── webui.py                  # ★ Web 服务主入口
├── inventory.py / watchlist.py / today_advisor.py
├── ai_account.py / ai_trader.py / comparison.py
├── notify.py / user_trades.py / daily.py
├── run.py                    # 实验执行
├── view.py                   # 结果查看
├── clear.py                  # 数据清理
├── _check_hold.py / _del_check.py / _del_trade.py  # 临时调试脚本
├── .env.example              # 环境变量模板
├── LICENSE
└── README.md
```

---

## 🔬 高级用法

### 添加自定义分析师

1. 在 `agents/analysts/` 中创建分析师实现:

```python
from graph.constants import AgentKey
from llm.inference import agent_call

def custom_analyst(ticker: str, llm_config, analyst_signal):
    # 你的分析逻辑
    return {
        "action": "BUY",
        "confidence": 0.8,
        "justification": "分析推理过程"
    }
```

2. 在 `agents/registry.py` 中注册:

```python
AgentRegistry.register(
    AgentKey.CUSTOM,
    custom_analyst,
    "自定义分析师描述"
)
```

3. 添加到工作流配置:

```yaml
workflow_analysts:
  - technical
  - sentiment
  - custom  # 你的新分析师
```

### 添加自定义 LLM 提供商

在 `llm/provider.py` 中配置:

```python
@dataclass
class ProviderConfig:
    name: str
    model_class: Any
    requires_api_key: bool = True
    env_key: str = "CUSTOM_API_KEY"
    base_url: str = None

# 注册提供商
Provider.add_provider("custom", ProviderConfig(...))
```

---

## 🤝 贡献指南

欢迎社区贡献!请遵循以下规范:

1. **Fork 仓库** 并创建功能分支
2. **遵循 PEP 8** 编码规范
3. 为新功能**添加测试**
4. **更新文档**说明 API 变更
5. **提交 Pull Request** 并附带清晰描述

### 开发环境

```bash
# 安装开发依赖
pip install -r requirements-dev.txt

# 运行测试
pytest tests/

# 运行代码检查
flake8 .
black .
```

---

## 📄 许可证

本项目基于 MIT 许可证发布,详见 [LICENSE](LICENSE) 文件。

---

## 🙏 致谢

- **LangChain & LangGraph**:智能体编排的基础框架
- **OpenAI、Anthropic、DeepSeek 等**:LLM 提供商
- **SteamDT**:CS2 市场数据与行情
- **CS2 社区**:市场数据与洞察
- **所有贡献者**:社区贡献者

---

## 🗺️ 路线图

### 计划中的功能

- [ ] **强化学习集成**:训练智能体实现自适应策略
- [ ] **实时交易**:实时市场对接与自动执行
- [ ] **高级风险模型**:VaR、CVaR 与凯利公式
- [ ] **多资产相关性**:跨资产分析与对冲
- [ ] **分布式执行**:多进程与云端部署
- [ ] **更多数据源**:接入更多市场 API

### 版本历史

- **v0.1.0**(2026-01-05):核心多智能体框架发布
- **v0.0.1**(2025-12):内测版本

---

<div align="center">

**⭐ 如果这个项目对你有帮助,请点个 Star! ⭐**

CSGOTrading Team 出品

</div>
