"""Immutable models for deterministic historical event replay."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from cryptolab.backtesting.engine import StrategyExecutionOutcome
from cryptolab.backtesting.models import BacktestReport, PortfolioState
from cryptolab.market_data.models import Candle
from cryptolab.strategies.models import StrategyDecision


class ReplayEventType(str, Enum):
    CANDLE_OPEN = "candle_open"
    POSITION_SIZED = "position_sized"
    RISK_CHECKED = "risk_checked"
    ORDER_INTENT_CREATED = "order_intent_created"
    FILL_APPLIED = "fill_applied"
    ACCOUNTING_UPDATED = "accounting_updated"
    DECISION_SKIPPED = "decision_skipped"
    CANDLE_CLOSED = "candle_closed"
    FEATURE_UPDATED = "feature_updated"
    STRATEGY_DECISION = "strategy_decision"
    PORTFOLIO_SNAPSHOT = "portfolio_snapshot"
    UNEXECUTED_DECISION = "unexecuted_decision"


ReplayMetadataValue = str | int | Decimal | bool | None


@dataclass(frozen=True)
class ReplayEvent:
    """One uniquely identified event in global deterministic order."""

    sequence: int
    event_id: str
    event_type: ReplayEventType
    occurred_at: int
    source_candle_open_time: int
    metadata: Mapping[str, ReplayMetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("Replay event sequence must be positive")
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("Replay event_id must be non-empty")
        if not isinstance(self.event_type, ReplayEventType):
            raise ValueError("event_type must be a ReplayEventType")
        for name, value in (
            ("occurred_at", self.occurred_at),
            ("source_candle_open_time", self.source_candle_open_time),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )


@dataclass(frozen=True)
class ReplayPortfolioSnapshot:
    """Portfolio state marked at a controlled market time."""

    sequence: int
    phase: str
    observed_at: int
    candle_open_time: int
    mark_price: Decimal
    state: PortfolioState
    equity: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class ReplayCandleResult:
    """Result cached for idempotent processing of one candle event."""

    candle_event_id: str
    candle: Candle
    executed_decision: StrategyDecision | None
    execution_outcome: StrategyExecutionOutcome | None
    new_decision: StrategyDecision
    open_snapshot: ReplayPortfolioSnapshot
    close_snapshot: ReplayPortfolioSnapshot


@dataclass(frozen=True)
class DeterministicReplayReport:
    """Replay audit trail plus the standard comparable backtest report."""

    backtest: BacktestReport
    decisions: tuple[StrategyDecision, ...]
    events: tuple[ReplayEvent, ...]
    snapshots: tuple[ReplayPortfolioSnapshot, ...]
    candle_event_ids: tuple[str, ...]
    risk_policy_name: str

    @property
    def final_equity(self) -> Decimal:
        return self.backtest.final_equity

