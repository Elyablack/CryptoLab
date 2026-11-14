"""Models and failure classifications for persistent replay Stage 2."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from cryptolab.backtesting.accounting import PositionLot
from cryptolab.backtesting.models import PortfolioState


class ReplayPersistenceError(RuntimeError):
    """Base error for fail-closed persistent replay operations."""


class ReplayStateIntegrityError(ReplayPersistenceError):
    """Persisted rows contradict each other or deterministic reconstruction."""


class ReplayConfigurationError(ReplayPersistenceError):
    """Recovery configuration differs from the stored session identity."""


class ReplaySessionUnavailableError(ReplayPersistenceError):
    """A failed or closed in-memory handle cannot process more events."""


class CrashPoint(str, Enum):
    BEFORE_PROCESSING = "before_processing"
    BEFORE_TRANSACTION = "before_transaction"
    AFTER_EVENT_JOURNAL = "after_event_journal"
    AFTER_DECISION = "after_decision"
    AFTER_ORDER_INTENTS = "after_order_intents"
    AFTER_ORDERS = "after_orders"
    AFTER_FILLS = "after_fills"
    AFTER_RESERVATIONS = "after_reservations"
    AFTER_ACCOUNTING = "after_accounting"
    BEFORE_CHECKPOINT = "before_checkpoint"
    AFTER_COMMIT = "after_commit"


class InjectedReplayCrash(ReplayPersistenceError):
    """Deterministic test-only failure at a declared transaction boundary."""

    def __init__(self, point: CrashPoint) -> None:
        self.point = point
        super().__init__(f"Injected replay crash at {point.value}")


@dataclass(frozen=True)
class StoredCandleEvent:
    sequence: int
    event_id: str
    open_time: int
    payload: str


@dataclass(frozen=True)
class StoredReplaySession:
    session_id: str
    configuration_json: str
    initial_risk_state_json: str
    checkpoint_sequence: int
    checkpoint_event_id: str | None
    checkpoint_open_time: int | None
    checkpoint_json: str
    risk_state_json: str
    candles: tuple[StoredCandleEvent, ...]


@dataclass(frozen=True)
class PersistentReplayState:
    session_id: str
    checkpoint_sequence: int
    checkpoint_event_id: str | None
    checkpoint_open_time: int | None
    portfolio: PortfolioState
    open_lots: tuple[PositionLot, ...]
    quote_balance: Decimal
    base_balance: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    replay_event_count: int
    decision_count: int
    order_intent_count: int
    order_count: int
    fill_count: int
    reservation_count: int
    risk_state_json: str
