"""Predeclared long-only pullback candidate for research."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


@dataclass(frozen=True)
class TrendPullbackStrategy:
    """Buy the first confirmed recovery from an EMA20 pullback in an uptrend.

    The strategy is stateless and position-agnostic. Its SELL is a trend-break
    market event; the BacktestEngine owns position state and the independent
    fixed protective stop.
    """

    name: ClassVar[str] = "trend_pullback_v1"

    trend_slope_lookback: int = 6
    pullback_lookback: int = 3
    pullback_rsi_min: Decimal = Decimal("40")
    pullback_rsi_max: Decimal = Decimal("55")
    confirmation_rsi_min: Decimal = Decimal("50")
    confirmation_rsi_max: Decimal = Decimal("65")
    minimum_relative_volume: Decimal = Decimal("0.8")
    maximum_atr_percent: Decimal = Decimal("0.025")

    def __post_init__(self) -> None:
        if type(self.trend_slope_lookback) is not int or (
            self.trend_slope_lookback <= 0
        ):
            raise ValueError("trend_slope_lookback must be a positive integer")
        if type(self.pullback_lookback) is not int or self.pullback_lookback <= 0:
            raise ValueError("pullback_lookback must be a positive integer")
        if not (
            Decimal("0")
            <= self.pullback_rsi_min
            <= self.pullback_rsi_max
            <= Decimal("100")
        ):
            raise ValueError("Pullback RSI thresholds must be ordered")
        if not (
            Decimal("0")
            <= self.confirmation_rsi_min
            <= self.confirmation_rsi_max
            <= Decimal("100")
        ):
            raise ValueError("Confirmation RSI thresholds must be ordered")
        if self.minimum_relative_volume < 0:
            raise ValueError("minimum_relative_volume must be non-negative")
        if not Decimal("0") < self.maximum_atr_percent < 1:
            raise ValueError("maximum_atr_percent must be between 0 and 1")

    @property
    def minimum_history(self) -> int:
        return max(
            50 + self.trend_slope_lookback,
            self.pullback_lookback + 2,
        )

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ) -> StrategyDecision:
        if len(history) < self.minimum_history:
            raise ValueError(
                f"{self.name} requires {self.minimum_history} feature rows"
            )
        current = history[-1]
        previous = history[-2]
        slope_reference = history[-self.trend_slope_lookback - 1]
        required = (
            current.sma20,
            current.sma50,
            current.ema20,
            current.rsi14,
            current.atr14,
            current.rvol20,
            current.simple_return,
            previous.sma20,
            previous.sma50,
            slope_reference.sma50,
        )
        if any(value is None for value in required):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.HOLD,
                explanation="Required pullback indicators are unavailable",
            )

        bearish_cross = (
            previous.sma20 >= previous.sma50
            and current.sma20 < current.sma50
        )
        price_broke_trend = (
            previous.close >= previous.sma50
            and current.close < current.sma50
            and current.sma20 < previous.sma20
        )
        diagnostics = {
            "close": current.close,
            "ema20": current.ema20,
            "sma20": current.sma20,
            "sma50": current.sma50,
            "sma50_slope_reference": slope_reference.sma50,
            "rsi14": current.rsi14,
            "rvol20": current.rvol20,
            "atr_percent": current.atr14 / current.close,
            "bearish_cross": bearish_cross,
            "price_broke_trend": price_broke_trend,
        }
        if bearish_cross or price_broke_trend:
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.SELL,
                explanation=(
                    "Uptrend structure broke; SELL means exit long Spot "
                    "exposure, not open a short"
                ),
                diagnostics=diagnostics,
            )

        uptrend = (
            current.sma20 > current.sma50
            and current.close > current.sma50
            and current.sma50 > slope_reference.sma50
        )
        pullback_origin = self._find_unused_pullback(history)
        confirmation = (
            current.close > previous.high
            and current.close > current.ema20
            and current.simple_return > 0
            and self.confirmation_rsi_min
            <= current.rsi14
            <= self.confirmation_rsi_max
            and current.rvol20 >= self.minimum_relative_volume
            and current.atr14 / current.close <= self.maximum_atr_percent
        )
        diagnostics.update({
            "uptrend": uptrend,
            "confirmation": confirmation,
            "pullback_origin_open_time": (
                None
                if pullback_origin is None
                else pullback_origin.open_time
            ),
        })
        if uptrend and pullback_origin is not None and confirmation:
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.BUY,
                explanation=(
                    "EMA20 pullback in a rising SMA20/SMA50 trend confirmed "
                    "by price recovery, RSI, volume, and ATR"
                ),
                diagnostics=diagnostics,
            )

        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=current,
            signal=Signal.HOLD,
            explanation="No confirmed uptrend pullback recovery",
            diagnostics=diagnostics,
        )

    def _find_unused_pullback(
        self,
        history: Sequence[FeatureRow],
    ) -> FeatureRow | None:
        start = max(0, len(history) - self.pullback_lookback - 1)
        end = len(history) - 1
        for index in range(end - 1, start - 1, -1):
            candidate = history[index]
            if (
                candidate.ema20 is None
                or candidate.sma50 is None
                or candidate.rsi14 is None
            ):
                continue
            if not (
                candidate.low <= candidate.ema20
                and candidate.close > candidate.sma50
                and self.pullback_rsi_min
                <= candidate.rsi14
                <= self.pullback_rsi_max
            ):
                continue
            already_confirmed = any(
                row.ema20 is not None
                and row.rsi14 is not None
                and row.close > history[later_index - 1].high
                and row.close > row.ema20
                and row.rsi14 >= self.confirmation_rsi_min
                for later_index, row in enumerate(
                    history[index + 1 : end],
                    start=index + 1,
                )
            )
            if not already_confirmed:
                return candidate
        return None
