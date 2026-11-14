"""Public models for deterministic shared-cash portfolio backtests."""

from dataclasses import dataclass
from decimal import Decimal

from cryptolab.backtesting.accounting import (
    LotRealization,
    PositionCycle,
    PositionLot,
)
from cryptolab.backtesting.models import (
    BacktestConfig,
    CompletedTrade,
    Fill,
    SkippedDecision,
)
from cryptolab.execution.models import (
    Fill as ExecutionFill,
    Instrument,
    Order,
    OrderIntent,
)
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.markets import DEFAULT_EXCHANGE, SPOT_MARKET_TYPE
from cryptolab.strategies.base import Strategy


@dataclass(frozen=True)
class PortfolioBacktestInput:
    """One strategy/instrument stream participating in a shared portfolio."""

    strategy: Strategy
    symbol: str
    interval: str
    rows: tuple[FeatureRow, ...]
    exchange: str = DEFAULT_EXCHANGE
    market_type: str = SPOT_MARKET_TYPE


@dataclass(frozen=True)
class PositionValue:
    """Final value and FIFO cost of one open instrument position."""

    instrument: Instrument
    quantity: Decimal
    cost_basis: Decimal
    mark_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class PortfolioEquityPoint:
    """Shared-cash portfolio equity marked at aligned candle close."""

    candle_open_time: int
    available_time: int
    quote_balance: Decimal
    positions: tuple[PositionValue, ...]
    equity: Decimal


@dataclass(frozen=True)
class PortfolioBacktestReport:
    """Result of several instruments sharing one deterministic cash balance."""

    quote_asset: str
    interval: str
    config: BacktestConfig
    position_sizer_name: str
    final_quote_balance: Decimal
    positions: tuple[PositionValue, ...]
    final_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    maximum_equity_drawdown: Decimal
    fills: tuple[Fill, ...]
    completed_trades: tuple[CompletedTrade, ...]
    skipped_decisions: tuple[SkippedDecision, ...]
    order_intents: tuple[OrderIntent, ...]
    open_lots: tuple[PositionLot, ...]
    lot_realizations: tuple[LotRealization, ...]
    position_cycles: tuple[PositionCycle, ...]
    equity_curve: tuple[PortfolioEquityPoint, ...]
    orders: tuple[Order, ...] = ()
    execution_fills: tuple[ExecutionFill, ...] = ()

    @property
    def total_return(self) -> Decimal:
        return self.total_pnl / self.config.initial_cash
