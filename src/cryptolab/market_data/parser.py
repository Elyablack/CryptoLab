"""Convert public exchange kline responses into domain models."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.market_data.models import Candle


EXPECTED_KLINE_FIELD_COUNT = 12
EXPECTED_BYBIT_KLINE_FIELD_COUNT = 7


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


def parse_bybit_klines(
    raw: object,
    symbol: str,
    interval: str,
) -> list[Candle]:
    """Parse a Bybit V5 Spot kline response in chronological order."""
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"Bybit response must be an object, got {type(raw).__name__}"
        )

    ret_code = raw.get("retCode")
    if isinstance(ret_code, bool) or not isinstance(ret_code, int):
        raise ValueError("Bybit response field 'retCode' must be an int")
    if ret_code != 0:
        raise ValueError(
            f"Bybit API error {ret_code}: {raw.get('retMsg', 'unknown error')}"
        )

    result = raw.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Bybit response field 'result' must be an object")
    if result.get("category") != "spot":
        raise ValueError("Bybit response category must be 'spot'")
    if result.get("symbol") != symbol:
        raise ValueError(
            "Bybit response symbol does not match the requested symbol"
        )

    raw_klines = result.get("list")
    if not isinstance(raw_klines, list):
        raise ValueError("Bybit response field 'list' must be a list")

    interval_milliseconds = get_interval_milliseconds(interval)
    candles: list[Candle] = []
    for index, item in enumerate(raw_klines):
        try:
            candles.append(
                _parse_bybit_kline(item, interval_milliseconds)
            )
        except ValueError as error:
            raise ValueError(
                f"Invalid Bybit kline at index {index}: {error}"
            ) from error

    return sorted(candles, key=lambda candle: candle.open_time)


def _parse_bybit_kline(
    raw: object,
    interval_milliseconds: int,
) -> Candle:
    if not isinstance(raw, list):
        raise ValueError(
            f"Bybit kline must be a list, got {type(raw).__name__}"
        )
    if len(raw) != EXPECTED_BYBIT_KLINE_FIELD_COUNT:
        raise ValueError(
            "Bybit kline must contain exactly "
            f"{EXPECTED_BYBIT_KLINE_FIELD_COUNT} fields, got {len(raw)}"
        )

    open_time = _parse_integer_string(raw[0], "start_time")
    return Candle(
        open_time=open_time,
        open=_parse_decimal(raw[1], "open"),
        high=_parse_decimal(raw[2], "high"),
        low=_parse_decimal(raw[3], "low"),
        close=_parse_decimal(raw[4], "close"),
        volume=_parse_decimal(raw[5], "volume"),
        close_time=open_time + interval_milliseconds - 1,
        quote_volume=_parse_decimal(raw[6], "turnover"),
        number_of_trades=None,
        taker_buy_base_volume=None,
        taker_buy_quote_volume=None,
    )


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


def _parse_integer_string(value: object, field_name: str) -> int:
    if not isinstance(value, str):
        raise ValueError(
            f"Kline field '{field_name}' must be an integer string, "
            f"got {type(value).__name__}"
        )
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(
            f"Kline field '{field_name}' is not a valid integer: {value!r}"
        ) from error

