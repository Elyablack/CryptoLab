"""Technical indicators for historical market data."""

from collections.abc import Sequence
from decimal import Decimal

from cryptolab.market_data.models import Candle


def calculate_sma(
    values: Sequence[Decimal],
    period: int,
) -> list[Decimal | None]:
    """Calculate simple moving average."""

    if period <= 0:
        raise ValueError("Period must be positive")

    results: list[Decimal | None] = []
    window_sum = Decimal("0")

    for index, value in enumerate(values):
        window_sum += value

        if index >= period:
            window_sum -= values[index - period]

        if index >= period - 1:
            results.append(window_sum / period)
        else:
            results.append(None)

    return results


def calculate_ema(
    values: Sequence[Decimal],
    period: int,
) -> list[Decimal | None]:
    """Calculate EMA initialized with an SMA seed."""

    if period <= 0:
        raise ValueError("Period must be positive")

    if len(values) < period:
        return [None] * len(values)

    results: list[Decimal | None] = [None] * (period - 1)

    multiplier = Decimal("2") / Decimal(period + 1)

    previous_ema = (
        sum(values[:period], Decimal("0")) / period
    )

    results.append(previous_ema)

    for value in values[period:]:
        previous_ema = (
            value * multiplier
            + previous_ema * (Decimal("1") - multiplier)
        )

        results.append(previous_ema)

    return results


def calculate_rsi(
    values: Sequence[Decimal],
    period: int = 14,
) -> list[Decimal | None]:
    """Calculate Wilder's RSI."""

    if period <= 0:
        raise ValueError("Period must be positive")

    results: list[Decimal | None] = [None] * len(values)

    if len(values) <= period:
        return results

    gains: list[Decimal] = []
    losses: list[Decimal] = []

    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, Decimal("0")))
        losses.append(max(-change, Decimal("0")))

    avg_gain = sum(gains[:period], Decimal("0")) / period
    avg_loss = sum(losses[:period], Decimal("0")) / period

    hundred = Decimal("100")

    def rsi_value(gain: Decimal, loss: Decimal) -> Decimal:
        if loss == 0:
            return hundred if gain > 0 else Decimal("50")

        rs = gain / loss
        return hundred - hundred / (Decimal("1") + rs)

    results[period] = rsi_value(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        avg_gain = (
            avg_gain * (period - 1) + gains[index - 1]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[index - 1]
        ) / period

        results[index] = rsi_value(avg_gain, avg_loss)

    return results


def calculate_atr(
    candles: Sequence[Candle],
    period: int = 14,
) -> list[Decimal | None]:
    """Calculate Wilder's Average True Range."""

    if period <= 0:
        raise ValueError("Period must be positive")

    results: list[Decimal | None] = [None] * len(candles)

    if len(candles) < period:
        return results

    true_ranges: list[Decimal] = []

    for index, candle in enumerate(candles):
        if index == 0:
            true_range = candle.high - candle.low
        else:
            previous_close = candles[index - 1].close

            true_range = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )

        true_ranges.append(true_range)

    previous_atr = (
        sum(true_ranges[:period], Decimal("0")) / period
    )

    results[period - 1] = previous_atr

    for index in range(period, len(candles)):
        previous_atr = (
            previous_atr * (period - 1) + true_ranges[index]
        ) / period

        results[index] = previous_atr

    return results


def calculate_relative_volume(
    candles: Sequence[Candle],
    period: int = 20,
) -> list[Decimal | None]:
    """Compare each candle volume with prior average volume."""

    if period <= 0:
        raise ValueError("Period must be positive")

    volumes = [candle.volume for candle in candles]

    results: list[Decimal | None] = [None] * len(candles)

    if len(candles) <= period:
        return results

    window_sum = sum(volumes[:period], Decimal("0"))

    for index in range(period, len(volumes)):
        average_volume = window_sum / period

        if average_volume > 0:
            results[index] = volumes[index] / average_volume

        window_sum += volumes[index]
        window_sum -= volumes[index - period]

    return results
