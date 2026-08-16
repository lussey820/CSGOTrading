# CSGOTrading — CS2 饰品交易助手

一个个人使用的 CS2 饰品交易辅助工具:用多智能体 LLM 分析行情(K 线 + 实时价格),生成买卖/观望建议,跟踪你的库存盈亏,并模拟一个「AI 虚拟账户」自动交易供你对照参考。

> 本系统基于多智能体 LLM 框架改造而来,原框架的实验/回测功能已基本弃用,当前以 Web 仪表盘为核心。

---

## ✨ 功能总览

| 页面 | 功能 |
|------|------|
| **关注列表** | 管理观察池,支持饰品图片一键识别入库(OCR) |
| **我的库存** | 库存增删改、买入时间、盈亏/收益率实时计算 |
| **今日建议** | 对库存饰品逐一行情分析,给出 买入/卖出/观望 + 理由 |
| **AI 持仓** | AI 虚拟账户:自动分析并模拟买卖,展示已实现/未实现盈亏 |
| **决策对照** | 用户端(今日建议)与 AI 端决策并排对比 |
| **持仓与净值** | 总资产/现金/净值变化 |
| **API 设置** | 查看当前使用的模型与 Key 配置 |

**核心能力**
- **实时行情**:SteamDT 实时价(带 300s 缓存 + 页面后台预热,首次加载秒开、价格渐进显示)
- **多智能体分析**:技术分析 + K 线视觉分析 → 风控 → 组合经理生成决策
- **风控规则**:补仓(逢低摊成本)、深亏止损、防御性减仓、Steam 7 天交易 CD
- **微信推送**:AI 账户分析完成推完整决策报告(Server酱 / pushplus)
- **AI 虚拟账户**:自动分析、模拟买卖并记账,供决策对照(可设 `AI_ACCOUNT_AUTO_INTERVAL`(秒)启用自动轮次,默认需手动触发)

---

## 📖 系统怎么运作

### 核心工作流程

对每个饰品,决策管线按以下顺序执行(基于 LangGraph 编排):

```
行情准备(SteamDT 实时价 + K 线截图)
      ↓
技术分析师  →  技术信号(Bullish/Bearish/Neutral + 支撑/阻力)
视觉分析师  →  视觉信号(解读 K 线截图)
      ↓
风控(计算目标仓位比例,受最大仓位约束)
      ↓
组合经理(综合信号 + 持仓 + 风控规则 → Buy / Sell / Hold + 数量 + 理由)
      ↓
落库(决策、组合快照)+ 微信推送
```

### 双账户机制

系统有两个独立账户,共用同一套决策管线:

| | 用户端(今日建议) | AI 端(AI 持仓) |
|---|---|---|
| 分析池 | 你的真实库存 | 关注列表(watchlist) |
| 持仓来源 | 真实库存(数量/成本/买入时间) | 模拟账户,自动买卖记账 |
| 结算 | 你手工执行建议 | 自动按市场价结算,扣 2% 手续费 |
| 用途 | 获得每天的操作建议 | 对照 AI 的模拟交易表现 |

「复制我的库存」可以把你的库存一键导入 AI 账户,让两端从相同起点对照。

### 决策依据(喂给 LLM 的输入)

- 实时价格、昨日涨跌
- K 线截图(视觉分析)
- 当前持仓(数量、加权成本、7 天 CD 可卖份额)
- 买入批次(FIFO,用于 CD 与补仓额度计算)
- 历史决策记忆、分析师信号(支撑/阻力、破位)

### 数据存储

本地 SQLite(`assets/cs2.db`),核心表:`cs2_config`(账户配置)、`cs2_portfolio`(组合快照/现金)、`cs2_decision`(决策)、`cs2_signal`(分析师信号)、`cs2_inventory`(真实库存)、`cs2_ai_trade`(AI 交易记录)、`cs2_screenshot_cache`(K 线截图缓存,5h TTL)。

---

## 🚀 快速开始(部署)

### 环境要求
- Python 3.8+(建议 3.10+)
- Playwright 浏览器(用于 K 线截图)

### 1. 安装依赖

```bash
pip install -r requirements.txt
# K 线截图需要浏览器(若安装失败,截图功能不可用,其余功能不受影响)
python -m playwright install chromium
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

需要配置的 Key:

| 变量 | 用途 | 获取方式 |
|------|------|----------|
| `DEEPSEEK_API_KEY` | 默认分析模型(deepseek-chat) | https://platform.deepseek.com/api_keys |
| `STEAMDT_API_KEY` | 实时价格 / K线 / 饰品搜索 | SteamDT 开放平台申请 |
| `PUSH_PROVIDER` | 推送渠道:`serverchan` 或 `pushplus` | 选一个即可 |
| `PUSH_TOKEN` | Server酱 sendkey 或 pushplus token | 对应平台获取 |
| `QWEN_API_KEY` | 视觉分析模型(qwen-vl-plus) | 阿里云百炼 |

> 视觉分析(K 线截图解读)依赖 `QWEN_API_KEY`;没有它分析会退化为纯技术分析。
> 其它 provider(OpenAI/Kimi/AIHubMix/YiZhan)可选用,需在 `config/live.yaml` 中切换。

### 3. 配置关注列表

编辑 `config/watchlist.yaml`,填入你要观察的饰品(英文市场名):

```yaml
tickers:
- FAMAS | ZX Spectron (Factory New)
- Desert Eagle | Night Heist (Factory New)
```

### 4. 初始化数据库

```bash
python database/cs2_sqlite_setup.py
```

### 5. 启动 Web 仪表盘

```bash
python webui.py                # http://127.0.0.1:5000
python webui.py --debug        # 开发模式(改代码自动热加载)
python webui.py --no-warmup    # 跳过启动时的 K 线截图预热
python webui.py --port 8080    # 自定义端口
```

启动后浏览器打开 **http://127.0.0.1:5000** 即可使用。

> 首次启动会先预先生成关注饰品的 K 线截图(已有缓存则秒启)。截图也用于今日建议的视觉分析。

---

## 📄 页面与使用说明

### 关注列表
- 添加/删除观察饰品
- **图像导入**:上传饰品图片 → 自动识别名称 → 批量选择入库(节省手动输入)

### 我的库存
- 记录每个饰品的**数量、买入价、买入时间**(买入时间用于 7 天 CD 计算)
- 自动计算当前价、收益率、昨日涨跌、累计盈亏
- 支持售出记录(自动扣 2% 手续费)

### 今日建议
- 点击「分析今日」对库存逐饰品分析,产出 买入/卖出/观望 + 理由
- 分析流程:准备 K 线截图 → 技术分析 → 视觉分析 → 风控 → 组合经理决策
- 同一饰品当天多次分析会覆盖为最新决策

### AI 持仓
- **AI 虚拟账户**:关注列表即其投资池,自动分析后模拟买卖
- 展示已实现盈亏(2% 手续费)、未实现盈亏、7 天 CD 可卖份额
- 「复制我的库存」:把你的库存一键导入 AI 账户(同名覆盖,现金按成本扣减)
- 「运行 AI 账户」:手动触发一轮分析(可用环境变量 `AI_ACCOUNT_AUTO_INTERVAL` 设置自动轮次间隔秒数,默认不自动)

### 决策对照
- 用户端建议 vs AI 端决策并排展示,便于判断 AI 是否比你自己更准

---

## 📊 决策与风控规则

以下规则是系统决策的核心逻辑(集中在 `agents/portfolio_manager.py`):

| 规则 | 内容 |
|------|------|
| **建仓/加仓** | 组合经理按目标仓位比例计算可买数量 |
| **补仓(逢低摊成本)** | 浮亏 ≥5% 且未破支撑、无看空共识时,按缺口 1/3 分批补仓;深亏(≤-30%)时额度减半(1/6) |
| **止损** | 深亏 + 真破位(跌破支撑且看空共识)强制止损 |
| **防御性减仓** | 深亏 + 弱破位 + 持仓≥3 股时,至少减 `ceil(持仓/3)`,≤2 股豁免 |
| **7 天交易 CD** | 买入满 7 天部分才可卖(按买入批次 FIFO),可卖份额会限制卖出量 |
| **手续费** | 卖出按 2% 计 |

> 看空共识 = 看空信号**严格多于**看多(中性/分析失败视为弃权)。

---

## 📦 目录结构(核心)

```
CSGOTrading/
├── webui.py                  # Web 服务主入口(所有页面与 API)
├── today_advisor.py          # 今日建议:截图预热 + 分析 + 结果读取
├── ai_account.py             # AI 虚拟账户:自动分析、记账、推送
├── ai_trader.py              # AI 交易盈亏统计(7 天 CD 可卖计算)
├── inventory.py              # 库存管理 + 价格抓取(no-market 秒回)
├── watchlist.py              # 关注列表
├── comparison.py             # 用户 vs AI 决策对照
├── notify.py                 # 报告生成 + 微信推送
├── daily.py                  # 每日 20:00 定时分析并推送(--daemon)
├── agents/                   # 智能体:组合经理(风控规则)、分析师
│   └── analysts/             #   technical / vision / ...
├── apis/cs2market/           # SteamDT 价格、K线、截图、搜索
├── graph/                    # LangGraph 决策工作流
├── llm/                      # LLM 调用与 Prompt 模板
├── templates/                # Web 页面(Jinja2)
├── config/                   # live.yaml / watchlist.yaml / ai_account.yaml
└── assets/                   # SQLite 数据库 + K线截图
```

---

## 💬 微信推送

两种推送渠道,任选其一:

- **Server酱**:`PUSH_PROVIDER=serverchan`,`PUSH_TOKEN` 填 sendkey
- **pushplus**:`PUSH_PROVIDER=pushplus`,`PUSH_TOKEN` 填 token

推送时机:
- **AI 账户**每轮分析完成(含完整决策报告:总资产/现金 + 买卖/观望 + 理由)
- **AI 账户**分析失败/崩溃(必推)
- **每日 20:00**(需运行 `python daily.py --daemon`)

---

## 🔧 常见问题

**Q: 价格不显示/显示「-」?**
A: 检查 `STEAMDT_API_KEY` 是否配置;或点击页面的价格预热(首次加载后台抓价,完成后自动带价刷新)。

**Q: 今日建议很慢?**
A: 每个饰品需多次 LLM 调用 + 截图,耗时正常。建议关注列表不要放太多饰品。

**Q: 视觉分析没效果?**
A: 确认 `QWEN_API_KEY` 已配置,K 线截图已生成(`--no-warmup` 启动后首次分析会自动补截图)。

**Q: 改了代码没生效?**
A: 用 `python webui.py --debug` 启动(自动热加载);默认模式需重启。

---

## 📄 License

MIT License,详见 [LICENSE](LICENSE)。
