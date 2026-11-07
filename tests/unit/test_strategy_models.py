from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from cryptolab.strategies.models import Signal, StrategyDecision


def test_strategy_decision_is_immutable_including_diagnostics() -> None:
    decision = StrategyDecision(
        strategy_name="example",
        symbol="BTCUSDT",
        interval="1h",
        signal=Signal.BUY,
        candle_open_time=0,
        signal_available_time=3_600_000,
        reference_price=Decimal("100"),
        explanation="Research signal",
        diagnostics={"rsi14": Decimal("40")},
    )

    with pytest.raises(FrozenInstanceError):
        decision.signal = Signal.HOLD  # type: ignore[misc]

    with pytest.raises(TypeError):
        decision.diagnostics["rsi14"] = Decimal("50")  # type: ignore[index]


def test_signal_values_have_explicit_spot_semantics() -> None:
    assert Signal.BUY.value == "BUY"
    assert Signal.SELL.value == "SELL"
    assert Signal.HOLD.value == "HOLD"
    assert "not mean opening a short" in Signal.__doc__


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_name": ""},
        {"symbol": "   "},
        {"signal_available_time": 0},
        {"candle_open_time": "0"},
        {"signal_available_time": True},
        {"reference_price": Decimal("0")},
        {"reference_price": 100},
        {"reference_price": Decimal("Infinity")},
        {"explanation": ""},
        {"explanation": "   "},
        {"diagnostics": {1: "invalid key"}},
        {"diagnostics": {"nested": []}},
    ],
)
def test_strategy_decision_rejects_invalid_fields(
    changes: dict[str, object],
) -> None:
    arguments = {
        "strategy_name": "example",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "signal": Signal.HOLD,
        "candle_open_time": 0,
        "signal_available_time": 3_600_000,
        "reference_price": Decimal("100"),
        "explanation": "No signal",
    }
    arguments.update(changes)

    with pytest.raises(ValueError):
        StrategyDecision(**arguments)  # type: ignore[arg-type]

