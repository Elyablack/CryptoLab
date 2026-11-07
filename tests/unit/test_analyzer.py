import time
from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.analytics.analyzer import MarketAnalyzer
from cryptolab.market_data.models import Candle


HOUR_MILLISECONDS = 3_600_000


def make_candles(
    count: int,
    *,
    start_time: int = 0,
    step: int = HOUR_MILLISECONDS,
) -> list[Candle]:
    candles = []
    for index in range(count):
        close = Decimal(100 + index)
        volume = Decimal(100 + index)
        open_time = start_time + index * step
        candles.append(
            Candle(
                open_time=open_time,
                open=close - Decimal("0.5"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=volume,
                close_time=open_time + step - 1,
                quote_volume=volume * close,
                number_of_trades=100 + index,
                taker_buy_base_volume=volume / 2,
                taker_buy_quote_volume=volume * close / 2,
            )
        )
    return candles


def reference_ema(values: list[Decimal], period: int) -> Decimal:
    value = sum(values[:period], Decimal("0")) / period
    multiplier = Decimal("2") / (period + 1)
    for current in values[period:]:
        value = current * multiplier + value * (Decimal("1") - multiplier)
    return value


def reference_sample_stddev(values: list[Decimal]) -> Decimal:
    mean = sum(values, Decimal("0")) / len(values)
    variance = sum(
        ((value - mean) ** 2 for value in values),
        Decimal("0"),
    ) / (len(values) - 1)
    return variance.sqrt()


def test_analyze_builds_report_from_reference_values() -> None:
    candles = make_candles(50)
    closes = [candle.close for candle in candles]
    returns = [
        (current - previous) / previous
        for previous, current in zip(closes, closes[1:])
    ]

    report = MarketAnalyzer().analyze("BTCUSDT", "1h", candles)

    assert report.symbol == "BTCUSDT"
    assert report.interval == "1h"
    assert report.candles_analyzed == 50
    assert report.last_open_time == 49 * HOUR_MILLISECONDS
    assert report.last_close == Decimal("149")
    assert report.total_return == Decimal("0.49")
    assert report.volatility == reference_sample_stddev(returns)
    assert report.max_drawdown == Decimal("0")
    assert report.sma20 == Decimal("139.5")
    assert report.sma50 == Decimal("124.5")
    assert report.ema20 == reference_ema(closes, period=20)
    assert report.rsi14 == Decimal("100")
    assert report.atr14 == Decimal("4")
    assert report.relative_volume20 == Decimal(298) / 277


def test_analyze_rejects_insufficient_history() -> None:
    with pytest.raises(ValueError, match="At least 50 candles"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", make_candles(49))


def test_analyze_rejects_duplicate_timestamp() -> None:
    candles = make_candles(50)
    candles[20] = replace(candles[20], open_time=candles[19].open_time)

    with pytest.raises(ValueError, match="strictly greater"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", candles)


def test_analyze_rejects_invalid_ohlcv() -> None:
    candles = make_candles(50)
    candles[10] = replace(candles[10], high=Decimal("1"))

    with pytest.raises(ValueError, match="high price cannot be less"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", candles)


def test_analyze_rejects_missing_interval() -> None:
    candles = make_candles(50)
    candles[25:] = [
        replace(
            candle,
            open_time=candle.open_time + HOUR_MILLISECONDS,
            close_time=candle.close_time + HOUR_MILLISECONDS,
        )
        for candle in candles[25:]
    ]

    with pytest.raises(ValueError, match="consecutive"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", candles)


def test_analyze_rejects_unaligned_open_time() -> None:
    candles = make_candles(50, start_time=1)

    with pytest.raises(ValueError, match="aligned"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", candles)


def test_analyze_rejects_incorrect_close_time() -> None:
    candles = make_candles(50)
    candles[10] = replace(candles[10], close_time=candles[10].close_time - 1)

    with pytest.raises(ValueError, match="close_time"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", candles)


def test_analyze_rejects_unclosed_candle() -> None:
    future_start = int(time.time() * 1000) + HOUR_MILLISECONDS
    future_start -= future_start % HOUR_MILLISECONDS
    candles = make_candles(50, start_time=future_start)

    with pytest.raises(ValueError, match="not closed"):
        MarketAnalyzer().analyze("BTCUSDT", "1h", candles)


def test_analyze_rejects_unsupported_interval() -> None:
    with pytest.raises(ValueError, match="Unsupported interval"):
        MarketAnalyzer().analyze("BTCUSDT", "2h", make_candles(50))

