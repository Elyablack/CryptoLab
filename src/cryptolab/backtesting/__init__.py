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
    PortfolioState,
    SimulatedFill,
    SkippedDecision,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestReport",
    "BenchmarkReport",
    "CompletedTrade",
    "EquityPoint",
    "PortfolioState",
    "SimulatedFill",
    "SkippedDecision",
    "calculate_benchmarks",
]
