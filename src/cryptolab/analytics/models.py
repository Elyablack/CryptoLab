"""Data models for market analytics."""

from dataclasses import dataclass
from decimal import Decimal

from cryptolab.market_data.markets import DEFAULT_EXCHANGE, SPOT_MARKET_TYPE


@dataclass(frozen=True)
class MarketAnalysisReport:
    """Summary of end-of-candle analytics.

    ``volatility`` is the sample standard deviation of simple returns for the
    report interval. It is not annualized. ``max_drawdown`` is non-positive.
    """

    symbol: str
    interval: str
    candles_analyzed: int
    last_open_time: int

    last_close: Decimal
    total_return: Decimal
    volatility: Decimal | None
    max_drawdown: Decimal

    sma20: Decimal
    sma50: Decimal
    ema20: Decimal
    rsi14: Decimal
    atr14: Decimal
    relative_volume20: Decimal
    exchange: str = DEFAULT_EXCHANGE
    market_type: str = SPOT_MARKET_TYPE

    @property
    def atr_percent(self) -> Decimal:
        return self.atr14 / self.last_close * 100
