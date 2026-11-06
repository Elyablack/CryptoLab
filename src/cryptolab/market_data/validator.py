"""Validation rules for parsed market data."""

from cryptolab.market_data.models import Candle


def validate_candle(candle: Candle) -> None:
    """Validate the invariants of a single candle."""
    prices = {
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
    }
    for field_name, value in prices.items():
        if value <= 0:
            raise ValueError(f"{field_name} price must be strictly positive")

    for field_name, value in (
        ("open", candle.open),
        ("close", candle.close),
        ("low", candle.low),
    ):
        if candle.high < value:
            raise ValueError(f"high price cannot be less than {field_name} price")

    for field_name, value in (
        ("open", candle.open),
        ("close", candle.close),
        ("high", candle.high),
    ):
        if candle.low > value:
            raise ValueError(f"low price cannot be greater than {field_name} price")

    volumes = {
        "volume": candle.volume,
        "quote_volume": candle.quote_volume,
        "taker_buy_base_volume": candle.taker_buy_base_volume,
        "taker_buy_quote_volume": candle.taker_buy_quote_volume,
    }
    for field_name, value in volumes.items():
        if value < 0:
            raise ValueError(f"{field_name} must be non-negative")

    if candle.taker_buy_base_volume > candle.volume:
        raise ValueError("taker_buy_base_volume cannot exceed volume")

    if candle.taker_buy_quote_volume > candle.quote_volume:
        raise ValueError("taker_buy_quote_volume cannot exceed quote_volume")

    if candle.number_of_trades < 0:
        raise ValueError("number_of_trades must be non-negative")

    if candle.open_time < 0:
        raise ValueError("open_time must be non-negative")

    if candle.close_time <= candle.open_time:
        raise ValueError("close_time must be greater than open_time")


def validate_candles(candles: list[Candle]) -> None:
    """Validate candles and require strictly increasing open times."""
    previous_open_time: int | None = None

    for index, candle in enumerate(candles):
        try:
            validate_candle(candle)
        except ValueError as error:
            raise ValueError(f"Invalid candle at index {index}: {error}") from error

        if previous_open_time is not None and candle.open_time <= previous_open_time:
            raise ValueError(
                f"Invalid candle at index {index}: open_time must be strictly "
                "greater than the previous candle open_time"
            )

        previous_open_time = candle.open_time

