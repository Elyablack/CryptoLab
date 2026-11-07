"""Build aligned historical market features."""

from collections.abc import Sequence

from cryptolab.analytics.indicators import (
    calculate_atr,
    calculate_ema,
    calculate_relative_volume,
    calculate_rsi,
    calculate_sma,
)
from cryptolab.analytics.returns import (
    calculate_log_returns,
    calculate_simple_returns,
)
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.intervals import INTERVAL_MILLISECONDS
from cryptolab.market_data.models import Candle
from cryptolab.market_data.validator import validate_historical_candles


class FeatureBuilder:
    """Build aligned, end-of-candle features without future information."""

    def build(
        self,
        candles: Sequence[Candle],
        *,
        interval: str | None = None,
        as_of_time: int | None = None,
    ) -> list[FeatureRow]:
        """Build one feature row for each closed input candle.

        ``interval`` is optional for backward compatibility and is inferred
        from the first candle duration when omitted. Pass ``as_of_time`` for
        deterministic historical simulation.
        """

        if not candles:
            return []

        effective_interval = interval or self._infer_interval(candles[0])
        validate_historical_candles(
            candles,
            effective_interval,
            as_of_time=as_of_time,
        )

        closes = [candle.close for candle in candles]

        simple_returns = [None, *calculate_simple_returns(candles)]
        log_returns = [None, *calculate_log_returns(candles)]

        sma20 = calculate_sma(closes, period=20)
        sma50 = calculate_sma(closes, period=50)
        ema20 = calculate_ema(closes, period=20)

        rsi14 = calculate_rsi(closes, period=14)
        atr14 = calculate_atr(candles, period=14)
        rvol20 = calculate_relative_volume(candles, period=20)

        rows: list[FeatureRow] = []

        for index, candle in enumerate(candles):
            rows.append(
                FeatureRow(
                    open_time=candle.open_time,
                    close_time=candle.close_time,
                    available_time=candle.close_time + 1,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    simple_return=simple_returns[index],
                    log_return=log_returns[index],
                    sma20=sma20[index],
                    sma50=sma50[index],
                    ema20=ema20[index],
                    rsi14=rsi14[index],
                    atr14=atr14[index],
                    rvol20=rvol20[index],
                )
            )

        return rows

    @staticmethod
    def _infer_interval(candle: Candle) -> str:
        duration = candle.close_time - candle.open_time + 1
        for interval, interval_milliseconds in INTERVAL_MILLISECONDS.items():
            if duration == interval_milliseconds:
                return interval

        raise ValueError(
            "Cannot infer a supported interval from candle timestamps"
        )
