from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from cryptolab.execution import (
    Instrument,
    IntentSource,
    OrderIntent,
    OrderSide,
)


def make_intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        source_event_id="decision-1",
        source=IntentSource.STRATEGY,
        instrument=Instrument(
            exchange="bybit",
            market_type="spot",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        ),
        side=OrderSide.BUY,
        quantity=Decimal("0.25"),
        created_at=2_000,
        earliest_execution_at=2_000,
        signal_available_time=2_000,
        source_candle_open_time=1_000,
        explanation="approved research allocation",
    )


def test_order_intent_is_immutable_and_explicitly_typed() -> None:
    intent = make_intent()

    assert intent.instrument.base_asset == "BTC"
    assert intent.side is OrderSide.BUY
    assert intent.source is IntentSource.STRATEGY

    with pytest.raises(FrozenInstanceError):
        intent.quantity = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"quantity": Decimal("0")}, "quantity"),
        ({"quantity": Decimal("NaN")}, "quantity"),
        ({"signal_available_time": None}, "signal_available_time"),
        ({"created_at": 1_999}, "before signal availability"),
        ({"earliest_execution_at": 1_999}, "before signal availability"),
        ({"source_candle_open_time": -1}, "timestamps"),
        ({"intent_id": ""}, "intent_id"),
    ],
)
def test_order_intent_rejects_invalid_fields(
    change: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(make_intent(), **change)


def test_instrument_rejects_ambiguous_assets() -> None:
    with pytest.raises(ValueError, match="must differ"):
        Instrument(
            exchange="bybit",
            market_type="spot",
            symbol="USDTUSDT",
            base_asset="USDT",
            quote_asset="USDT",
        )
