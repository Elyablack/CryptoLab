from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.market_data.models import Candle
from cryptolab.market_data.validator import validate_candle, validate_candles


VALID_CANDLE = Candle(
    open_time=1_000,
    open=Decimal("100"),
    high=Decimal("120"),
    low=Decimal("90"),
    close=Decimal("110"),
    volume=Decimal("10"),
    close_time=1_999,
    quote_volume=Decimal("1000"),
    number_of_trades=25,
    taker_buy_base_volume=Decimal("6"),
    taker_buy_quote_volume=Decimal("600"),
)


def test_validate_candle_accepts_valid_candle() -> None:
    assert validate_candle(VALID_CANDLE) is None


@pytest.mark.parametrize("field_name", ["open", "high", "low", "close"])
def test_validate_candle_rejects_non_positive_price(field_name: str) -> None:
    candle = replace(VALID_CANDLE, **{field_name: Decimal("0")})

    with pytest.raises(
        ValueError,
        match=f"{field_name} price must be strictly positive",
    ):
        validate_candle(candle)


def test_validate_candle_rejects_high_below_other_price() -> None:
    candle = replace(VALID_CANDLE, high=Decimal("99"))

    with pytest.raises(ValueError, match="high price cannot be less than open price"):
        validate_candle(candle)


def test_validate_candle_rejects_low_above_other_price() -> None:
    candle = replace(VALID_CANDLE, low=Decimal("101"))

    with pytest.raises(ValueError, match="low price cannot be greater than open price"):
        validate_candle(candle)


@pytest.mark.parametrize(
    "field_name",
    [
        "volume",
        "quote_volume",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    ],
)
def test_validate_candle_rejects_negative_volume(field_name: str) -> None:
    candle = replace(VALID_CANDLE, **{field_name: Decimal("-1")})

    with pytest.raises(ValueError, match=f"{field_name} must be non-negative"):
        validate_candle(candle)


def test_validate_candle_rejects_taker_buy_base_volume_above_total() -> None:
    candle = replace(VALID_CANDLE, taker_buy_base_volume=Decimal("11"))

    with pytest.raises(ValueError, match="taker_buy_base_volume cannot exceed volume"):
        validate_candle(candle)


def test_validate_candle_rejects_taker_buy_quote_volume_above_total() -> None:
    candle = replace(VALID_CANDLE, taker_buy_quote_volume=Decimal("1001"))

    with pytest.raises(
        ValueError,
        match="taker_buy_quote_volume cannot exceed quote_volume",
    ):
        validate_candle(candle)


def test_validate_candle_rejects_negative_number_of_trades() -> None:
    candle = replace(VALID_CANDLE, number_of_trades=-1)

    with pytest.raises(ValueError, match="number_of_trades must be non-negative"):
        validate_candle(candle)


def test_validate_candle_rejects_negative_open_time() -> None:
    candle = replace(VALID_CANDLE, open_time=-1)

    with pytest.raises(ValueError, match="open_time must be non-negative"):
        validate_candle(candle)


def test_validate_candle_rejects_close_time_not_after_open_time() -> None:
    candle = replace(VALID_CANDLE, close_time=VALID_CANDLE.open_time)

    with pytest.raises(ValueError, match="close_time must be greater than open_time"):
        validate_candle(candle)


def test_validate_candles_accepts_empty_list() -> None:
    assert validate_candles([]) is None


def test_validate_candles_accepts_increasing_open_times() -> None:
    second_candle = replace(VALID_CANDLE, open_time=2_000, close_time=2_999)

    assert validate_candles([VALID_CANDLE, second_candle]) is None


def test_validate_candles_reports_invalid_candle_index() -> None:
    invalid_candle = replace(
        VALID_CANDLE,
        open_time=2_000,
        close_time=2_999,
        volume=Decimal("-1"),
    )

    with pytest.raises(ValueError, match="Invalid candle at index 1: volume"):
        validate_candles([VALID_CANDLE, invalid_candle])


@pytest.mark.parametrize("open_time", [1_000, 999])
def test_validate_candles_rejects_non_increasing_open_time(open_time: int) -> None:
    second_candle = replace(VALID_CANDLE, open_time=open_time, close_time=2_999)

    with pytest.raises(
        ValueError,
        match="Invalid candle at index 1: open_time must be strictly greater",
    ):
        validate_candles([VALID_CANDLE, second_candle])
