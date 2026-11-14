"""Deterministic, research-only long Spot backtesting."""

from cryptolab.backtesting.benchmarks import (
    BenchmarkReport,
    calculate_benchmarks,
)
from cryptolab.backtesting.engine import BacktestEngine
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
    SimulatedFill,
    SkippedDecision,
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
    "BacktestReport",
    "BenchmarkReport",
    "CompletedTrade",
    "EquityPoint",
    "Fill",
    "FixedFractionPositionSizer",
    "FixedFractionScaleSizer",
    "LegacyAllInAllOutSizer",
    "PortfolioState",
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
    "TrailingStopFill",
    "TrailingStopTriggerKind",
    "calculate_benchmarks",
]
