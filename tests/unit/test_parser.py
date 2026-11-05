from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from cryptolab.market_data.models import Candle
from cryptolab.market_data.parser import parse_kline, parse_klines


RAW_KLINE = [
    1_499_040_000_000,
    "0.01634790",
    "0.80000000",
    "0.01575800",
    "0.01577100",
    "148976.11427815",
    1_499_643_999_999,
    "2434.19055334",
    308,
    "1756.87402397",
    "28.46694368",
    "0",
]


def test_parse_kline_returns_typed_candle() -> None:
    candle = parse_kline(RAW_KLINE)

    assert candle == Candle(
        open_time=1_499_040_000_000,
        open=Decimal("0.01634790"),
        high=Decimal("0.80000000"),
        low=Decimal("0.01575800"),
        close=Decimal("0.01577100"),
        volume=Decimal("148976.11427815"),
        close_time=1_499_643_999_999,
        quote_volume=Decimal("2434.19055334"),
        number_of_trades=308,
        taker_buy_base_volume=Decimal("1756.87402397"),
        taker_buy_quote_volume=Decimal("28.46694368"),
    )


def test_parse_klines_returns_list_of_candles() -> None:
    result = parse_klines([RAW_KLINE, RAW_KLINE.copy()])

    assert len(result) == 2
    assert all(isinstance(candle, Candle) for candle in result)


def test_candle_is_immutable() -> None:
    candle = parse_kline(RAW_KLINE)

    with pytest.raises(FrozenInstanceError):
        candle.close = Decimal("1.0")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not a list", "Kline must be a list"),
        (RAW_KLINE[:-1], "must contain exactly 12 fields"),
        (["invalid", *RAW_KLINE[1:]], "'open_time' must be an int"),
        ([RAW_KLINE[0], 1.5, *RAW_KLINE[2:]], "'open' must be a decimal string"),
        ([RAW_KLINE[0], "invalid", *RAW_KLINE[2:]], "'open' is not a valid decimal"),
    ],
)
def test_parse_kline_rejects_invalid_format(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_kline(raw)


def test_parse_klines_identifies_invalid_item_index() -> None:
    with pytest.raises(ValueError, match="Invalid kline at index 1"):
        parse_klines([RAW_KLINE, ["invalid"]])
