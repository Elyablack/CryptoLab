"""Typed intent models that do not perform or imply real order execution."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class OrderSide(str, Enum):
    """Direction of a proposed Spot order."""

    BUY = "BUY"
    SELL = "SELL"


class IntentSource(str, Enum):
    """Auditable origin of an order intent."""

    STRATEGY = "strategy"
    TRAILING_STOP = "trailing_stop"
    RISK = "risk"


class OrderStatus(str, Enum):
    """Explicit lifecycle state for a research or paper order."""

    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }


@dataclass(frozen=True)
class Instrument:
    """Explicit exchange and asset identity for one Spot instrument."""

    exchange: str
    market_type: str
    symbol: str
    base_asset: str
    quote_asset: str

    def __post_init__(self) -> None:
        values = (
            self.exchange,
            self.market_type,
            self.symbol,
            self.base_asset,
            self.quote_asset,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Instrument identifiers must be non-empty strings")
        if self.base_asset == self.quote_asset:
            raise ValueError("Instrument base and quote assets must differ")


@dataclass(frozen=True)
class OrderIntent:
    """Approved desired action, separate from an order or execution fill.

    Phase 1 creates intents only for strategy decisions. An intent records a
    base-asset quantity but does not submit an exchange order. The deterministic
    identifiers make accidental duplicate conversion of one source event
    detectable during a backtest.
    """

    intent_id: str
    source_event_id: str
    source: IntentSource
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    created_at: int
    earliest_execution_at: int
    signal_available_time: int | None
    source_candle_open_time: int
    explanation: str

    def __post_init__(self) -> None:
        for name, value in (
            ("intent_id", self.intent_id),
            ("source_event_id", self.source_event_id),
            ("explanation", self.explanation),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.source, IntentSource):
            raise ValueError("source must be an IntentSource")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide")
        if (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise ValueError("quantity must be a finite positive Decimal")

        timestamps = (
            self.created_at,
            self.earliest_execution_at,
            self.source_candle_open_time,
        )
        if any(type(value) is not int or value < 0 for value in timestamps):
            raise ValueError("Order intent timestamps must be non-negative integers")
        if self.signal_available_time is not None and (
            type(self.signal_available_time) is not int
            or self.signal_available_time < 0
        ):
            raise ValueError(
                "signal_available_time must be a non-negative integer or None"
            )
        if self.source is IntentSource.STRATEGY:
            if self.signal_available_time is None:
                raise ValueError(
                    "Strategy intent requires signal_available_time"
                )
            if self.created_at < self.signal_available_time:
                raise ValueError(
                    "Strategy intent cannot be created before signal availability"
                )
            if self.earliest_execution_at < self.signal_available_time:
                raise ValueError(
                    "Strategy intent cannot execute before signal availability"
                )
        if self.earliest_execution_at < self.created_at:
            raise ValueError(
                "earliest_execution_at cannot be earlier than created_at"
            )


@dataclass(frozen=True)
class Order:
    """Venue-neutral order state; quantity is always base-asset quantity."""

    order_id: str
    intent_id: str
    instrument: Instrument
    side: OrderSide
    requested_quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus
    created_at: int
    updated_at: int
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("order_id", "intent_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide")
        for name, value in (
            ("requested_quantity", self.requested_quantity),
            ("filled_quantity", self.filled_quantity),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if self.requested_quantity <= 0:
            raise ValueError("requested_quantity must be positive")
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("filled_quantity cannot exceed requested_quantity")
        if not isinstance(self.status, OrderStatus):
            raise ValueError("status must be an OrderStatus")
        if (
            type(self.created_at) is not int
            or type(self.updated_at) is not int
            or self.created_at < 0
            or self.updated_at < self.created_at
        ):
            raise ValueError("Order timestamps are invalid")
        if self.status is OrderStatus.NEW and self.filled_quantity != 0:
            raise ValueError("A new order cannot have filled quantity")
        if (
            self.status is OrderStatus.PARTIALLY_FILLED
            and not Decimal("0") < self.filled_quantity < self.requested_quantity
        ):
            raise ValueError("Partially filled order quantity is inconsistent")
        if (
            self.status is OrderStatus.FILLED
            and self.filled_quantity != self.requested_quantity
        ):
            raise ValueError("Filled order must contain the requested quantity")
        if self.terminal_reason is not None and (
            not isinstance(self.terminal_reason, str)
            or not self.terminal_reason.strip()
        ):
            raise ValueError("terminal_reason must be non-empty when provided")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity


@dataclass(frozen=True)
class Fill:
    """One venue execution; an Order may receive several unique fills."""

    fill_id: str
    order_id: str
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    price: Decimal
    quote_notional: Decimal
    fee: Decimal
    fee_asset: str
    executed_at: int
    venue_fill_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("fill_id", "order_id", "fee_asset"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide")
        for name, value in (
            ("quantity", self.quantity),
            ("price", self.price),
            ("quote_notional", self.quote_notional),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive Decimal")
        if (
            not isinstance(self.fee, Decimal)
            or not self.fee.is_finite()
            or self.fee < 0
        ):
            raise ValueError("fee must be a finite non-negative Decimal")
        if type(self.executed_at) is not int or self.executed_at < 0:
            raise ValueError("executed_at must be a non-negative integer")
        if self.venue_fill_id is not None and (
            not isinstance(self.venue_fill_id, str)
            or not self.venue_fill_id.strip()
        ):
            raise ValueError("venue_fill_id must be non-empty when provided")
        scale = max(
            Decimal("1"),
            abs(self.quote_notional),
            abs(self.quantity * self.price),
        )
        if abs(self.quote_notional - self.quantity * self.price) > (
            scale * Decimal("1e-24")
        ):
            raise ValueError(
                "quote_notional must equal quantity times price"
            )
