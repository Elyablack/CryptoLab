from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.analytics.returns import (
    calculate_log_returns,
    calculate_max_drawdown,
    calculate_simple_returns,
    calculate_volatility,
)
from cryptolab.market_data.models import Candle


def make_candles(closes: list[str]) -> list[Candle]:
    candles = []
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        open_time = index * 60_000
        candles.append(
            Candle(
                open_time=open_time,
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("10"),
                close_time=open_time + 59_999,
                quote_volume=Decimal("1000"),
                number_of_trades=10,
                taker_buy_base_volume=Decimal("5"),
                taker_buy_quote_volume=Decimal("500"),
            )
        )
    return candles


def test_calculate_simple_returns_uses_close_to_close_change() -> None:
    candles = make_candles(["100", "110", "99"])

    assert calculate_simple_returns(candles) == [
        Decimal("0.1"),
        Decimal("-0.1"),
    ]


def test_calculate_simple_returns_preserves_decimal_precision() -> None:
    candles = make_candles(["1", "1.000000000000000000000000001"])

    assert calculate_simple_returns(candles) == [Decimal("1E-27")]


def test_calculate_log_returns_matches_reference_constants() -> None:
    candles = make_candles(["100", "110", "99"])

    result = calculate_log_returns(candles)

    assert result[0] == pytest.approx(
        Decimal("0.09531017980432486004395212328"),
        abs=Decimal("1E-27"),
    )
    assert result[1] == pytest.approx(
        Decimal("-0.1053605156578263012275009808"),
        abs=Decimal("1E-27"),
    )


@pytest.mark.parametrize(
    "calculator",
    [calculate_simple_returns, calculate_log_returns],
)
def test_return_calculators_reject_duplicate_or_reversed_time(
    calculator: object,
) -> None:
    candles = make_candles(["100", "101"])
    candles[1] = replace(candles[1], open_time=candles[0].open_time)

    with pytest.raises(ValueError, match="ordered by open_time"):
        calculator(candles)  # type: ignore[operator]


@pytest.mark.parametrize(
    "calculator",
    [calculate_simple_returns, calculate_log_returns],
)
def test_return_calculators_reject_non_positive_close(
    calculator: object,
) -> None:
    candles = make_candles(["100", "0"])

    with pytest.raises(ValueError, match="Close prices must be positive"):
        calculator(candles)  # type: ignore[operator]


def test_return_calculators_handle_insufficient_data() -> None:
    candle = make_candles(["100"])

    assert calculate_simple_returns([]) == []
    assert calculate_simple_returns(candle) == []
    assert calculate_log_returns([]) == []
    assert calculate_log_returns(candle) == []


def test_calculate_volatility_uses_sample_standard_deviation() -> None:
    returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]

    assert calculate_volatility(returns) == Decimal("0.01")


def test_calculate_volatility_requires_two_returns() -> None:
    assert calculate_volatility([]) is None
    assert calculate_volatility([Decimal("0.01")]) is None


def test_calculate_max_drawdown_returns_non_positive_peak_loss() -> None:
    candles = make_candles(["100", "120", "90", "150", "105"])

    assert calculate_max_drawdown(candles) == Decimal("-0.3")


def test_calculate_max_drawdown_handles_empty_and_rising_prices() -> None:
    assert calculate_max_drawdown([]) is None
    assert calculate_max_drawdown(make_candles(["100", "110", "120"])) == 0

