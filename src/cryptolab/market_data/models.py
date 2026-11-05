"""Domain models for market data."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    """A single OHLCV candlestick returned by Binance Spot."""

    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int
    quote_volume: Decimal
    number_of_trades: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal

