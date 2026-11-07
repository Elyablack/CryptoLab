from decimal import Decimal

import pytest

from cryptolab.analytics.indicators import (
    calculate_atr,
    calculate_ema,
    calculate_relative_volume,
    calculate_rsi,
    calculate_sma,
)
from cryptolab.market_data.models import Candle


def make_candle(
    index: int,
    *,
    high: str,
    low: str,
    close: str,
    volume: str = "10",
) -> Candle:
    open_time = index * 60_000
    close_value = Decimal(close)
    volume_value = Decimal(volume)
    return Candle(
        open_time=open_time,
        open=close_value,
        high=Decimal(high),
        low=Decimal(low),
        close=close_value,
        volume=volume_value,
        close_time=open_time + 59_999,
        quote_volume=volume_value * close_value,
        number_of_trades=10,
        taker_buy_base_volume=volume_value / 2,
        taker_buy_quote_volume=volume_value * close_value / 2,
    )


def assert_decimal_close(actual: Decimal | None, expected: Decimal) -> None:
    assert actual is not None
    assert abs(actual - expected) < Decimal("1E-25")


def test_calculate_sma_uses_sliding_window() -> None:
    values = [Decimal(value) for value in (1, 2, 3, 4, 5)]

    assert calculate_sma(values, period=3) == [
        None,
        None,
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
    ]


def test_calculate_ema_uses_sma_seed_and_standard_multiplier() -> None:
    values = [Decimal(value) for value in (1, 2, 3, 6, 10)]

    assert calculate_ema(values, period=3) == [
        None,
        None,
        Decimal("2"),
        Decimal("4.0"),
        Decimal("7.00"),
    ]


def test_calculate_ema_returns_none_until_seed_is_available() -> None:
    assert calculate_ema([Decimal("1"), Decimal("2")], period=3) == [None, None]


def test_calculate_rsi_matches_wilder_reference_example() -> None:
    values = [Decimal(value) for value in (10, 11, 12, 11, 13, 12, 14)]

    result = calculate_rsi(values, period=3)

    assert result[:3] == [None, None, None]
    assert_decimal_close(result[3], Decimal(200) / 3)
    assert_decimal_close(result[4], Decimal(250) / 3)
    assert_decimal_close(result[5], Decimal(2000) / 33)
    assert_decimal_close(result[6], Decimal(235) / 3)


def test_calculate_rsi_handles_zero_gain_and_zero_loss_cases() -> None:
    assert calculate_rsi(
        [Decimal(value) for value in (10, 11, 12, 13)],
        period=3,
    )[-1] == Decimal("100")
    assert calculate_rsi(
        [Decimal(value) for value in (13, 12, 11, 10)],
        period=3,
    )[-1] == Decimal("0")
    assert calculate_rsi([Decimal("10")] * 4, period=3)[-1] == Decimal("50")


def test_calculate_atr_matches_wilder_true_range_reference() -> None:
    candles = [
        make_candle(0, high="10", low="8", close="9"),
        make_candle(1, high="12", low="9", close="11"),
        make_candle(2, high="13", low="10", close="12"),
        make_candle(3, high="15", low="11", close="14"),
        make_candle(4, high="12", low="8", close="10"),
    ]

    result = calculate_atr(candles, period=3)

    assert result[:2] == [None, None]
    assert_decimal_close(result[2], Decimal(8) / 3)
    assert_decimal_close(result[3], Decimal(28) / 9)
    assert_decimal_close(result[4], Decimal(110) / 27)


def test_relative_volume_uses_only_previous_candles() -> None:
    volumes = ["10", "20", "30", "80", "40"]
    candles = [
        make_candle(index, high="11", low="9", close="10", volume=volume)
        for index, volume in enumerate(volumes)
    ]

    result = calculate_relative_volume(candles, period=3)

    assert result[:3] == [None, None, None]
    assert result[3] == Decimal("4")
    assert_decimal_close(result[4], Decimal(12) / 13)


def test_relative_volume_returns_none_for_zero_previous_average() -> None:
    candles = [
        make_candle(index, high="11", low="9", close="10", volume=volume)
        for index, volume in enumerate(("0", "0", "0", "1"))
    ]

    assert calculate_relative_volume(candles, period=3)[-1] is None


@pytest.mark.parametrize(
    "calculator,values",
    [
        (calculate_sma, [Decimal("1")]),
        (calculate_ema, [Decimal("1")]),
        (calculate_rsi, [Decimal("1")]),
        (calculate_atr, []),
        (calculate_relative_volume, []),
    ],
)
def test_indicators_reject_non_positive_period(
    calculator: object,
    values: object,
) -> None:
    with pytest.raises(ValueError, match="Period must be positive"):
        calculator(values, period=0)  # type: ignore[operator]

