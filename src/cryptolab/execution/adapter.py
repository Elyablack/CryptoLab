"""Offline execution adapters for research, paper tests, and recovery tests."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol

from cryptolab.execution.lifecycle import (
    BalanceReservation,
    LifecycleSnapshot,
    OrderLifecycle,
)
from cryptolab.execution.models import Fill, Order, OrderIntent, OrderStatus


class ExecutionEventType(str, Enum):
    ORDER_ACCEPTED = "order_accepted"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    ORDER_EXPIRED = "order_expired"


@dataclass(frozen=True)
class ExecutionEvent:
    sequence: int
    event_id: str
    event_type: ExecutionEventType
    order_id: str
    occurred_at: int
    fill_id: str | None = None


@dataclass(frozen=True)
class ExecutionAdapterSnapshot:
    """All local state required for deterministic restart recovery."""

    lifecycle: LifecycleSnapshot
    intents: tuple[OrderIntent, ...]
    events: tuple[ExecutionEvent, ...]
    next_sequence: int


@dataclass(frozen=True)
class ReconciliationReport:
    missing_local_order_ids: tuple[str, ...]
    missing_remote_order_ids: tuple[str, ...]
    mismatched_order_ids: tuple[str, ...]
    missing_local_fill_ids: tuple[str, ...]
    missing_remote_fill_ids: tuple[str, ...]
    mismatched_fill_ids: tuple[str, ...]

    @property
    def is_consistent(self) -> bool:
        return not any((
            self.missing_local_order_ids,
            self.missing_remote_order_ids,
            self.mismatched_order_ids,
            self.missing_local_fill_ids,
            self.missing_remote_fill_ids,
            self.mismatched_fill_ids,
        ))


class ExecutionAdapter(Protocol):
    """Venue-neutral contract; implementations must be idempotent."""

    @property
    def orders(self) -> tuple[Order, ...]: ...

    @property
    def fills(self) -> tuple[Fill, ...]: ...

    @property
    def reservations(self) -> tuple[BalanceReservation, ...]: ...

    def submit(
        self,
        intent: OrderIntent,
        *,
        order_id: str,
        created_at: int,
        reservation_amount: Decimal,
        available_balance: Decimal,
    ) -> Order: ...

    def apply_fill(self, fill: Fill) -> tuple[Order, bool]: ...

    def cancel(self, order_id: str, *, at: int, reason: str) -> Order: ...

    def snapshot(self) -> ExecutionAdapterSnapshot: ...


class SimulatedExecutionAdapter:
    """Local adapter using the same lifecycle as deterministic backtests."""

    def __init__(self, lifecycle: OrderLifecycle | None = None) -> None:
        self._lifecycle = lifecycle or OrderLifecycle()
        self._intents: dict[str, OrderIntent] = {}
        self._events: list[ExecutionEvent] = []
        self._event_ids: set[str] = set()
        self._next_sequence = 1

    @property
    def orders(self) -> tuple[Order, ...]:
        return self._lifecycle.orders

    @property
    def fills(self) -> tuple[Fill, ...]:
        return self._lifecycle.fills

    @property
    def reservations(self) -> tuple[BalanceReservation, ...]:
        return self._lifecycle.reservations

    @property
    def events(self) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events)

    def submit(
        self,
        intent: OrderIntent,
        *,
        order_id: str,
        created_at: int,
        reservation_amount: Decimal,
        available_balance: Decimal,
    ) -> Order:
        existing_intent = self._intents.get(intent.intent_id)
        if existing_intent is not None and existing_intent != intent:
            raise ValueError("Conflicting duplicate order intent")
        before = len(self.orders)
        order = self._lifecycle.create_order(
            intent,
            order_id=order_id,
            created_at=created_at,
            reservation_amount=reservation_amount,
            available_balance=available_balance,
        )
        self._intents[intent.intent_id] = intent
        if len(self.orders) != before:
            self._record_event(
                event_id=f"accepted:{order.order_id}",
                event_type=ExecutionEventType.ORDER_ACCEPTED,
                order=order,
                occurred_at=created_at,
            )
        return order

    def apply_fill(self, fill: Fill) -> tuple[Order, bool]:
        order, applied = self._lifecycle.apply_fill(fill)
        if applied:
            event_type = (
                ExecutionEventType.ORDER_FILLED
                if order.status is OrderStatus.FILLED
                else ExecutionEventType.ORDER_PARTIALLY_FILLED
            )
            self._record_event(
                event_id=f"fill:{fill.fill_id}",
                event_type=event_type,
                order=order,
                occurred_at=fill.executed_at,
                fill_id=fill.fill_id,
            )
        return order, applied

    def cancel(self, order_id: str, *, at: int, reason: str) -> Order:
        order = self._lifecycle.cancel(order_id, at=at, reason=reason)
        self._record_event(
            event_id=f"cancelled:{order_id}",
            event_type=ExecutionEventType.ORDER_CANCELLED,
            order=order,
            occurred_at=at,
        )
        return order

    def reject(self, order_id: str, *, at: int, reason: str) -> Order:
        order = self._lifecycle.reject(order_id, at=at, reason=reason)
        self._record_event(
            event_id=f"rejected:{order_id}",
            event_type=ExecutionEventType.ORDER_REJECTED,
            order=order,
            occurred_at=at,
        )
        return order

    def expire(self, order_id: str, *, at: int, reason: str) -> Order:
        order = self._lifecycle.expire(order_id, at=at, reason=reason)
        self._record_event(
            event_id=f"expired:{order_id}",
            event_type=ExecutionEventType.ORDER_EXPIRED,
            order=order,
            occurred_at=at,
        )
        return order

    def snapshot(self) -> ExecutionAdapterSnapshot:
        return ExecutionAdapterSnapshot(
            lifecycle=self._lifecycle.snapshot(),
            intents=tuple(
                self._intents[key] for key in sorted(self._intents)
            ),
            events=self.events,
            next_sequence=self._next_sequence,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ExecutionAdapterSnapshot,
    ) -> "SimulatedExecutionAdapter":
        if not isinstance(snapshot, ExecutionAdapterSnapshot):
            raise ValueError("snapshot must be an ExecutionAdapterSnapshot")
        adapter = cls(OrderLifecycle.from_snapshot(snapshot.lifecycle))
        adapter._intents = {
            intent.intent_id: intent for intent in snapshot.intents
        }
        if len(adapter._intents) != len(snapshot.intents):
            raise ValueError("Snapshot contains duplicate intents")
        adapter._events = list(snapshot.events)
        adapter._event_ids = {event.event_id for event in snapshot.events}
        if len(adapter._event_ids) != len(snapshot.events):
            raise ValueError("Snapshot contains duplicate event IDs")
        expected_sequence = tuple(range(1, len(snapshot.events) + 1))
        if tuple(event.sequence for event in snapshot.events) != expected_sequence:
            raise ValueError("Snapshot event sequence is not contiguous")
        if snapshot.next_sequence != len(snapshot.events) + 1:
            raise ValueError("Snapshot next_sequence is inconsistent")
        adapter._next_sequence = snapshot.next_sequence
        if {order.intent_id for order in adapter.orders} != set(adapter._intents):
            raise ValueError("Snapshot orders and intents do not reconcile")
        return adapter

    def reconcile(
        self,
        venue_snapshot: ExecutionAdapterSnapshot,
    ) -> ReconciliationReport:
        local_orders = {item.order_id: item for item in self.orders}
        venue_orders = {
            item.order_id: item for item in venue_snapshot.lifecycle.orders
        }
        local_fills = {item.fill_id: item for item in self.fills}
        venue_fills = {
            item.fill_id: item for item in venue_snapshot.lifecycle.fills
        }
        return ReconciliationReport(
            missing_local_order_ids=tuple(sorted(
                set(venue_orders) - set(local_orders)
            )),
            missing_remote_order_ids=tuple(sorted(
                set(local_orders) - set(venue_orders)
            )),
            mismatched_order_ids=tuple(sorted(
                order_id
                for order_id in set(local_orders) & set(venue_orders)
                if local_orders[order_id] != venue_orders[order_id]
            )),
            missing_local_fill_ids=tuple(sorted(
                set(venue_fills) - set(local_fills)
            )),
            missing_remote_fill_ids=tuple(sorted(
                set(local_fills) - set(venue_fills)
            )),
            mismatched_fill_ids=tuple(sorted(
                fill_id
                for fill_id in set(local_fills) & set(venue_fills)
                if local_fills[fill_id] != venue_fills[fill_id]
            )),
        )

    def _record_event(
        self,
        *,
        event_id: str,
        event_type: ExecutionEventType,
        order: Order,
        occurred_at: int,
        fill_id: str | None = None,
    ) -> None:
        if event_id in self._event_ids:
            raise ValueError(f"Duplicate execution event: {event_id}")
        self._events.append(ExecutionEvent(
            sequence=self._next_sequence,
            event_id=event_id,
            event_type=event_type,
            order_id=order.order_id,
            occurred_at=occurred_at,
            fill_id=fill_id,
        ))
        self._event_ids.add(event_id)
        self._next_sequence += 1


class FakeVenueExecutionAdapter(SimulatedExecutionAdapter):
    """Offline venue double with idempotent externally identified fills."""

    def process_fill_event(
        self,
        *,
        event_id: str,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
        quote_notional: Decimal,
        fee: Decimal,
        fee_asset: str,
        executed_at: int,
    ) -> tuple[Order, bool]:
        fill_id = f"venue-fill:{event_id}"
        order = next(
            (item for item in self.orders if item.order_id == order_id),
            None,
        )
        if order is None:
            raise ValueError(f"Unknown order: {order_id}")
        fill = Fill(
            fill_id=fill_id,
            order_id=order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=quantity,
            price=price,
            quote_notional=quote_notional,
            fee=fee,
            fee_asset=fee_asset,
            executed_at=executed_at,
            venue_fill_id=event_id,
        )
        return self.apply_fill(fill)
