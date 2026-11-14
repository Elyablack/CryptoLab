"""Idempotent order lifecycle and deterministic balance reservations."""

from dataclasses import dataclass, replace
from decimal import Decimal

from cryptolab.execution.models import (
    Fill,
    Instrument,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
)


ZERO = Decimal("0")


@dataclass(frozen=True)
class BalanceReservation:
    order_id: str
    instrument: Instrument
    asset: str
    original_amount: Decimal
    remaining_amount: Decimal


@dataclass(frozen=True)
class LifecycleSnapshot:
    """Immutable restart state for orders, fills, and reservations."""

    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    reservations: tuple[BalanceReservation, ...]


class ReservationBook:
    """Track funds unavailable to other open orders without moving balances."""

    def __init__(self) -> None:
        self._reservations: dict[str, BalanceReservation] = {}

    @property
    def reservations(self) -> tuple[BalanceReservation, ...]:
        return tuple(
            self._reservations[key]
            for key in sorted(self._reservations)
        )

    def reserve(
        self,
        *,
        order_id: str,
        instrument: Instrument,
        asset: str,
        amount: Decimal,
        available_balance: Decimal,
    ) -> BalanceReservation:
        if order_id in self._reservations:
            raise ValueError(f"Order already has a reservation: {order_id}")
        for name, value in (("amount", amount), ("available_balance", available_balance)):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if amount <= 0:
            raise ValueError("Reservation amount must be positive")
        already_reserved = sum(
            (
                item.remaining_amount
                for item in self._reservations.values()
                if (
                    item.asset == asset
                    and item.instrument.exchange == instrument.exchange
                    and item.instrument.market_type == instrument.market_type
                )
            ),
            ZERO,
        )
        if amount + already_reserved > available_balance:
            raise ValueError("Reservation exceeds available balance")
        reservation = BalanceReservation(
            order_id=order_id,
            instrument=instrument,
            asset=asset,
            original_amount=amount,
            remaining_amount=amount,
        )
        self._reservations[order_id] = reservation
        return reservation

    def consume(self, order_id: str, amount: Decimal) -> BalanceReservation:
        if order_id not in self._reservations:
            raise ValueError(f"No reservation for order: {order_id}")
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError("Consumed reservation must be non-negative")
        current = self._reservations[order_id]
        if amount > current.remaining_amount:
            raise ValueError("Fill exceeds the remaining balance reservation")
        updated = replace(
            current,
            remaining_amount=current.remaining_amount - amount,
        )
        self._reservations[order_id] = updated
        return updated

    def release(self, order_id: str) -> BalanceReservation | None:
        current = self._reservations.get(order_id)
        if current is None:
            return None
        updated = replace(current, remaining_amount=ZERO)
        self._reservations[order_id] = updated
        return updated


class OrderLifecycle:
    """Own order transitions, fill idempotency, and reservations."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._intent_orders: dict[str, str] = {}
        self._fills: dict[str, Fill] = {}
        self._venue_fill_ids: dict[str, str] = {}
        self._reservations = ReservationBook()

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(self._orders[key] for key in sorted(self._orders))

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills.values())

    @property
    def reservations(self) -> tuple[BalanceReservation, ...]:
        return self._reservations.reservations

    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            orders=self.orders,
            fills=self.fills,
            reservations=self.reservations,
        )

    @classmethod
    def from_snapshot(cls, snapshot: LifecycleSnapshot) -> "OrderLifecycle":
        if not isinstance(snapshot, LifecycleSnapshot):
            raise ValueError("snapshot must be a LifecycleSnapshot")
        lifecycle = cls()
        for order in snapshot.orders:
            if order.order_id in lifecycle._orders:
                raise ValueError("Snapshot contains duplicate order IDs")
            if order.intent_id in lifecycle._intent_orders:
                raise ValueError("Snapshot contains duplicate intent IDs")
            lifecycle._orders[order.order_id] = order
            lifecycle._intent_orders[order.intent_id] = order.order_id
        for fill in snapshot.fills:
            if fill.fill_id in lifecycle._fills:
                raise ValueError("Snapshot contains duplicate fill IDs")
            if fill.order_id not in lifecycle._orders:
                raise ValueError("Snapshot fill references an unknown order")
            if fill.venue_fill_id is not None:
                if fill.venue_fill_id in lifecycle._venue_fill_ids:
                    raise ValueError(
                        "Snapshot contains duplicate venue fill IDs"
                    )
                lifecycle._venue_fill_ids[fill.venue_fill_id] = fill.fill_id
            lifecycle._fills[fill.fill_id] = fill
        for reservation in snapshot.reservations:
            if reservation.order_id not in lifecycle._orders:
                raise ValueError(
                    "Snapshot reservation references an unknown order"
                )
            if reservation.order_id in lifecycle._reservations._reservations:
                raise ValueError("Snapshot contains duplicate reservations")
            lifecycle._reservations._reservations[
                reservation.order_id
            ] = reservation
        lifecycle._validate_snapshot_consistency()
        return lifecycle

    def order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as error:
            raise ValueError(f"Unknown order: {order_id}") from error

    def create_order(
        self,
        intent: OrderIntent,
        *,
        order_id: str,
        created_at: int,
        reservation_amount: Decimal,
        available_balance: Decimal,
    ) -> Order:
        if intent.intent_id in self._intent_orders:
            existing = self._orders[self._intent_orders[intent.intent_id]]
            if existing.order_id == order_id:
                return existing
            raise ValueError("Order intent has already created an order")
        if order_id in self._orders:
            raise ValueError(f"Duplicate order identifier: {order_id}")
        if created_at < intent.earliest_execution_at:
            raise ValueError("Order cannot be created before earliest execution")
        order = Order(
            order_id=order_id,
            intent_id=intent.intent_id,
            instrument=intent.instrument,
            side=intent.side,
            requested_quantity=intent.quantity,
            filled_quantity=ZERO,
            status=OrderStatus.NEW,
            created_at=created_at,
            updated_at=created_at,
        )
        asset = (
            intent.instrument.quote_asset
            if intent.side is OrderSide.BUY
            else intent.instrument.base_asset
        )
        self._reservations.reserve(
            order_id=order_id,
            instrument=intent.instrument,
            asset=asset,
            amount=reservation_amount,
            available_balance=available_balance,
        )
        self._orders[order_id] = order
        self._intent_orders[intent.intent_id] = order_id
        return order

    def apply_fill(self, fill: Fill) -> tuple[Order, bool]:
        """Apply one fill and return ``(order, applied)``.

        Repeated fill IDs or venue fill IDs are idempotent only when their
        payload identifies the same execution; conflicting reuse fails.
        """
        existing_fill = self._fills.get(fill.fill_id)
        if existing_fill is not None:
            if existing_fill != fill:
                raise ValueError("Conflicting duplicate fill identifier")
            return self.order(fill.order_id), False
        if fill.venue_fill_id is not None:
            existing_id = self._venue_fill_ids.get(fill.venue_fill_id)
            if existing_id is not None:
                if self._fills[existing_id] != fill:
                    raise ValueError("Conflicting duplicate venue fill")
                return self.order(fill.order_id), False

        order = self.order(fill.order_id)
        if order.status.is_terminal:
            raise ValueError("Cannot fill a terminal order")
        if fill.instrument != order.instrument or fill.side is not order.side:
            raise ValueError("Fill does not match its order")
        if fill.executed_at < order.created_at:
            raise ValueError("Fill cannot predate its order")
        if fill.quantity > order.remaining_quantity:
            raise ValueError("Fill would overfill the order")

        if order.side is OrderSide.BUY:
            if fill.fee_asset != order.instrument.quote_asset:
                raise ValueError("BUY lifecycle currently requires quote fee")
            reservation_consumed = fill.quote_notional + fill.fee
        else:
            reservation_consumed = fill.quantity
        self._reservations.consume(order.order_id, reservation_consumed)

        filled_quantity = order.filled_quantity + fill.quantity
        status = (
            OrderStatus.FILLED
            if filled_quantity == order.requested_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        updated = replace(
            order,
            filled_quantity=filled_quantity,
            status=status,
            updated_at=fill.executed_at,
        )
        self._orders[order.order_id] = updated
        self._fills[fill.fill_id] = fill
        if fill.venue_fill_id is not None:
            self._venue_fill_ids[fill.venue_fill_id] = fill.fill_id
        if status is OrderStatus.FILLED:
            self._reservations.release(order.order_id)
        return updated, True

    def cancel(self, order_id: str, *, at: int, reason: str) -> Order:
        return self._terminal_transition(
            order_id, OrderStatus.CANCELLED, at, reason
        )

    def reject(self, order_id: str, *, at: int, reason: str) -> Order:
        return self._terminal_transition(
            order_id, OrderStatus.REJECTED, at, reason
        )

    def expire(self, order_id: str, *, at: int, reason: str) -> Order:
        return self._terminal_transition(
            order_id, OrderStatus.EXPIRED, at, reason
        )

    def _terminal_transition(
        self,
        order_id: str,
        status: OrderStatus,
        at: int,
        reason: str,
    ) -> Order:
        order = self.order(order_id)
        if order.status.is_terminal:
            raise ValueError("Order is already terminal")
        if type(at) is not int or at < order.updated_at:
            raise ValueError("Order transition timestamp is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Order transition reason must be non-empty")
        updated = replace(
            order,
            status=status,
            updated_at=at,
            terminal_reason=reason,
        )
        self._orders[order_id] = updated
        self._reservations.release(order_id)
        return updated

    def _validate_snapshot_consistency(self) -> None:
        for order in self._orders.values():
            fills = [
                fill
                for fill in self._fills.values()
                if fill.order_id == order.order_id
            ]
            filled_quantity = sum(
                (fill.quantity for fill in fills), ZERO
            )
            if filled_quantity != order.filled_quantity:
                raise ValueError(
                    "Snapshot order quantity does not match its fills"
                )
            reservation = self._reservations._reservations.get(order.order_id)
            if reservation is None:
                raise ValueError("Snapshot order has no balance reservation")
            if order.status.is_terminal and reservation.remaining_amount != 0:
                raise ValueError(
                    "Terminal snapshot order retains a reservation"
                )
