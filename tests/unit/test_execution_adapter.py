from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.execution import (
    ExecutionAdapterSnapshot,
    ExecutionEventType,
    FakeVenueExecutionAdapter,
    Instrument,
    IntentSource,
    LifecycleSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    SimulatedExecutionAdapter,
)


INSTRUMENT = Instrument(
    exchange="paper",
    market_type="spot",
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="paper-intent-1",
        source_event_id="strategy-event-1",
        source=IntentSource.STRATEGY,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        created_at=100,
        earliest_execution_at=100,
        signal_available_time=100,
        source_candle_open_time=0,
        explanation="paper adapter test",
    )


def submitted_adapter() -> FakeVenueExecutionAdapter:
    adapter = FakeVenueExecutionAdapter()
    adapter.submit(
        intent(),
        order_id="paper-order-1",
        created_at=100,
        reservation_amount=Decimal("1001"),
        available_balance=Decimal("1001"),
    )
    return adapter


def apply_partial(
    adapter: FakeVenueExecutionAdapter,
    *,
    event_id: str = "venue-event-1",
) -> tuple[object, bool]:
    return adapter.process_fill_event(
        event_id=event_id,
        order_id="paper-order-1",
        quantity=Decimal("4"),
        price=Decimal("100"),
        quote_notional=Decimal("400"),
        fee=Decimal("0.4"),
        fee_asset="USDT",
        executed_at=101,
    )


def test_fake_adapter_handles_partial_and_duplicate_venue_events() -> None:
    adapter = submitted_adapter()

    order, applied = apply_partial(adapter)
    repeated, applied_again = apply_partial(adapter)

    assert applied
    assert not applied_again
    assert repeated == order
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("4")
    assert len(adapter.fills) == 1
    assert [event.event_type for event in adapter.events] == [
        ExecutionEventType.ORDER_ACCEPTED,
        ExecutionEventType.ORDER_PARTIALLY_FILLED,
    ]

    with pytest.raises(ValueError, match="Conflicting duplicate"):
        adapter.process_fill_event(
            event_id="venue-event-1",
            order_id="paper-order-1",
            quantity=Decimal("5"),
            price=Decimal("100"),
            quote_notional=Decimal("500"),
            fee=Decimal("0.5"),
            fee_asset="USDT",
            executed_at=101,
        )


def test_snapshot_restart_recovery_continues_order_safely() -> None:
    adapter = submitted_adapter()
    apply_partial(adapter)
    snapshot = adapter.snapshot()

    restored = FakeVenueExecutionAdapter.from_snapshot(snapshot)
    assert restored.snapshot() == snapshot
    _, duplicate_applied = apply_partial(restored)
    assert not duplicate_applied

    order, applied = restored.process_fill_event(
        event_id="venue-event-2",
        order_id="paper-order-1",
        quantity=Decimal("6"),
        price=Decimal("100"),
        quote_notional=Decimal("600"),
        fee=Decimal("0.6"),
        fee_asset="USDT",
        executed_at=102,
    )

    assert applied
    assert order.status is OrderStatus.FILLED
    assert restored.reservations[0].remaining_amount == 0
    assert [event.sequence for event in restored.events] == [1, 2, 3]
    assert restored.events[-1].event_type is ExecutionEventType.ORDER_FILLED


def test_reconciliation_detects_missing_and_mismatched_state() -> None:
    local = submitted_adapter()
    apply_partial(local)
    identical = local.snapshot()
    assert local.reconcile(identical).is_consistent

    venue_lifecycle = replace(
        identical.lifecycle,
        fills=(),
        orders=(replace(
            identical.lifecycle.orders[0],
            filled_quantity=Decimal("0"),
            status=OrderStatus.NEW,
        ),),
    )
    venue = ExecutionAdapterSnapshot(
        lifecycle=venue_lifecycle,
        intents=identical.intents,
        events=identical.events[:1],
        next_sequence=2,
    )
    report = local.reconcile(venue)

    assert not report.is_consistent
    assert report.mismatched_order_ids == ("paper-order-1",)
    assert report.missing_remote_fill_ids == ("venue-fill:venue-event-1",)


def test_recovery_rejects_corrupt_snapshot() -> None:
    adapter = submitted_adapter()
    snapshot = adapter.snapshot()
    corrupt = ExecutionAdapterSnapshot(
        lifecycle=LifecycleSnapshot(
            orders=snapshot.lifecycle.orders,
            fills=snapshot.lifecycle.fills,
            reservations=(),
        ),
        intents=snapshot.intents,
        events=snapshot.events,
        next_sequence=snapshot.next_sequence,
    )

    with pytest.raises(ValueError, match="no balance reservation"):
        SimulatedExecutionAdapter.from_snapshot(corrupt)


def test_cancelled_paper_order_cannot_receive_late_fill() -> None:
    adapter = submitted_adapter()
    cancelled = adapter.cancel(
        "paper-order-1", at=101, reason="paper user cancelled"
    )
    assert cancelled.status is OrderStatus.CANCELLED
    assert adapter.reservations[0].remaining_amount == 0

    with pytest.raises(ValueError, match="terminal"):
        apply_partial(adapter)


def test_adapter_has_no_network_or_credentials_configuration() -> None:
    adapter = FakeVenueExecutionAdapter()
    assert adapter.orders == ()
    assert adapter.fills == ()
    assert adapter.events == ()
