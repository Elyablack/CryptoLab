"""Immutable models for reproducible strategy research reports."""

from dataclasses import dataclass
from decimal import Decimal

from cryptolab.backtesting.benchmarks import BenchmarkReport
from cryptolab.backtesting.models import (
    BacktestReport,
    CompletedTrade,
    SimulatedFill,
)
from cryptolab.strategies.models import StrategyDecision


@dataclass(frozen=True)
class ConditionFrequency:
    """How often one documented signal condition was satisfied."""

    name: str
    satisfied: int
    evaluated: int

    @property
    def percentage(self) -> Decimal:
        if self.evaluated == 0:
            return Decimal("0")
        return Decimal(self.satisfied) / Decimal(self.evaluated)


@dataclass(frozen=True)
class TrendTradeResearch:
    """Executed trend trade linked back to its research decisions."""

    trade: CompletedTrade
    entry_decision: StrategyDecision
    exit_decision: StrategyDecision
    holding_milliseconds: int

    @property
    def return_percentage(self) -> Decimal:
        entry_cost = (
            self.trade.entry_fill.quote_notional
            + self.trade.entry_fill.fee
        )
        return self.trade.pnl / entry_cost


@dataclass(frozen=True)
class TrendOpenPositionResearch:
    """Still-open trend position with its entry decision and final mark."""

    entry_fill: SimulatedFill
    entry_decision: StrategyDecision
    entry_cost: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class BreakoutDiagnosis:
    """Counts of breakout events and long-only execution mismatches."""

    buy_signals: int
    sell_signals: int
    skipped_buy_while_holding: int
    skipped_sell_while_flat: int
    new_events: int
    continued_events: int


@dataclass(frozen=True)
class StrategyResearchReport:
    """Baseline strategies, benchmarks, and diagnostic evidence."""

    exchange: str
    market_type: str
    symbol: str
    interval: str
    start_time: int
    end_time: int
    candle_count: int
    strategy_reports: tuple[BacktestReport, ...]
    benchmarks: tuple[BenchmarkReport, ...]
    trend_trades: tuple[TrendTradeResearch, ...]
    trend_open_position: TrendOpenPositionResearch | None
    mean_reversion_conditions: tuple[ConditionFrequency, ...]
    breakout_diagnosis: BreakoutDiagnosis

