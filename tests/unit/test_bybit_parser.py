from decimal import Decimal

import pytest

from cryptolab.market_data.models import Candle
from cryptolab.market_data.parser import parse_bybit_klines
from cryptolab.market_data.validator import validate_candles


def response(klines: list[object]) -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "spot",
            "symbol": "BTCUSDT",
            "list": klines,
        },
        "retExtInfo": {},
        "time": 1_704_153_600_000,
    }


def raw_kline(open_time: int, close: str) -> list[str]:
    return [
        str(open_time),
        "100.10",
        "120.20",
        "90.30",
        close,
        "10.50",
        "1100.60",
    ]


def test_parse_bybit_klines_normalizes_reverse_response_without_fabrication() -> None:
    result = parse_bybit_klines(
        response([
            raw_kline(3_600_000, "111.40"),
            raw_kline(0, "110.40"),
        ]),
        "BTCUSDT",
        "1h",
    )

    assert result == [
        Candle(
            open_time=0,
            open=Decimal("100.10"),
            high=Decimal("120.20"),
            low=Decimal("90.30"),
            close=Decimal("110.40"),
            volume=Decimal("10.50"),
            close_time=3_599_999,
            quote_volume=Decimal("1100.60"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ),
        Candle(
            open_time=3_600_000,
            open=Decimal("100.10"),
            high=Decimal("120.20"),
            low=Decimal("90.30"),
            close=Decimal("111.40"),
            volume=Decimal("10.50"),
            close_time=7_199_999,
            quote_volume=Decimal("1100.60"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ),
    ]
    validate_candles(result)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"retCode": 10006, "retMsg": "Too many visits!"}, "API error 10006"),
        (
            {
                "retCode": 0,
                "result": {"category": "linear", "symbol": "BTCUSDT", "list": []},
            },
            "category must be 'spot'",
        ),
        (
            {
                "retCode": 0,
                "result": {"category": "spot", "symbol": "ETHUSDT", "list": []},
            },
            "symbol does not match",
        ),
        (response([["0"]]), "exactly 7 fields"),
    ],
)
def test_parse_bybit_klines_rejects_invalid_response(
    payload: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_bybit_klines(payload, "BTCUSDT", "1h")
