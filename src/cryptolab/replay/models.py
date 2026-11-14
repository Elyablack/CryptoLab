"""Immutable models for deterministic historical event replay."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from cryptolab.backtesting.engine import (
    BacktestExecutionCheckpoint,
    StrategyExecutionOutcome,
)
from cryptolab.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    EquityPoint,
    PortfolioState,
)
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
    PRICE_OBSERVED = "price_observed"
    BACKFILL_DECISION_SUPPRESSED = "backfill_decision_suppressed"


class ReplayExecutionMode(str, Enum):
    NEXT_CANDLE_OPEN = "next_candle_open"
    OBSERVED_AFTER_CLOSE = "observed_after_close"


ReplayMetadataValue = str | int | Decimal | bool | None


@dataclass(frozen=True)
class PriceObservation:
    """Public market price observed by the local process at a real timestamp."""

    price: Decimal
    observed_at: int
    source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or self.price <= 0
        ):
            raise ValueError("Observed price must be a finite positive Decimal")
        if type(self.observed_at) is not int or self.observed_at < 0:
            raise ValueError("observed_at must be a non-negative integer")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Price observation source must be non-empty")


@dataclass(frozen=True)
class ObservedCandleEvent:
    """Closed candle plus the real local times used by paper execution."""

    candle: Candle
    received_at: int
    price_observation: PriceObservation | None
    trading_enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.candle, Candle):
            raise ValueError("candle must be a Candle")
        if type(self.received_at) is not int or (
            self.received_at <= self.candle.close_time
        ):
            raise ValueError("received_at must be after candle close_time")
        if type(self.trading_enabled) is not bool:
            raise ValueError("trading_enabled must be a boolean")
        observation = self.price_observation
        if observation is not None and (
            observation.observed_at < self.received_at
        ):
            raise ValueError("Price observation cannot predate candle receipt")
        if self.trading_enabled and observation is None:
            raise ValueError("Trading requires a price observation")


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


@dataclass(frozen=True)
class ReplaySessionCheckpoint:
    """Complete immutable state at a processed-candle boundary."""

    exchange: str
    market_type: str
    symbol: str
    interval: str
    strategy_name: str
    position_sizer_name: str
    risk_policy_name: str
    config: BacktestConfig
    candles: tuple[Candle, ...]
    candle_event_ids: tuple[str, ...]
    decisions: tuple[StrategyDecision, ...]
    events: tuple[ReplayEvent, ...]
    snapshots: tuple[ReplayPortfolioSnapshot, ...]
    equity_curve: tuple[EquityPoint, ...]
    execution: BacktestExecutionCheckpoint
    clock_time: int | None

