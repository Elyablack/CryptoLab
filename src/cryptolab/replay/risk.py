"""Minimal deterministic risk gate for replay Stage 1."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from cryptolab.backtesting.models import PortfolioState
from cryptolab.backtesting.sizing import (
    BuySizingProposal,
    SellSizingProposal,
    SizingRejection,
)
from cryptolab.execution.models import Instrument
from cryptolab.strategies.models import StrategyDecision


ReplaySizing = BuySizingProposal | SellSizingProposal | SizingRejection | None


@dataclass(frozen=True)
class ReplayRiskContext:
    """Information available after sizing and before intent creation."""

    decision: StrategyDecision
    sizing: ReplaySizing
    portfolio: PortfolioState
    instrument: Instrument
    market_open_price: Decimal
    observed_at: int


@dataclass(frozen=True)
class RiskCheckResult:
    accepted: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise ValueError("accepted must be a boolean")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Risk check reason must be non-empty")


class ReplayRiskPolicy(Protocol):
    @property
    def name(self) -> str: ...

    def check(self, context: ReplayRiskContext) -> RiskCheckResult: ...


@dataclass(frozen=True)
class AllowAllReplayRiskPolicy:
    """Stage 1 policy: preserve backtest behavior without extra limits."""

    @property
    def name(self) -> str:
        return "allow_all"

    def check(self, context: ReplayRiskContext) -> RiskCheckResult:
        if not isinstance(context, ReplayRiskContext):
            raise ValueError("context must be a ReplayRiskContext")
        return RiskCheckResult(
            accepted=True,
            reason="Stage 1 policy accepts the deterministic sizing result",
        )

