"""Validation rules for parsed market data."""

import time
from collections.abc import Sequence

from cryptolab.market_data.intervals import get_interval_milliseconds
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
    }
    if candle.taker_buy_base_volume is not None:
        volumes["taker_buy_base_volume"] = candle.taker_buy_base_volume
    if candle.taker_buy_quote_volume is not None:
        volumes["taker_buy_quote_volume"] = candle.taker_buy_quote_volume
    for field_name, value in volumes.items():
        if value < 0:
            raise ValueError(f"{field_name} must be non-negative")

    if (
        candle.taker_buy_base_volume is not None
        and candle.taker_buy_base_volume > candle.volume
    ):
        raise ValueError("taker_buy_base_volume cannot exceed volume")

    if (
        candle.taker_buy_quote_volume is not None
        and candle.taker_buy_quote_volume > candle.quote_volume
    ):
        raise ValueError("taker_buy_quote_volume cannot exceed quote_volume")

    if candle.number_of_trades is not None and candle.number_of_trades < 0:
        raise ValueError("number_of_trades must be non-negative")

    if candle.open_time < 0:
        raise ValueError("open_time must be non-negative")

    if candle.close_time <= candle.open_time:
        raise ValueError("close_time must be greater than open_time")


def validate_candles(candles: Sequence[Candle]) -> None:
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


def validate_historical_candles(
    candles: Sequence[Candle],
    interval: str,
    *,
    as_of_time: int | None = None,
) -> None:
    """Validate a complete sequence available at ``as_of_time``.

    A candle becomes available one millisecond after its inclusive
    ``close_time``. When ``as_of_time`` is omitted, the current system time is
    used for backward compatibility.
    """
    interval_milliseconds = get_interval_milliseconds(interval)
    validate_candles(candles)

    evaluation_time = (
        int(time.time() * 1000)
        if as_of_time is None
        else as_of_time
    )
    previous_open_time: int | None = None

    for index, candle in enumerate(candles):
        if candle.open_time % interval_milliseconds != 0:
            raise ValueError(
                f"Candle at index {index}: open_time must be aligned "
                f"to interval {interval}"
            )

        if (
            previous_open_time is not None
            and candle.open_time - previous_open_time
            != interval_milliseconds
        ):
            raise ValueError(
                f"Candle at index {index}: candles must be consecutive "
                f"for interval {interval}"
            )

        expected_close_time = candle.open_time + interval_milliseconds - 1
        if candle.close_time != expected_close_time:
            raise ValueError(
                f"Candle at index {index}: close_time does not match "
                f"interval {interval}"
            )

        if candle.close_time >= evaluation_time:
            raise ValueError(f"Candle at index {index} is not closed")

        previous_open_time = candle.open_time

