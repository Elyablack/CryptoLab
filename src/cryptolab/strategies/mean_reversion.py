"""Confirmed mean-reversion research strategy."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


@dataclass(frozen=True)
class MeanReversionStrategy:
    """Signal only on the first confirmed recovery from a recent extreme.

    ``recovery_window=1`` preserves the original consecutive-candle behavior.
    A larger window permits gradual recovery without retaining strategy state.
    """

    name: ClassVar[str] = "mean_reversion"

    oversold_rsi: Decimal = Decimal("30")
    oversold_recovery_rsi: Decimal = Decimal("35")
    overbought_recovery_rsi: Decimal = Decimal("65")
    overbought_rsi: Decimal = Decimal("70")
    minimum_sma_deviation: Decimal = Decimal("0.03")
    recovery_window: int = 1

    def __post_init__(self) -> None:
        if not (
            0 <= self.oversold_rsi
            < self.oversold_recovery_rsi
            <= self.overbought_recovery_rsi
            < self.overbought_rsi
            <= 100
        ):
            raise ValueError("RSI thresholds must be ordered between 0 and 100")
        if self.minimum_sma_deviation <= 0:
            raise ValueError("minimum_sma_deviation must be positive")
        if type(self.recovery_window) is not int or self.recovery_window <= 0:
            raise ValueError("recovery_window must be a positive integer")

    @property
    def minimum_history(self) -> int:
        return max(21, self.recovery_window + 1)

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
        required = (
            current.sma20,
            current.rsi14,
            current.simple_return,
        )
        if any(value is None for value in required):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.HOLD,
                explanation="Required mean-reversion indicators are not available",
            )

        current_deviation = (
            current.close - current.sma20
        ) / current.sma20
        previous_deviation = (
            None
            if previous.sma20 is None
            else (previous.close - previous.sma20) / previous.sma20
        )
        recovery_rows = history[-self.recovery_window - 1 : -1]
        oversold_origin = self._find_recovery_origin(
            recovery_rows,
            extreme_rsi=self.oversold_rsi,
            minimum_deviation=-self.minimum_sma_deviation,
            is_oversold=True,
            recovery_rsi=self.oversold_recovery_rsi,
        )
        overbought_origin = self._find_recovery_origin(
            recovery_rows,
            extreme_rsi=self.overbought_rsi,
            minimum_deviation=self.minimum_sma_deviation,
            is_oversold=False,
            recovery_rsi=self.overbought_recovery_rsi,
        )
        diagnostics = {
            "previous_rsi14": previous.rsi14,
            "rsi14": current.rsi14,
            "previous_sma20_deviation": previous_deviation,
            "sma20_deviation": current_deviation,
            "simple_return": current.simple_return,
            "recovery_window": self.recovery_window,
            "oversold_origin_open_time": (
                None if oversold_origin is None else oversold_origin.open_time
            ),
            "overbought_origin_open_time": (
                None if overbought_origin is None else overbought_origin.open_time
            ),
        }

        if (
            oversold_origin is not None
            and current.rsi14 >= self.oversold_recovery_rsi
            and current.simple_return > 0
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.BUY,
                explanation=(
                    "Oversold price deviation is recovering with positive "
                    "return and RSI confirmation"
                ),
                diagnostics=diagnostics,
            )

        if (
            overbought_origin is not None
            and current.rsi14 <= self.overbought_recovery_rsi
            and current.simple_return < 0
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.SELL,
                explanation=(
                    "Overbought price deviation is reversing; SELL means "
                    "reduce or exit spot exposure, not open a short"
                ),
                diagnostics=diagnostics,
            )

        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=current,
            signal=Signal.HOLD,
            explanation="No confirmed mean-reversion recovery",
            diagnostics=diagnostics,
        )

    @staticmethod
    def _find_recovery_origin(
        rows: Sequence[FeatureRow],
        *,
        extreme_rsi: Decimal,
        minimum_deviation: Decimal,
        is_oversold: bool,
        recovery_rsi: Decimal,
    ) -> FeatureRow | None:
        """Find the latest unused extreme within a past-only window."""
        for index in range(len(rows) - 1, -1, -1):
            candidate = rows[index]
            if candidate.rsi14 is None or candidate.sma20 is None:
                continue

            deviation = (
                candidate.close - candidate.sma20
            ) / candidate.sma20
            is_extreme = (
                candidate.rsi14 <= extreme_rsi
                and deviation <= minimum_deviation
                if is_oversold
                else candidate.rsi14 >= extreme_rsi
                and deviation >= minimum_deviation
            )
            if not is_extreme:
                continue

            later_rsi_values = (
                row.rsi14
                for row in rows[index + 1 :]
                if row.rsi14 is not None
            )
            already_recovered = (
                any(value >= recovery_rsi for value in later_rsi_values)
                if is_oversold
                else any(value <= recovery_rsi for value in later_rsi_values)
            )
            if not already_recovered:
                return candidate

        return None

