"""Historical market feature models."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FeatureRow:
    """Features known only after ``available_time``.

    Binance candle ``close_time`` is inclusive, therefore the row becomes
    usable at ``close_time + 1``. Indicator values use only this candle and
    earlier candles.
    """

    open_time: int
    close_time: int
    available_time: int

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    simple_return: Decimal | None
    log_return: Decimal | None

    sma20: Decimal | None
    sma50: Decimal | None
    ema20: Decimal | None

    rsi14: Decimal | None
    atr14: Decimal | None
    rvol20: Decimal | None
