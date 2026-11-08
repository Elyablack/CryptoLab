"""Immutable models for deterministic Spot backtesting."""

from dataclasses import dataclass
from decimal import Decimal

from cryptolab.strategies.models import Signal, StrategyDecision


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions expressed as decimal fractions."""

    initial_cash: Decimal = Decimal("1000")
    fee_rate: Decimal = Decimal("0.001")
    slippage_rate: Decimal = Decimal("0.0005")

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


@dataclass(frozen=True)
class CompletedTrade:
    """One completed long trade from BUY fill through SELL fill."""

    entry_fill: SimulatedFill
    exit_fill: SimulatedFill
    pnl: Decimal


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
    fills: tuple[SimulatedFill, ...]
    completed_trades: tuple[CompletedTrade, ...]
    skipped_decisions: tuple[SkippedDecision, ...]
    equity_curve: tuple[EquityPoint, ...]

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
