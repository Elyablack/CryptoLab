"""Immutable models for deterministic Spot backtesting."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from cryptolab.backtesting.accounting import (
    LotRealization,
    PositionCycle,
    PositionLot,
)
from cryptolab.execution.models import (
    Fill as ExecutionFill,
    Instrument,
    Order,
    OrderIntent,
)
from cryptolab.strategies.models import Signal, StrategyDecision


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions expressed as decimal fractions."""

    initial_cash: Decimal = Decimal("1000")
    fee_rate: Decimal = Decimal("0.001")
    slippage_rate: Decimal = Decimal("0.0005")
    trailing_stop_rate: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.initial_cash, Decimal)
            or not self.initial_cash.is_finite()
            or self.initial_cash <= 0
        ):
            raise ValueError("initial_cash must be a finite positive Decimal")
        for name, value in (
            ("fee_rate", self.fee_rate),
            ("slippage_rate", self.slippage_rate),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or not Decimal("0") <= value < 1
            ):
                raise ValueError(f"{name} must be between 0 (inclusive) and 1")
        if self.trailing_stop_rate is not None and (
            not isinstance(self.trailing_stop_rate, Decimal)
            or not self.trailing_stop_rate.is_finite()
            or not Decimal("0") < self.trailing_stop_rate < 1
        ):
            raise ValueError("trailing_stop_rate must be between 0 and 1")


@dataclass(frozen=True)
class PortfolioState:
    """Long-only virtual portfolio state after an execution event."""

    quote_balance: Decimal
    base_balance: Decimal
    entry_cost: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal


@dataclass(frozen=True)
class SimulatedFill:
    """A deterministic fill at the next candle's adversely slipped open."""

    side: Signal
    signal_open_time: int
    signal_available_time: int
    execution_open_time: int
    market_open_price: Decimal
    execution_price: Decimal
    base_quantity: Decimal
    quote_notional: Decimal
    fee: Decimal
    quote_balance_after: Decimal
    base_balance_after: Decimal
    intent_id: str | None = None
    fill_id: str | None = None
    instrument: Instrument | None = None


class TrailingStopTriggerKind(str, Enum):
    """How an active trailing stop was crossed within an OHLC candle."""

    GAP_OPEN = "gap_open"
    INTRABAR = "intrabar"


@dataclass(frozen=True)
class TrailingStopFill:
    """A stop-market SELL without fabricated strategy signal timestamps."""

    trigger_kind: TrailingStopTriggerKind
    trigger_candle_open_time: int
    high_watermark: Decimal
    active_stop_price: Decimal
    market_reference_price: Decimal
    execution_price: Decimal
    base_quantity: Decimal
    quote_notional: Decimal
    fee: Decimal
    quote_balance_after: Decimal
    base_balance_after: Decimal
    fill_id: str | None = None
    instrument: Instrument | None = None
    intent_id: str | None = None

    @property
    def side(self) -> Signal:
        return Signal.SELL


Fill = SimulatedFill | TrailingStopFill


@dataclass(frozen=True)
class CompletedTrade:
    """One realized FIFO lot portion, or one legacy round trip.

    The optional allocation fields preserve the original public constructor
    while allowing a partial exit to report only the consumed quantity and
    its allocated entry cost/proceeds.
    """

    entry_fill: SimulatedFill
    exit_fill: Fill
    pnl: Decimal
    quantity: Decimal | None = None
    entry_cost: Decimal | None = None
    exit_net_proceeds: Decimal | None = None
    entry_fee: Decimal | None = None
    exit_fee: Decimal | None = None

    @property
    def closed_quantity(self) -> Decimal:
        return self.quantity or self.entry_fill.base_quantity

    @property
    def allocated_entry_cost(self) -> Decimal:
        return self.entry_cost or (
            self.entry_fill.quote_notional + self.entry_fill.fee
        )


@dataclass(frozen=True)
class SkippedDecision:
    """An actionable decision that could not produce a fill."""

    decision: StrategyDecision
    reason: str


@dataclass(frozen=True)
class EquityPoint:
    """Portfolio marked to the candle close after any fill at its open."""

    candle_open_time: int
    available_time: int
    close_price: Decimal
    quote_balance: Decimal
    base_balance: Decimal
    equity: Decimal


@dataclass(frozen=True)
class BacktestReport:
    """Result for one strategy and one independent virtual portfolio.

    ``maximum_equity_drawdown`` follows the CryptoLab convention and is a
    non-positive return: ``-0.20`` means a 20% peak-to-trough decline.
    """

    strategy_name: str
    exchange: str
    market_type: str
    symbol: str
    interval: str
    config: BacktestConfig
    final_state: PortfolioState
    final_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    maximum_equity_drawdown: Decimal
    fills: tuple[Fill, ...]
    completed_trades: tuple[CompletedTrade, ...]
    skipped_decisions: tuple[SkippedDecision, ...]
    equity_curve: tuple[EquityPoint, ...]
    position_sizer_name: str = "legacy_all_in_all_out"
    order_intents: tuple[OrderIntent, ...] = ()
    open_lots: tuple[PositionLot, ...] = ()
    lot_realizations: tuple[LotRealization, ...] = ()
    position_cycles: tuple[PositionCycle, ...] = ()
    orders: tuple[Order, ...] = ()
    execution_fills: tuple[ExecutionFill, ...] = ()

    @property
    def total_return(self) -> Decimal:
        return self.total_pnl / self.config.initial_cash

    @property
    def win_rate(self) -> Decimal | None:
        if not self.completed_trades:
            return None
        wins = sum(trade.pnl > 0 for trade in self.completed_trades)
        return Decimal(wins) / Decimal(len(self.completed_trades))

    @property
    def fees_paid(self) -> Decimal:
        return self.final_state.fees_paid

    @property
    def completed_trade_count(self) -> int:
        return len(self.completed_trades)

    @property
    def exposure_percentage(self) -> Decimal:
        """Share of candle-close equity points with an open base position."""
        if not self.equity_curve:
            return Decimal("0")
        exposed_points = sum(
            point.base_balance > 0
            for point in self.equity_curve
        )
        return Decimal(exposed_points) / Decimal(len(self.equity_curve))
