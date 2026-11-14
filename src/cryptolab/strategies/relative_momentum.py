"""Look-ahead-safe BTC/ETH relative-momentum rotation signals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


HOUR_MILLISECONDS = 3_600_000
DEFAULT_LOOKBACK_CANDLES = 30 * 24
DEFAULT_REBALANCE_CANDLES = 7 * 24


class RotationTarget(str, Enum):
    """Permitted holdings for the fixed BTC/ETH Spot rotation study."""

    CASH = "USDT"
    BTC = "BTCUSDT"
    ETH = "ETHUSDT"


@dataclass(frozen=True)
class RotationReview:
    """One weekly decision calculated from information known at candle close."""

    candle_open_time: int
    signal_available_time: int
    btc_momentum: Decimal
    eth_momentum: Decimal
    previous_target: RotationTarget
    target: RotationTarget

    @property
    def target_changed(self) -> bool:
        return self.target is not self.previous_target


@dataclass(frozen=True)
class RotationLegSignal:
    """Action for one instrument participating in a portfolio rotation."""

    symbol: str
    candle_open_time: int
    signal: Signal
    review: RotationReview


@dataclass(frozen=True)
class RelativeMomentumRotationPlan:
    """Immutable synchronized decisions for both portfolio legs."""

    reviews: tuple[RotationReview, ...]
    leg_signals: tuple[RotationLegSignal, ...]

    @property
    def target_change_count(self) -> int:
        return sum(review.target_changed for review in self.reviews)

    @property
    def asset_rotation_count(self) -> int:
        assets = {RotationTarget.BTC, RotationTarget.ETH}
        return sum(
            review.previous_target in assets
            and review.target in assets
            and review.target_changed
            for review in self.reviews
        )

    def strategy_for(self, symbol: str) -> RotationLegStrategy:
        """Return the Strategy adapter for one configured instrument."""
        if symbol not in {RotationTarget.BTC.value, RotationTarget.ETH.value}:
            raise ValueError(f"Unsupported rotation symbol: {symbol}")
        return RotationLegStrategy(
            symbol=symbol,
            signals=tuple(
                signal for signal in self.leg_signals if signal.symbol == symbol
            ),
        )


@dataclass(frozen=True)
class RotationLegStrategy:
    """Expose one synchronized rotation leg through the Strategy protocol."""

    symbol: str
    signals: tuple[RotationLegSignal, ...]
    _signals_by_time: Mapping[int, RotationLegSignal] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.symbol not in {
            RotationTarget.BTC.value,
            RotationTarget.ETH.value,
        }:
            raise ValueError(f"Unsupported rotation symbol: {self.symbol}")
        by_time: dict[int, RotationLegSignal] = {}
        for item in self.signals:
            if item.symbol != self.symbol:
                raise ValueError("Rotation signal belongs to another symbol")
            if item.candle_open_time in by_time:
                raise ValueError("Duplicate rotation signal timestamp")
            by_time[item.candle_open_time] = item
        object.__setattr__(self, "_signals_by_time", MappingProxyType(by_time))

    @property
    def name(self) -> str:
        return f"btc_eth_relative_momentum:{self.symbol}"

    @property
    def minimum_history(self) -> int:
        return 1

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ) -> StrategyDecision:
        if symbol != self.symbol:
            raise ValueError(
                f"Rotation leg for {self.symbol} cannot evaluate {symbol}"
            )
        row = history[-1]
        item = self._signals_by_time.get(row.open_time)
        if item is None:
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=row,
                signal=Signal.HOLD,
                explanation="No target change at this timestamp",
                diagnostics={"rotation_target_changed": False},
            )
        review = item.review
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=row,
            signal=item.signal,
            explanation=(
                f"Weekly target changed from {review.previous_target.value} "
                f"to {review.target.value}"
            ),
            diagnostics={
                "btc_momentum_30d": review.btc_momentum,
                "eth_momentum_30d": review.eth_momentum,
                "previous_target": review.previous_target.value,
                "rotation_target": review.target.value,
                "rotation_target_changed": True,
            },
        )


@dataclass(frozen=True)
class RelativeMomentumRotationPlanner:
    """Build the fixed 30-day momentum / seven-day review plan.

    Configuration is exposed for deterministic small-fixture tests. The
    research command uses the fixed defaults and does not expose parameter
    search or CLI tuning.
    """

    lookback_candles: int = DEFAULT_LOOKBACK_CANDLES
    rebalance_candles: int = DEFAULT_REBALANCE_CANDLES

    def __post_init__(self) -> None:
        for name, value in (
            ("lookback_candles", self.lookback_candles),
            ("rebalance_candles", self.rebalance_candles),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def build(
        self,
        btc_rows: Sequence[FeatureRow],
        eth_rows: Sequence[FeatureRow],
        *,
        trading_start_time: int | None = None,
        trading_end_time: int | None = None,
    ) -> RelativeMomentumRotationPlan:
        """Build decisions using only the review candle and prior closes.

        The review schedule is anchored to the first input candle. Each
        isolated research period starts in cash, while pre-period rows remain
        available solely for the momentum lookback.
        """
        self._validate_rows(btc_rows, eth_rows)
        start_time = (
            btc_rows[0].open_time
            if trading_start_time is None
            else trading_start_time
        )
        end_time = (
            btc_rows[-1].available_time
            if trading_end_time is None
            else trading_end_time
        )
        if type(start_time) is not int or type(end_time) is not int:
            raise ValueError("Trading boundaries must be integer timestamps")
        if end_time <= start_time:
            raise ValueError("trading_end_time must be after trading_start_time")
        if start_time < btc_rows[0].open_time:
            raise ValueError("Trading start precedes the feature history")
        if end_time > btc_rows[-1].available_time:
            raise ValueError("Trading end exceeds the feature history")

        current_target = RotationTarget.CASH
        reviews: list[RotationReview] = []
        signals: list[RotationLegSignal] = []
        for index in range(
            self.lookback_candles,
            len(btc_rows),
            self.rebalance_candles,
        ):
            btc_row = btc_rows[index]
            if btc_row.open_time < start_time:
                continue
            if btc_row.open_time >= end_time:
                break
            past_index = index - self.lookback_candles
            btc_momentum = (
                btc_row.close / btc_rows[past_index].close - Decimal("1")
            )
            eth_momentum = (
                eth_rows[index].close / eth_rows[past_index].close
                - Decimal("1")
            )
            target = self._select_target(
                btc_momentum,
                eth_momentum,
                current_target,
            )
            review = RotationReview(
                candle_open_time=btc_row.open_time,
                signal_available_time=btc_row.available_time,
                btc_momentum=btc_momentum,
                eth_momentum=eth_momentum,
                previous_target=current_target,
                target=target,
            )
            reviews.append(review)
            if review.target_changed:
                if current_target is not RotationTarget.CASH:
                    signals.append(RotationLegSignal(
                        symbol=current_target.value,
                        candle_open_time=btc_row.open_time,
                        signal=Signal.SELL,
                        review=review,
                    ))
                if target is not RotationTarget.CASH:
                    signals.append(RotationLegSignal(
                        symbol=target.value,
                        candle_open_time=btc_row.open_time,
                        signal=Signal.BUY,
                        review=review,
                    ))
                current_target = target

        return RelativeMomentumRotationPlan(
            reviews=tuple(reviews),
            leg_signals=tuple(signals),
        )

    @staticmethod
    def _select_target(
        btc_momentum: Decimal,
        eth_momentum: Decimal,
        current_target: RotationTarget,
    ) -> RotationTarget:
        if btc_momentum <= 0 and eth_momentum <= 0:
            return RotationTarget.CASH
        if btc_momentum > eth_momentum:
            return RotationTarget.BTC
        if eth_momentum > btc_momentum:
            return RotationTarget.ETH
        if current_target in {RotationTarget.BTC, RotationTarget.ETH}:
            return current_target
        return RotationTarget.BTC

    def _validate_rows(
        self,
        btc_rows: Sequence[FeatureRow],
        eth_rows: Sequence[FeatureRow],
    ) -> None:
        if len(btc_rows) != len(eth_rows):
            raise ValueError("BTC and ETH histories must have equal length")
        if len(btc_rows) <= self.lookback_candles:
            raise ValueError("Insufficient history for the momentum lookback")
        previous_open_time: int | None = None
        for index, (btc_row, eth_row) in enumerate(zip(btc_rows, eth_rows)):
            btc_timeline = (
                btc_row.open_time,
                btc_row.close_time,
                btc_row.available_time,
            )
            eth_timeline = (
                eth_row.open_time,
                eth_row.close_time,
                eth_row.available_time,
            )
            if btc_timeline != eth_timeline:
                raise ValueError(
                    f"BTC and ETH histories are misaligned at index {index}"
                )
            if previous_open_time is not None and (
                btc_row.open_time - previous_open_time != HOUR_MILLISECONDS
            ):
                raise ValueError(
                    f"Hourly history is not consecutive at index {index}"
                )
            for symbol, row in (("BTC", btc_row), ("ETH", eth_row)):
                if (
                    not isinstance(row.close, Decimal)
                    or not row.close.is_finite()
                    or row.close <= 0
                ):
                    raise ValueError(
                        f"{symbol} close at index {index} must be positive"
                    )
            previous_open_time = btc_row.open_time
