import operator
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from typing_extensions import TypedDict, Annotated
from pydantic import BaseModel, Field
from graph.constants import Signal, Action

# Steam 市场 7 天交易 CD:买入后 7 天内不可再次上架出售
TRADING_CD_DAYS = 7


class AnalystSignal(BaseModel):
    """Signal from analyst"""
    signal: Signal = Field(
        description=f"Choose from {Signal.BULLISH}, {Signal.BEARISH}, or {Signal.NEUTRAL}",
        default=Signal.NEUTRAL
    )
    justification: str = Field(
        description="Brief explanation for the signal",
        default="No justification provided due to error"
    )
    support: Optional[float] = Field(
        default=None,
        description="最近支撑位价格(数值),无法判断时为 null"
    )
    resistance: Optional[float] = Field(
        default=None,
        description="最近阻力位价格(数值),无法判断时为 null"
    )

class Decision(BaseModel):
    """Decision made by portfolio manager"""
    action: Action = Field( 
        description=f"Choose from {Action.BUY}, {Action.SELL}, or {Action.HOLD}",
        default=Action.HOLD
    )
    shares: int = Field(
        description="Number of shares to buy or sell, set 0 for hold",
        default=0
    )
    price: float = Field(
        description="Current price for the ticker",
        default=0
    )
    justification: str = Field(
        description="Brief explanation for the decision",
        default="Just hold due to error"
    )

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
    lots: List[Dict] = Field(
        default_factory=list,
        description="买入批次: [{'date': 'YYYY-MM-DD', 'shares': n}],"
                    "用于计算 7 天交易 CD 后的可卖份额。"
    )

    def available_shares(self, as_of: str) -> int:
        """7 天 CD 后可卖份额:买入日期距 as_of 满 7 天(含)的批次合计。

        无批次记录(旧数据)视为全部可卖;锁定部分不可卖。
        """
        if not self.lots:
            return self.shares
        try:
            cutoff = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date() - timedelta(days=TRADING_CD_DAYS)
            cutoff_str = cutoff.isoformat()
        except (ValueError, TypeError):
            return self.shares
        available = sum(
            int(lot.get("shares") or 0)
            for lot in self.lots
            if str(lot.get("date") or "")[:10] <= cutoff_str
        )
        return min(available, self.shares)

class PositionRisk(BaseModel):
    """Risk assessment for a single ticker"""
    optimal_position_ratio: float = Field(
        description="The optimal ratio of the position value to the total portfolio value",
        default=0.0
    )
    justification: str = Field(
        description="Detailed risk assessment rationale explaining the recommendations",
        default="No assessment provided due to insufficient data"
    )

class Portfolio(BaseModel):
    """Portfolio state when running the workflow."""
    id: str = Field(description="Portfolio id.")
    cashflow: float = Field(description="Cashflow for the fund.")
    positions: dict[str, Position] = Field(description="Positions for each ticker.")

class FundState(TypedDict):
    """Fund state when running the workflow."""

    # from environment
    exp_name: str
    trading_date: datetime
    ticker: str
    llm_config: Dict[str, Any]
    portfolio: Portfolio
    num_tickers: int
    enable_transaction_fee: bool

    # updated by workflow
    # ticker -> signal of all analysts
    analyst_signals: Annotated[List[AnalystSignal], operator.add]
    # portfolio manager output
    decision: Decision
    