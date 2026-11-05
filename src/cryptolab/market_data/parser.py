"""Convert raw Binance Spot kline data into domain models."""

from decimal import Decimal, InvalidOperation

from cryptolab.market_data.models import Candle


EXPECTED_KLINE_FIELD_COUNT = 12


def parse_kline(raw: object) -> Candle:
    """Parse one Binance Spot kline array into a Candle."""
    if not isinstance(raw, list):
        raise ValueError(f"Kline must be a list, got {type(raw).__name__}")

    if len(raw) != EXPECTED_KLINE_FIELD_COUNT:
        raise ValueError(
            "Kline must contain exactly "
            f"{EXPECTED_KLINE_FIELD_COUNT} fields, got {len(raw)}"
        )

    return Candle(
        open_time=_parse_int(raw[0], "open_time"),
        open=_parse_decimal(raw[1], "open"),
        high=_parse_decimal(raw[2], "high"),
        low=_parse_decimal(raw[3], "low"),
        close=_parse_decimal(raw[4], "close"),
        volume=_parse_decimal(raw[5], "volume"),
        close_time=_parse_int(raw[6], "close_time"),
        quote_volume=_parse_decimal(raw[7], "quote_volume"),
        number_of_trades=_parse_int(raw[8], "number_of_trades"),
        taker_buy_base_volume=_parse_decimal(
            raw[9],
            "taker_buy_base_volume",
        ),
        taker_buy_quote_volume=_parse_decimal(
            raw[10],
            "taker_buy_quote_volume",
        ),
    )


def parse_klines(raw: object) -> list[Candle]:
    """Parse a Binance Spot klines response into Candle objects."""
    if not isinstance(raw, list):
        raise ValueError(f"Klines response must be a list, got {type(raw).__name__}")

    candles: list[Candle] = []
    for index, item in enumerate(raw):
        try:
            candles.append(parse_kline(item))
        except ValueError as error:
            raise ValueError(f"Invalid kline at index {index}: {error}") from error

    return candles


def _parse_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"Kline field '{field_name}' must be an int, "
            f"got {type(value).__name__}"
        )
    return value


def _parse_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(
            f"Kline field '{field_name}' must be a decimal string, "
            f"got {type(value).__name__}"
        )

    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(
            f"Kline field '{field_name}' is not a valid decimal: {value!r}"
        ) from error

    if not parsed.is_finite():
        raise ValueError(
            f"Kline field '{field_name}' must be finite, got {value!r}"
        )

    return parsed

