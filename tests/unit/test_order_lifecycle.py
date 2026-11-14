from decimal import Decimal

import pytest

from cryptolab.execution import (
    Fill,
    Instrument,
    IntentSource,
    OrderIntent,
    OrderLifecycle,
    OrderSide,
    OrderStatus,
    ReservationBook,
)


BTC = Instrument("bybit", "spot", "BTCUSDT", "BTC", "USDT")
ETH = Instrument("bybit", "spot", "ETHUSDT", "ETH", "USDT")


def intent(
    *,
    intent_id: str = "intent-1",
    instrument: Instrument = BTC,
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal("10"),
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        source_event_id=f"event:{intent_id}",
        source=IntentSource.STRATEGY,
        instrument=instrument,
        side=side,
        quantity=quantity,
        created_at=100,
        earliest_execution_at=100,
        signal_available_time=100,
        source_candle_open_time=0,
        explanation="lifecycle test",
    )


def fill(
    fill_id: str,
    quantity: str,
    *,
    order_id: str = "order-1",
    price: str = "100",
    fee: str = "0",
    at: int = 101,
) -> Fill:
    amount = Decimal(quantity)
    execution_price = Decimal(price)
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        instrument=BTC,
        side=OrderSide.BUY,
        quantity=amount,
        price=execution_price,
        quote_notional=amount * execution_price,
        fee=Decimal(fee),
        fee_asset="USDT",
        executed_at=at,
    )


def test_partial_fills_reconcile_order_and_reservation() -> None:
    lifecycle = OrderLifecycle()
    lifecycle.create_order(
        intent(),
        order_id="order-1",
        created_at=100,
        reservation_amount=Decimal("1001"),
        available_balance=Decimal("1001"),
    )

    order, applied = lifecycle.apply_fill(
        fill("fill-1", "4", fee="0.4")
    )
    assert applied
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("4")
    assert order.remaining_quantity == Decimal("6")
    assert lifecycle.reservations[0].remaining_amount == Decimal("600.6")

    order, applied = lifecycle.apply_fill(
        fill("fill-2", "6", fee="0.6", at=102)
    )
    assert applied
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal("10")
    assert lifecycle.reservations[0].remaining_amount == 0
    assert [item.fill_id for item in lifecycle.fills] == [
        "fill-1",
        "fill-2",
    ]


def test_duplicate_fill_is_idempotent_but_conflict_fails() -> None:
    lifecycle = OrderLifecycle()
    lifecycle.create_order(
        intent(),
        order_id="order-1",
        created_at=100,
        reservation_amount=Decimal("1000"),
        available_balance=Decimal("1000"),
    )
    original = fill("fill-1", "4")
    first, applied = lifecycle.apply_fill(original)
    repeated, applied_again = lifecycle.apply_fill(original)

    assert applied
    assert not applied_again
    assert repeated == first
    assert len(lifecycle.fills) == 1

    with pytest.raises(ValueError, match="Conflicting duplicate"):
        lifecycle.apply_fill(fill("fill-1", "5"))


def test_overfill_is_rejected_without_mutating_order() -> None:
    lifecycle = OrderLifecycle()
    initial = lifecycle.create_order(
        intent(quantity=Decimal("2")),
        order_id="order-1",
        created_at=100,
        reservation_amount=Decimal("200"),
        available_balance=Decimal("200"),
    )

    with pytest.raises(ValueError, match="overfill"):
        lifecycle.apply_fill(fill("fill-1", "3"))

    assert lifecycle.order("order-1") == initial
    assert lifecycle.fills == ()
    assert lifecycle.reservations[0].remaining_amount == Decimal("200")


@pytest.mark.parametrize(
    ("transition", "status"),
    [
        ("cancel", OrderStatus.CANCELLED),
        ("reject", OrderStatus.REJECTED),
        ("expire", OrderStatus.EXPIRED),
    ],
)
def test_terminal_transitions_release_reservations(
    transition: str,
    status: OrderStatus,
) -> None:
    lifecycle = OrderLifecycle()
    lifecycle.create_order(
        intent(),
        order_id="order-1",
        created_at=100,
        reservation_amount=Decimal("1000"),
        available_balance=Decimal("1000"),
    )

    order = getattr(lifecycle, transition)(
        "order-1", at=101, reason="deterministic test"
    )

    assert order.status is status
    assert order.terminal_reason == "deterministic test"
    assert lifecycle.reservations[0].remaining_amount == 0
    with pytest.raises(ValueError, match="terminal"):
        lifecycle.apply_fill(fill("fill-1", "1", at=102))


def test_one_intent_cannot_create_two_orders() -> None:
    lifecycle = OrderLifecycle()
    first = lifecycle.create_order(
        intent(),
        order_id="order-1",
        created_at=100,
        reservation_amount=Decimal("1000"),
        available_balance=Decimal("1000"),
    )
    repeated = lifecycle.create_order(
        intent(),
        order_id="order-1",
        created_at=100,
        reservation_amount=Decimal("1000"),
        available_balance=Decimal("1000"),
    )
    assert repeated == first

    with pytest.raises(ValueError, match="already created"):
        lifecycle.create_order(
            intent(),
            order_id="order-2",
            created_at=100,
            reservation_amount=Decimal("1000"),
            available_balance=Decimal("1000"),
        )


def test_quote_reservations_are_shared_across_instruments() -> None:
    book = ReservationBook()
    book.reserve(
        order_id="btc-order",
        instrument=BTC,
        asset="USDT",
        amount=Decimal("600"),
        available_balance=Decimal("1000"),
    )

    with pytest.raises(ValueError, match="available balance"):
        book.reserve(
            order_id="eth-order",
            instrument=ETH,
            asset="USDT",
            amount=Decimal("500"),
            available_balance=Decimal("1000"),
        )
