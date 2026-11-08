"""Domain models for market data."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    """A normalized public Spot OHLCV candlestick.

    Trade count and taker-buy volumes are optional because Bybit V5 klines do
    not provide them. Missing exchange fields remain ``None`` rather than being
    estimated or fabricated.
    """

    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int
    quote_volume: Decimal
    number_of_trades: int | None
    taker_buy_base_volume: Decimal | None
    taker_buy_quote_volume: Decimal | None

