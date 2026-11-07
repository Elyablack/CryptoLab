"""Common strategy interface and decision construction."""

from collections.abc import Mapping, Sequence
from typing import Protocol

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.models import (
    DiagnosticValue,
    Signal,
    StrategyDecision,
)


class Strategy(Protocol):
    """Interface for deterministic, research-only signal generation."""

    @property
    def name(self) -> str: ...

    @property
    def minimum_history(self) -> int: ...

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ) -> StrategyDecision: ...


def make_decision(
    *,
    strategy_name: str,
    symbol: str,
    interval: str,
    row: FeatureRow,
    signal: Signal,
    explanation: str,
    diagnostics: Mapping[str, DiagnosticValue] | None = None,
) -> StrategyDecision:
    """Create a decision tied to the latest available feature row."""
    return StrategyDecision(
        strategy_name=strategy_name,
        symbol=symbol,
        interval=interval,
        signal=signal,
        candle_open_time=row.open_time,
        signal_available_time=row.available_time,
        reference_price=row.close,
        explanation=explanation,
        diagnostics={} if diagnostics is None else diagnostics,
    )

