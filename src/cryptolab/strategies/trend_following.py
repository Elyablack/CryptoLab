"""Moving-average crossover research strategy."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


class TrendExitPolicy(str, Enum):
    """Confirmation required for a bearish crossover SELL signal."""

    CONFIRMED = "confirmed"
    CROSSOVER_ONLY = "crossover_only"


@dataclass(frozen=True)
class TrendFollowingStrategy:
    """Generate SMA20/SMA50 crossover market-event signals.

    The default exit policy preserves RSI and relative-volume confirmation.
    ``CROSSOVER_ONLY`` is an explicit research alternative, not an executable
    stop-loss or a position-dependent exit instruction.
    """

    name: ClassVar[str] = "trend_following"

    buy_rsi_min: Decimal = Decimal("50")
    buy_rsi_max: Decimal = Decimal("70")
    sell_rsi_max: Decimal = Decimal("50")
    minimum_relative_volume: Decimal = Decimal("1")
    exit_policy: TrendExitPolicy = TrendExitPolicy.CONFIRMED

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.buy_rsi_min <= self.buy_rsi_max <= 100:
            raise ValueError("Buy RSI thresholds must satisfy 0 <= min <= max <= 100")
        if not Decimal("0") <= self.sell_rsi_max <= 100:
            raise ValueError("sell_rsi_max must be between 0 and 100")
        if self.minimum_relative_volume < 0:
            raise ValueError("minimum_relative_volume must be non-negative")
        if not isinstance(self.exit_policy, TrendExitPolicy):
            raise ValueError("exit_policy must be a TrendExitPolicy")

    @property
    def minimum_history(self) -> int:
        return 51

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
            previous.sma50,
            current.sma20,
            current.sma50,
        )
        if any(value is None for value in required):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.HOLD,
                explanation="Required trend indicators are not available",
            )

        bullish_cross = (
            previous.sma20 <= previous.sma50
            and current.sma20 > current.sma50
        )
        bearish_cross = (
            previous.sma20 >= previous.sma50
            and current.sma20 < current.sma50
        )
        volume_confirmed = (
            current.rvol20 is not None
            and current.rvol20 >= self.minimum_relative_volume
        )
        buy_rsi_confirmed = (
            current.rsi14 is not None
            and self.buy_rsi_min <= current.rsi14 <= self.buy_rsi_max
        )
        sell_rsi_confirmed = (
            current.rsi14 is not None
            and current.rsi14 <= self.sell_rsi_max
        )
        exit_confirmed = (
            self.exit_policy is TrendExitPolicy.CROSSOVER_ONLY
            or sell_rsi_confirmed and volume_confirmed
        )

        diagnostics = {
            "close": current.close,
            "sma20": current.sma20,
            "sma50": current.sma50,
            "rsi14": current.rsi14,
            "rvol20": current.rvol20,
            "exit_policy": self.exit_policy.value,
            "exit_rsi_confirmed": sell_rsi_confirmed,
            "volume_confirmed": volume_confirmed,
        }

        if (
            bullish_cross
            and current.close > current.sma20 > current.sma50
            and buy_rsi_confirmed
            and volume_confirmed
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.BUY,
                explanation=(
                    "Bullish SMA20/SMA50 crossover confirmed by price, RSI, "
                    "and relative volume"
                ),
                diagnostics=diagnostics,
            )

        if (
            bearish_cross
            and current.close < current.sma20 < current.sma50
            and exit_confirmed
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.SELL,
                explanation=(
                    "Bearish SMA20/SMA50 crossover satisfies the configured "
                    f"{self.exit_policy.value} research exit policy; SELL "
                    "means reducing or exiting spot exposure"
                ),
                diagnostics=diagnostics,
            )

        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=current,
            signal=Signal.HOLD,
            explanation="No confirmed trend crossover signal",
            diagnostics=diagnostics,
        )

