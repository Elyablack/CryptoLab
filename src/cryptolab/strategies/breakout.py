"""Historical-range breakout research strategy."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


class BreakoutEventPolicy(str, Enum):
    """Control whether confirmed continuation candles emit signals."""

    NEW_ONLY = "new_only"
    EVERY_CONFIRMED = "every_confirmed"


@dataclass(frozen=True)
class BreakoutStrategy:
    """Detect close breakouts using ranges strictly before the current row.

    The default policy emits a signal only when the rolling breakout condition
    changes from false to true. It can emit again after the condition resets,
    without retaining portfolio or execution state.
    """

    name: ClassVar[str] = "breakout"

    lookback_period: int = 20
    minimum_relative_volume: Decimal = Decimal("1.5")
    minimum_atr_percent: Decimal = Decimal("0.005")
    event_policy: BreakoutEventPolicy = BreakoutEventPolicy.NEW_ONLY

    def __post_init__(self) -> None:
        if self.lookback_period <= 0:
            raise ValueError("lookback_period must be positive")
        if self.minimum_relative_volume < 0:
            raise ValueError("minimum_relative_volume must be non-negative")
        if self.minimum_atr_percent < 0:
            raise ValueError("minimum_atr_percent must be non-negative")
        if not isinstance(self.event_policy, BreakoutEventPolicy):
            raise ValueError("event_policy must be a BreakoutEventPolicy")

    @property
    def minimum_history(self) -> int:
        return self.lookback_period + 1

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
        prior_rows = history[-self.lookback_period - 1 : -1]

        if current.rvol20 is None or current.atr14 is None:
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.HOLD,
                explanation="Required breakout indicators are not available",
            )

        upper_breakout_level = max(row.high for row in prior_rows)
        lower_breakout_level = min(row.low for row in prior_rows)
        atr_percent = current.atr14 / current.close
        volume_confirmed = current.rvol20 >= self.minimum_relative_volume
        volatility_confirmed = atr_percent >= self.minimum_atr_percent

        previous_was_above = False
        previous_was_below = False
        if len(history) >= self.lookback_period + 2:
            previous = history[-2]
            previous_prior_rows = history[
                -self.lookback_period - 2 : -2
            ]
            previous_upper = max(row.high for row in previous_prior_rows)
            previous_lower = min(row.low for row in previous_prior_rows)
            previous_was_above = previous.close > previous_upper
            previous_was_below = previous.close < previous_lower

        is_above = current.close > upper_breakout_level
        is_below = current.close < lower_breakout_level
        event_kind = "none"
        if is_above:
            event_kind = "continued" if previous_was_above else "new"
        elif is_below:
            event_kind = "continued" if previous_was_below else "new"

        emit_above = is_above and (
            not previous_was_above
            or self.event_policy is BreakoutEventPolicy.EVERY_CONFIRMED
        )
        emit_below = is_below and (
            not previous_was_below
            or self.event_policy is BreakoutEventPolicy.EVERY_CONFIRMED
        )

        diagnostics = {
            "upper_breakout_level": upper_breakout_level,
            "lower_breakout_level": lower_breakout_level,
            "rvol20": current.rvol20,
            "atr_percent": atr_percent,
            "lookback_period": self.lookback_period,
            "event_policy": self.event_policy.value,
            "event_kind": event_kind,
        }

        if (
            emit_above
            and volume_confirmed
            and volatility_confirmed
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.BUY,
                explanation=(
                    f"{event_kind.title()} close breakout above the prior "
                    "historical range with "
                    "relative-volume and ATR confirmation"
                ),
                diagnostics=diagnostics,
            )

        if (
            emit_below
            and volume_confirmed
            and volatility_confirmed
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.SELL,
                explanation=(
                    f"{event_kind.title()} close breakout below the prior "
                    "historical range; SELL "
                    "means reduce or exit spot exposure, not open a short"
                ),
                diagnostics=diagnostics,
            )

        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=current,
            signal=Signal.HOLD,
            explanation="No newly confirmed historical-range breakout",
            diagnostics=diagnostics,
        )

