"""Deterministic, research-only long Spot backtesting."""

from cryptolab.backtesting.benchmarks import (
    BenchmarkReport,
    calculate_benchmarks,
)
from cryptolab.backtesting.engine import (
    BacktestEngine,
    BacktestExecutionSession,
    StrategyExecutionOutcome,
)
from cryptolab.backtesting.accounting import (
    LotRealization,
    PortfolioAccounting,
    PositionCycle,
    PositionLot,
)
from cryptolab.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    CompletedTrade,
    EquityPoint,
    Fill,
    PortfolioState,
    ProtectiveStopFill,
    ProtectiveStopTriggerKind,
    SimulatedFill,
    SkippedDecision,
    TimedExitFill,
    TrailingStopFill,
    TrailingStopTriggerKind,
)
from cryptolab.backtesting.portfolio import (
    PortfolioBacktestInput,
    PortfolioBacktestReport,
    PortfolioEquityPoint,
    PositionValue,
)
from cryptolab.backtesting.sizing import (
    BuySizingProposal,
    FixedFractionPositionSizer,
    FixedFractionScaleSizer,
    LegacyAllInAllOutSizer,
    PositionSizer,
    SellSizingProposal,
    SizingContext,
    SizingRejection,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestExecutionSession",
    "BacktestReport",
    "BenchmarkReport",
    "CompletedTrade",
    "EquityPoint",
    "Fill",
    "FixedFractionPositionSizer",
    "FixedFractionScaleSizer",
    "LegacyAllInAllOutSizer",
    "PortfolioState",
    "ProtectiveStopFill",
    "ProtectiveStopTriggerKind",
    "PositionSizer",
    "PositionLot",
    "LotRealization",
    "PositionCycle",
    "PortfolioAccounting",
    "PortfolioBacktestInput",
    "PortfolioBacktestReport",
    "PortfolioEquityPoint",
    "PositionValue",
    "SimulatedFill",
    "SizingContext",
    "SizingRejection",
    "BuySizingProposal",
    "SellSizingProposal",
    "SkippedDecision",
    "StrategyExecutionOutcome",
    "TimedExitFill",
    "TrailingStopFill",
    "TrailingStopTriggerKind",
    "calculate_benchmarks",
]
