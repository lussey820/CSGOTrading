from typing import  Dict, Any
from langgraph.graph import StateGraph, START, END
from graph.schema import FundState, Portfolio, Decision, Action, Position
from graph.constants import AgentKey
from agents.registry import AgentRegistry
from agents.planner import planner_agent
from util.cs2_db_helper import get_cs2_db
from util.logger import logger
from time import perf_counter

# Trading friction assumptions (applied to sell only)
TRANSACTION_FEE_RATE = 0.02  # 2% sell fee


class AgentWorkflow:
    """Trading Decision Workflow."""

    def __init__(self, config: Dict[str, Any], config_id: str):
        self.llm_config = config['llm']
        self.tickers = config['tickers']
        self.exp_name = config['exp_name']
        self.trading_date = config['trading_date']
        self.db = get_cs2_db()

        # load latest portfolio from DB
        portfolio = self.db.get_latest_portfolio(config_id)
        if not portfolio:
            portfolio = self.db.create_portfolio(config_id, config['cashflow'], config['trading_date'])
            if not portfolio:
                raise RuntimeError(f"Failed to create portfolio for config {self.exp_name}")

        # copy portfolio with a new id
        new_portfolio = self.db.copy_portfolio(config_id, portfolio, config['trading_date'])
        self.init_portfolio = Portfolio(**new_portfolio)
        logger.info(f"New portfolio ID: {self.init_portfolio.id}")

        # 今日建议模式:用真实库存持仓覆盖模拟组合,让决策引擎看到真实持仓(数量+成本)
        if config.get("seed_from_inventory", False):
            from inventory import list_items
            # 覆盖语义:每次运行都从真实库存重建持仓,并把现金重置为配置初始值,
            # 避免同一个持久化 portfolio 被反复追加,导致持仓数量膨胀、现金被反复扣减
            self.init_portfolio.positions = {}
            self.init_portfolio.cashflow = round(float(config.get("cashflow") or 0.0), 2)
            for it in list_items(with_market=False):
                name = it["item_name"]
                shares = int(it.get("shares") or 0)
                if shares <= 0:
                    continue
                buy_price = float(it.get("buy_price") or 0)
                buy_date = str(it.get("buy_date") or "")[:10] or str(self.trading_date)[:10]
                pos = self.init_portfolio.positions.setdefault(name, Position())
                # 记录买入批次(7 天 CD 依据),同一饰品多行买入时聚合
                pos.lots.append({"date": buy_date, "shares": shares})
                pos.shares += shares
                pos.value = round(pos.value + buy_price * shares, 2)
                if pos.shares > 0:
                    pos.avg_cost = round(
                        (pos.avg_cost * (pos.shares - shares) + buy_price * shares) / pos.shares, 2
                    )
            logger.info(
                "Seeded portfolio positions from real inventory: "
                + ", ".join(f"{k}={v.shares}@{v.avg_cost}" for k, v in self.init_portfolio.positions.items())
            )
        
        # Initialize workflow configuration
        self.planner_mode = config.get('planner_mode', False)
        
        # Get workflow analysts (optional - can be empty list for direct LLM analysis)
        self.workflow_analysts = config.get('workflow_analysts', [])
        
        # Transaction fee setting (default: True for backward compatibility)
        self.enable_transaction_fee = config.get('enable_transaction_fee', True)

        # AI 端:卖出决策后自动结算已实现盈亏
        self.auto_settle = config.get('auto_settle', False)
        
        # Validate analysts and remove invalid ones
        if self.workflow_analysts:
            invalid_analysts = [a for a in self.workflow_analysts if not AgentRegistry.check_agent_key(a)]
            if invalid_analysts:
                logger.warning(f"Invalid analyst keys removed: {invalid_analysts}")
                self.workflow_analysts = [a for a in self.workflow_analysts if a not in invalid_analysts]
            
        if not self.workflow_analysts:
            logger.info("No analysts configured - using direct LLM analysis mode")


    def build(self) -> StateGraph:
        """Build the workflow"""
        graph = StateGraph(FundState)
        
        # create node for portfolio manager
        portfolio_agent = AgentRegistry.get_agent_func_by_key(AgentKey.PORTFOLIO)
        graph.add_node(AgentKey.PORTFOLIO, portfolio_agent)
        
        # create node for each analyst and add edge
        if self.current_analysts:
            for analyst in self.current_analysts:
                agent_func = AgentRegistry.get_agent_func_by_key(analyst)
                graph.add_node(analyst, agent_func)
                graph.add_edge(START, analyst)
                graph.add_edge(analyst, AgentKey.PORTFOLIO)
        else:
            # Direct LLM mode: no analysts, go straight to portfolio manager
            graph.add_edge(START, AgentKey.PORTFOLIO)
        
        # Route portfolio manager to end
        graph.add_edge(AgentKey.PORTFOLIO, END)
        workflow = graph.compile()

        return workflow 
        

    def load_analysts(self, ticker: str):
        """
        Load the analysts for processing:
        - If planner_mode is True: use planner to select from verified workflow_analysts
        - If planner_mode is False: use all verified workflow_analysts
        - If no workflow_analysts: use direct LLM mode (no analysts)
        """
        if not self.workflow_analysts:
            logger.info(f"Direct LLM mode for {ticker} - no analysts, using LLM prompt directly")
            self.current_analysts = []
        elif self.planner_mode:
            logger.info("Using planner agent to select analysts from verified list")
            self.current_analysts = planner_agent(ticker, self.llm_config, self.workflow_analysts)
            if not self.current_analysts:
                raise ValueError("No analysts selected by planner")
        else:
            logger.info("Using all verified analysts")
            self.current_analysts = self.workflow_analysts.copy()
            
        logger.info(f"Active analysts for {ticker}: {self.current_analysts}")
    
    def run(self, config_id: str) -> float:
        """Run the workflow."""
        start_time = perf_counter()

        # will be updated by the output of workflow
        portfolio = self.init_portfolio 
        for ticker in self.tickers:
            self.load_analysts(ticker)
            
            # init FundState
            state = FundState(
                ticker = ticker,
                exp_name = self.exp_name,
                trading_date = self.trading_date,
                llm_config = self.llm_config,
                portfolio = portfolio,
                num_tickers = len(self.tickers),
                enable_transaction_fee = self.enable_transaction_fee
            )

            # build the workflow
            workflow = self.build()
            logger.info(f"{ticker} workflow compiled successfully")
            try:
                final_state = workflow.invoke(state)
            except Exception as e:
                logger.error(f"Error running workflow: {e}")
                raise RuntimeError(f"Failed to generate new portfolio {portfolio.id}")

            # update portfolio
            portfolio = self.update_portfolio_ticker(portfolio, ticker, final_state["decision"], self.enable_transaction_fee)
            logger.log_portfolio(f"{ticker} position update", portfolio)

            if self.planner_mode:
                self.current_analysts = None # clean and reset current_analysts

        logger.log_portfolio("Final Portfolio", portfolio)
        logger.info("Updating portfolio to Database")
        portfolio_dict = portfolio.model_dump()
        success = self.db.update_portfolio(config_id, portfolio_dict, self.trading_date)
        if not success:
            logger.error("Failed to update portfolio to database")
            raise RuntimeError("Failed to update portfolio to database")

        end_time = perf_counter()
        time_cost = end_time - start_time

        return time_cost


    def update_portfolio_ticker(self, portfolio: Portfolio, ticker: str, decision: Decision, enable_transaction_fee: bool = True) -> Portfolio:
        """Update the ticker asset in the portfolio."""

        action = decision.action
        shares = decision.shares
        price = decision.price

        if ticker not in portfolio.positions:
            portfolio.positions[ticker] = Position(shares=0, value=0)

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

            # 记录买入批次:7 天交易 CD 依据,新买入份额进入锁定
            if actual_shares > 0:
                portfolio.positions[ticker].lots.append({
                    "date": str(self.trading_date)[:10],
                    "shares": actual_shares,
                })

            # log limited buy order
            if actual_shares < shares:
                logger.warning(
                    f"Limited buy order for {ticker}: requested {shares}, actual {actual_shares} "
                    f"(cash: {portfolio.cashflow:.2f})"
                )
        elif action == Action.SELL:
            # safety limit: ensure no negative position, and respect 7-day CD
            pos = portfolio.positions[ticker]
            available = pos.available_shares(as_of=str(self.trading_date)[:10])
            max_sellable_shares = min(pos.shares, available)
            actual_shares = min(shares, max_sellable_shares)

            pos.shares -= actual_shares
            # FIFO 扣减买入批次(优先卖最早买入、已解锁的份额)
            to_sell = actual_shares
            for lot in sorted(pos.lots, key=lambda l: str(l.get("date") or "")):
                if to_sell <= 0:
                    break
                lot_shares = int(lot.get("shares") or 0)
                if lot_shares <= 0:
                    continue
                take = min(lot_shares, to_sell)
                lot["shares"] = lot_shares - take
                to_sell -= take
            pos.lots = [lot for lot in pos.lots if int(lot.get("shares") or 0) > 0]

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

        # Always recalculate position value with latest price
        portfolio.positions[ticker].value = round(price * portfolio.positions[ticker].shares, 2)

        # round cashflow
        portfolio.cashflow = round(portfolio.cashflow, 2)

        return portfolio
