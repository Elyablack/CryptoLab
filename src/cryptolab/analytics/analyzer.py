"""Market analysis orchestration."""

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
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.market_data.validator import validate_historical_candles


class MarketAnalyzer:
    """Build a market report from a complete, closed candle sequence."""

    MIN_CANDLES = 50

    def analyze(
        self,
        symbol: str,
        interval: str,
        candles: Sequence[Candle],
        *,
        as_of_time: int | None = None,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> MarketAnalysisReport:
        validate_market_identity(exchange, market_type)
        if len(candles) < self.MIN_CANDLES:
            raise ValueError(
                f"At least {self.MIN_CANDLES} candles are required"
            )

        validate_historical_candles(
            candles,
            interval,
            as_of_time=as_of_time,
        )

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
            exchange=exchange,
            market_type=market_type,
        )
