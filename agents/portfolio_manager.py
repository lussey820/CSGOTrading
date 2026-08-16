from graph.constants import AgentKey, Action
from llm.prompt import PORTFOLIO_PROMPT, PORTFOLIO_PROMPT_NO_FEE, RISK_CONTROL_PROMPT, RISK_CONTROL_PROMPT_DIRECT_LLM
from graph.schema import Decision, FundState, PositionRisk
from llm.inference import agent_call
from apis.router import Router, APISource
from util.cs2_db_helper import get_cs2_db
from util.logger import logger

# Portfolio Manager Thresholds
thresholds = {
    "decision_memory_limit": 5,
    # 补仓(逢低摊成本)参数
    "avg_down_min_pnl": -5.0,       # 浮亏达到该值(≤)才允许补仓
    "avg_down_deep_pnl": -30.0,     # 深亏线:低于该值(≤)视为深亏,补仓额度再减半
    "avg_down_batch": 1 / 3,        # 正常:单次补仓 = 目标仓位缺口 × 1/3(分批留后手)
    "avg_down_deep_batch": 1 / 6,   # 深亏:单次补仓 = 目标仓位缺口 × 1/6
}

# Trading friction assumptions, 2% per trade
TRANSACTION_FEE_RATE = 0.02 

def portfolio_agent(state: FundState):
    """Make final trading decisions and generate orders for a given weapon"""
    agent_name = AgentKey.PORTFOLIO
    portfolio = state["portfolio"]
    ticker = state["ticker"]
    exp_name = state["exp_name"]
    trading_date = state["trading_date"]
    analyst_signals = state["analyst_signals"]
    llm_config = state["llm_config"]
    num_tickers = state["num_tickers"]
    enable_transaction_fee = state.get("enable_transaction_fee", True)

    # Get db instance based on experiment name
    db = get_cs2_db()
    
    # Get price data
    router = Router(APISource.CS2_MARKET)
    try:
        current_price = router.get_cs2_stock_last_close_price(ticker=ticker, trading_date=trading_date)
    except Exception as e:
        logger.error(f"Failed to fetch price data for {ticker}: {e}")
        raise RuntimeError(f"Failed to make decision")
    
    # calculate the max position ratio
    max_position_ratio = 1
    if num_tickers > 1:
        # suppose a single ticker can occupy its own base allocation (1/N) plus that of one other ticker maximally, round to the nearest 0.05
        max_position_ratio = round(2 / num_tickers * 20) / 20
    

    # risk control
    # Use different prompts based on whether we have analyst signals
    if analyst_signals:
        # Traditional mode: use analyst signals
        risk_prompt = RISK_CONTROL_PROMPT.format(
            ticker_signals=analyst_signals,
            portfolio=portfolio.model_dump_json(),
            max_position_ratio=max_position_ratio,
        )
    else:
        # Direct LLM mode: analyze ticker directly without analyst signals
        risk_prompt = RISK_CONTROL_PROMPT_DIRECT_LLM.format(
            ticker=ticker,
            portfolio=portfolio.model_dump_json(),
            max_position_ratio=max_position_ratio,
        )

    position_risk = agent_call(
        prompt=risk_prompt,
        llm_config=llm_config,
        pydantic_model=PositionRisk,
    )
    
    logger.log_agent_status(agent_name, ticker, "Risk control")
    logger.log_risk(ticker, position_risk)

    # verify the position ratio if it is in the range
    if position_risk.optimal_position_ratio > max_position_ratio:
        # too bullish, set to the max
        position_risk.optimal_position_ratio = max_position_ratio
    elif position_risk.optimal_position_ratio < 0:
        # too bearish, set to 0
        position_risk.optimal_position_ratio = 0

    logger.log_agent_status(agent_name, ticker, "Making trading decisions")

    # Get decision memory
    decision_memory = db.get_decision_memory(exp_name, ticker, thresholds["decision_memory_limit"])
    current_shares, tradable_shares = calculate_ticker_shares(portfolio, current_price, ticker, position_risk.optimal_position_ratio)

    # 持仓成本(加权平均),无持仓时为 0
    pos = portfolio.positions.get(ticker)
    avg_cost = pos.avg_cost if pos else 0.0
    # 浮盈/浮亏百分比(正=盈利,负=亏损),无成本时为 0
    floating_pnl_pct = (
        round((current_price - avg_cost) / avg_cost * 100.0, 2)
        if avg_cost and avg_cost > 0 else 0.0
    )

    # 从分析师信号汇总最近支撑/阻力:支撑取最高(最贴近现价),阻力取最低
    signals = analyst_signals or []
    supports = [s.support for s in signals if s is not None and s.support is not None and s.support > 0]
    resistances = [s.resistance for s in signals if s is not None and s.resistance is not None and s.resistance > 0]
    nearest_support = max(supports) if supports else None
    nearest_resistance = min(resistances) if resistances else None
    # 破位判断:现价跌破最近支撑位
    broke_support = bool(nearest_support is not None and current_price < nearest_support)
    # 破位幅度:现价相对支撑位的偏离百分比(负=已跌破)。用于区分真破位 vs 假破位
    break_pct = (
        round((current_price - nearest_support) / nearest_support * 100.0, 2)
        if nearest_support and nearest_support > 0 else 0.0
    )
    # 看空信号一致性:看空信号严格多于看多才算共识(中性=弃权/分析失败,不参与)
    bearish_count = sum(1 for s in signals if s is not None and s.signal.value == "Bearish")
    bullish_count = sum(1 for s in signals if s is not None and s.signal.value == "Bullish")
    bearish_consensus = bearish_count > bullish_count

    # 7 天交易 CD:实际可卖份额(买入满 7 天解锁的部分),旧数据无批次视为全部可卖
    available_shares = pos.available_shares(as_of=str(trading_date)[:10]) if pos else 0

    # 补仓资格(逢低摊成本):结构未破 + 信号未空 + 浮亏达标 + 组合有空间 + 已持仓
    avg_down_eligible = False
    avg_down_max_shares = 0
    if (
        pos is not None and pos.shares > 0 and avg_cost > 0
        and floating_pnl_pct <= thresholds["avg_down_min_pnl"]
        and not broke_support
        and not bearish_consensus
        and tradable_shares > 0
    ):
        batch = (
            thresholds["avg_down_deep_batch"]
            if floating_pnl_pct <= thresholds["avg_down_deep_pnl"]
            else thresholds["avg_down_batch"]
        )
        # 上限 = 目标缺口 × 批次比例(分批留后手);并受底仓守恒约束:补仓 ≤ 已解锁旧仓(可做 T/止损)
        avg_down_max_shares = max(1, min(int(tradable_shares * batch), pos.shares))
        avg_down_eligible = True

    # make trading decision
    if enable_transaction_fee:
        prompt = PORTFOLIO_PROMPT.format(
            decision_memory=decision_memory,
            current_price=current_price,
            current_shares=current_shares,
            avg_cost=avg_cost,
            floating_pnl_pct=floating_pnl_pct,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            broke_support=broke_support,
            break_pct=break_pct,
            bearish_consensus=bearish_consensus,
            tradable_shares=tradable_shares,
            available_shares=available_shares,
            avg_down_eligible=avg_down_eligible,
            avg_down_max_shares=avg_down_max_shares,
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
            floating_pnl_pct=floating_pnl_pct,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            broke_support=broke_support,
            break_pct=break_pct,
            bearish_consensus=bearish_consensus,
            tradable_shares=tradable_shares,
            available_shares=available_shares,
            avg_down_eligible=avg_down_eligible,
            avg_down_max_shares=avg_down_max_shares,
        )

    # Generate the trading decision
    ticker_decision = agent_call(
        prompt=prompt,
        llm_config=llm_config,
        pydantic_model=Decision
    )

    # post-process the decision due to possible reasoning error
    ticker_decision.price = current_price
    if ticker_decision.shares < 0 and ticker_decision.action == Action.SELL:
        ticker_decision.shares = -ticker_decision.shares
        
    # save decision
    logger.log_decision(ticker, ticker_decision)
    db.save_decision(portfolio.id, ticker, prompt, ticker_decision, trading_date)

    return {"decision": ticker_decision}


def calculate_ticker_shares(portfolio, current_price, ticker, optimal_position_ratio):
    """calculate the tradable shares for a given ticker based on portfolio"""

    # Get current position value (0 if no position exists)
    current_shares = 0 
    if ticker in portfolio.positions:
        current_shares = portfolio.positions[ticker].shares
    # current value for the ticker
    current_value = current_shares * current_price
    # total portfolio value
    total_portfolio_value = portfolio.cashflow + sum(portfolio.positions[t].value for t in portfolio.positions)
    # position limit for the ticker
    position_limit = total_portfolio_value * optimal_position_ratio
    # position value gap
    position_value_gap = position_limit - current_value

    if position_value_gap > 0: # still have room to buy, maximum tradable cash is the minor between position_value_gap and cashflow
        tradable_shares = min(position_value_gap, portfolio.cashflow) // current_price
    else: # need to sell, maximun selling shares is the minor between position gap and current shares
        tradable_shares = max(position_value_gap // current_price, -current_shares)
    
    return current_shares, tradable_shares
        

    
