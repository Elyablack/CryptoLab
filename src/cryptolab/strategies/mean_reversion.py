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
    """Signal only after an extreme starts recovering, not at the extreme."""

    name: ClassVar[str] = "mean_reversion"

    oversold_rsi: Decimal = Decimal("30")
    oversold_recovery_rsi: Decimal = Decimal("35")
    overbought_recovery_rsi: Decimal = Decimal("65")
    overbought_rsi: Decimal = Decimal("70")
    minimum_sma_deviation: Decimal = Decimal("0.03")

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

    @property
    def minimum_history(self) -> int:
        return 21

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
            previous.sma20,
            previous.rsi14,
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

        previous_deviation = (
            previous.close - previous.sma20
        ) / previous.sma20
        current_deviation = (
            current.close - current.sma20
        ) / current.sma20
        diagnostics = {
            "previous_rsi14": previous.rsi14,
            "rsi14": current.rsi14,
            "previous_sma20_deviation": previous_deviation,
            "sma20_deviation": current_deviation,
            "simple_return": current.simple_return,
        }

        if (
            previous.rsi14 <= self.oversold_rsi
            and previous_deviation <= -self.minimum_sma_deviation
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
            previous.rsi14 >= self.overbought_rsi
            and previous_deviation >= self.minimum_sma_deviation
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

