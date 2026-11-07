"""Market analysis orchestration."""

import time
from collections.abc import Sequence
from decimal import Decimal

from cryptolab.analytics.indicators import (
    calculate_atr,
    calculate_ema,
    calculate_relative_volume,
    calculate_rsi,
    calculate_sma,
)
from cryptolab.analytics.models import MarketAnalysisReport
from cryptolab.analytics.returns import (
    calculate_max_drawdown,
    calculate_simple_returns,
    calculate_volatility,
)
from cryptolab.market_data.models import Candle
from cryptolab.market_data.validator import validate_candles


class MarketAnalyzer:
    """Build a market report from a complete, closed candle sequence."""

    MIN_CANDLES = 50
    INTERVAL_MILLISECONDS = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
        "1d": 24 * 60 * 60_000,
    }

    def analyze(
        self,
        symbol: str,
        interval: str,
        candles: Sequence[Candle],
    ) -> MarketAnalysisReport:
        if len(candles) < self.MIN_CANDLES:
            raise ValueError(
                f"At least {self.MIN_CANDLES} candles are required"
            )

        validate_candles(list(candles))
        self._validate_candle_times(interval, candles)

        closes = [candle.close for candle in candles]

        simple_returns = calculate_simple_returns(candles)

        sma20 = calculate_sma(closes, period=20)[-1]
        sma50 = calculate_sma(closes, period=50)[-1]
        ema20 = calculate_ema(closes, period=20)[-1]
        rsi14 = calculate_rsi(closes, period=14)[-1]
        atr14 = calculate_atr(candles, period=14)[-1]
        rvol20 = calculate_relative_volume(candles, period=20)[-1]

        volatility = calculate_volatility(simple_returns)
        max_drawdown = calculate_max_drawdown(candles)

        if any(
            value is None
            for value in (
                sma20,
                sma50,
                ema20,
                rsi14,
                atr14,
                rvol20,
                max_drawdown,
            )
        ):
            raise ValueError("Insufficient data for market indicators")

        total_return = closes[-1] / closes[0] - Decimal("1")

        return MarketAnalysisReport(
            symbol=symbol,
            interval=interval,
            candles_analyzed=len(candles),
            last_open_time=candles[-1].open_time,
            last_close=closes[-1],
            total_return=total_return,
            volatility=volatility,
            max_drawdown=max_drawdown,
            sma20=sma20,
            sma50=sma50,
            ema20=ema20,
            rsi14=rsi14,
            atr14=atr14,
            relative_volume20=rvol20,
        )

    def _validate_candle_times(
        self,
        interval: str,
        candles: Sequence[Candle],
    ) -> None:
        try:
            interval_milliseconds = self.INTERVAL_MILLISECONDS[interval]
        except KeyError as error:
            raise ValueError(f"Unsupported interval: {interval}") from error

        current_time = int(time.time() * 1000)
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

            expected_close_time = (
                candle.open_time + interval_milliseconds - 1
            )
            if candle.close_time != expected_close_time:
                raise ValueError(
                    f"Candle at index {index}: close_time does not match "
                    f"interval {interval}"
                )

            if candle.close_time >= current_time:
                raise ValueError(
                    f"Candle at index {index} is not closed"
                )

            previous_open_time = candle.open_time
