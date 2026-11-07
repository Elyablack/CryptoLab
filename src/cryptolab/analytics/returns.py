"""Financial return calculations."""

from collections.abc import Sequence
from decimal import Decimal

from cryptolab.market_data.models import Candle


def calculate_simple_returns(
    candles: Sequence[Candle],
) -> list[Decimal]:
    """Calculate close-to-close simple returns."""

    if len(candles) < 2:
        return []

    returns = []

    for previous, current in zip(candles, candles[1:]):
        if previous.close <= 0 or current.close <= 0:
            raise ValueError("Close prices must be positive")

        if current.open_time <= previous.open_time:
            raise ValueError("Candles must be ordered by open_time")

        value = (current.close - previous.close) / previous.close

        returns.append(value)

    return returns


def calculate_log_returns(
    candles: Sequence[Candle],
) -> list[Decimal]:
    """Calculate close-to-close logarithmic returns."""

    if len(candles) < 2:
        return []

    returns = []

    for previous, current in zip(candles, candles[1:]):
        if previous.close <= 0 or current.close <= 0:
            raise ValueError("Close prices must be positive")

        if current.open_time <= previous.open_time:
            raise ValueError("Candles must be ordered by open_time")

        value = (current.close / previous.close).ln()

        returns.append(value)

    return returns


def calculate_volatility(
    returns: Sequence[Decimal],
) -> Decimal | None:
    """Calculate sample standard deviation of returns."""

    if len(returns) < 2:
        return None

    count = len(returns)

    mean = sum(returns, Decimal("0")) / count

    squared_deviations = sum(
        ((value - mean) ** 2 for value in returns),
        Decimal("0"),
    )

    variance = squared_deviations / (count - 1)

    return variance.sqrt()


def calculate_max_drawdown(
    candles: Sequence[Candle],
) -> Decimal | None:
    """Calculate maximum close-to-close price drawdown."""

    if not candles:
        return None

    peak = candles[0].close

    if peak <= 0:
        raise ValueError("Close prices must be positive")

    max_drawdown = Decimal("0")

    previous_open_time = candles[0].open_time

    for candle in candles[1:]:
        if candle.close <= 0:
            raise ValueError("Close prices must be positive")

        if candle.open_time <= previous_open_time:
            raise ValueError("Candles must be ordered by open_time")

        previous_open_time = candle.open_time

        if candle.close > peak:
            peak = candle.close

        drawdown = (candle.close - peak) / peak

        if drawdown < max_drawdown:
            max_drawdown = drawdown

    return max_drawdown
