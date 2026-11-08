"""Deterministic, research-only long Spot backtesting."""

from cryptolab.backtesting.benchmarks import (
    BenchmarkReport,
    calculate_benchmarks,
)
from cryptolab.backtesting.engine import BacktestEngine
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

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestReport",
    "BenchmarkReport",
    "CompletedTrade",
    "EquityPoint",
    "Fill",
    "PortfolioState",
    "SimulatedFill",
    "SkippedDecision",
    "TrailingStopFill",
    "TrailingStopTriggerKind",
    "calculate_benchmarks",
]
