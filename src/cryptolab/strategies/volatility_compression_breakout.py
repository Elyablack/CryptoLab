"""Volatility-compression breakout research strategy."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


@dataclass(frozen=True)
class VolatilityCompressionBreakoutStrategy:
    """Buy a new upper-range breakout after prior volatility compression.

    For candle ``t``, both the 120-candle volatility baseline and the
    24-candle compression range end strictly before ``t``. The current candle
    may confirm expansion because its signal is available only after it closes.
    The strategy is position-agnostic; Spot position state and the independent
    protective stop remain responsibilities of the backtest engine.
    """

    name: ClassVar[str] = "volatility_compression_breakout_v1"
    atr_period: ClassVar[int] = 14

    baseline_window: int = 120
    compression_window: int = 24
    exit_lookback: int = 12
    maximum_compression_ratio: Decimal = Decimal("0.70")
    minimum_atr_expansion_ratio: Decimal = Decimal("1.25")
    minimum_relative_volume: Decimal = Decimal("1.25")

    def __post_init__(self) -> None:
        for name, value in (
            ("baseline_window", self.baseline_window),
            ("compression_window", self.compression_window),
            ("exit_lookback", self.exit_lookback),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (
            ("maximum_compression_ratio", self.maximum_compression_ratio),
            ("minimum_atr_expansion_ratio", self.minimum_atr_expansion_ratio),
            ("minimum_relative_volume", self.minimum_relative_volume),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive Decimal")

    @property
    def minimum_history(self) -> int:
        """Rows needed for full ATR baseline, compression, and current candle.

        Wilder ATR14 first exists at zero-based index 13. With 158 rows the
        baseline is indices 13..132, compression is 133..156, and the current
        candle is index 157.
        """

        return max(
            (
                self.atr_period - 1
                + self.baseline_window
                + self.compression_window
                + 1
            ),
            self.compression_window + 2,
            self.exit_lookback + 2,
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
        compression_start = len(history) - self.compression_window - 1
        baseline_start = compression_start - self.baseline_window
        baseline_rows = history[baseline_start:compression_start]
        compression_rows = history[compression_start:-1]
        previous_compression_rows = history[
            -self.compression_window - 2 : -2
        ]
        exit_rows = history[-self.exit_lookback - 1 : -1]
        previous_exit_rows = history[-self.exit_lookback - 2 : -2]

        self._require_complete_window(
            "baseline", baseline_rows, self.baseline_window
        )
        self._require_complete_window(
            "compression", compression_rows, self.compression_window
        )
        self._require_complete_window(
            "previous compression",
            previous_compression_rows,
            self.compression_window,
        )
        self._require_complete_window("exit", exit_rows, self.exit_lookback)
        self._require_complete_window(
            "previous exit", previous_exit_rows, self.exit_lookback
        )

        atr_rows = (*baseline_rows, *compression_rows, current)
        if current.rvol20 is None or any(row.atr14 is None for row in atr_rows):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.HOLD,
                explanation="Required ATR or relative-volume values are unavailable",
                diagnostics={
                    "baseline_observations": len(baseline_rows),
                    "compression_observations": len(compression_rows),
                    "buy_blocker": "indicator_unavailable",
                },
            )

        baseline_atr_percent = self._mean_atr_percent(baseline_rows)
        compression_atr_percent = self._mean_atr_percent(compression_rows)
        current_atr_percent = current.atr14 / current.close
        compression_ratio = compression_atr_percent / baseline_atr_percent
        atr_expansion_ratio = current_atr_percent / compression_atr_percent

        upper_breakout_level = max(row.high for row in compression_rows)
        lower_compression_level = min(row.low for row in compression_rows)
        previous_upper = max(row.high for row in previous_compression_rows)
        exit_level = min(row.low for row in exit_rows)
        previous_exit_level = min(row.low for row in previous_exit_rows)

        compression_confirmed = (
            compression_ratio <= self.maximum_compression_ratio
        )
        upper_breakout_confirmed = current.close > upper_breakout_level
        previous_was_above = previous.close > previous_upper
        new_upper_breakout = upper_breakout_confirmed and not previous_was_above
        bullish_candle_confirmed = current.close > current.open
        atr_expansion_confirmed = (
            atr_expansion_ratio >= self.minimum_atr_expansion_ratio
        )
        volume_confirmed = current.rvol20 >= self.minimum_relative_volume
        lower_breakdown_confirmed = current.close < exit_level
        new_lower_breakdown = (
            lower_breakdown_confirmed and previous.close >= previous_exit_level
        )

        ordered_buy_conditions = (
            ("compression", compression_confirmed),
            ("upper_breakout", upper_breakout_confirmed),
            ("new_breakout_event", new_upper_breakout),
            ("bullish_candle", bullish_candle_confirmed),
            ("atr_expansion", atr_expansion_confirmed),
            ("relative_volume", volume_confirmed),
        )
        buy_blocker = next(
            (name for name, confirmed in ordered_buy_conditions if not confirmed),
            "none",
        )
        diagnostics = {
            "baseline_observations": len(baseline_rows),
            "compression_observations": len(compression_rows),
            "baseline_atr_percent_mean": baseline_atr_percent,
            "compression_atr_percent_mean": compression_atr_percent,
            "compression_ratio": compression_ratio,
            "maximum_compression_ratio": self.maximum_compression_ratio,
            "current_atr_percent": current_atr_percent,
            "atr_expansion_ratio": atr_expansion_ratio,
            "minimum_atr_expansion_ratio": self.minimum_atr_expansion_ratio,
            "upper_breakout_level": upper_breakout_level,
            "lower_compression_level": lower_compression_level,
            "exit_level": exit_level,
            "rvol20": current.rvol20,
            "compression_confirmed": compression_confirmed,
            "upper_breakout_confirmed": upper_breakout_confirmed,
            "new_upper_breakout": new_upper_breakout,
            "bullish_candle_confirmed": bullish_candle_confirmed,
            "atr_expansion_confirmed": atr_expansion_confirmed,
            "volume_confirmed": volume_confirmed,
            "new_lower_breakdown": new_lower_breakdown,
            "buy_blocker": buy_blocker,
        }

        if new_lower_breakdown:
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.SELL,
                explanation=(
                    "New close breakdown below the prior 12-candle exit range; "
                    "SELL means exit long Spot exposure, not open a short"
                ),
                diagnostics=diagnostics,
            )

        if all(confirmed for _, confirmed in ordered_buy_conditions):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.BUY,
                explanation=(
                    "New upper-range breakout after volatility compression, "
                    "confirmed by ATR expansion and relative volume"
                ),
                diagnostics=diagnostics,
            )

        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=current,
            signal=Signal.HOLD,
            explanation=f"BUY blocked by {buy_blocker}",
            diagnostics=diagnostics,
        )

    @staticmethod
    def _require_complete_window(
        name: str,
        rows: Sequence[FeatureRow],
        expected: int,
    ) -> None:
        if len(rows) != expected:
            raise RuntimeError(
                f"Incomplete {name} window: expected {expected}, got {len(rows)}"
            )

    @staticmethod
    def _mean_atr_percent(rows: Sequence[FeatureRow]) -> Decimal:
        values = []
        for row in rows:
            if row.atr14 is None:
                raise RuntimeError("ATR window unexpectedly contains None")
            values.append(row.atr14 / row.close)
        mean = sum(values, Decimal("0")) / Decimal(len(values))
        if mean <= 0:
            raise ValueError("ATR percentage mean must be positive")
        return mean
