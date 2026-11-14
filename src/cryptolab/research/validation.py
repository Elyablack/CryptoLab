"""Chronological research splits and period-restricted strategy evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import Strategy, make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


@dataclass(frozen=True)
class ResearchPeriod:
    """One half-open evaluation period over a shared feature timeline."""

    name: str
    start_index: int
    end_index: int
    start_time: int
    end_time: int

    @property
    def candle_count(self) -> int:
        return self.end_index - self.start_index


@dataclass(frozen=True)
class ChronologicalSplit:
    """Development, validation, and untouched test periods."""

    train: ResearchPeriod
    validation: ResearchPeriod
    test: ResearchPeriod


def create_chronological_split(
    rows: Sequence[FeatureRow],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
    minimum_train_candles: int = 52,
) -> ChronologicalSplit:
    """Split rows by count while retaining exact half-open timestamps."""
    if not rows:
        raise ValueError("Cannot split an empty feature timeline")
    for name, value in (
        ("train_fraction", train_fraction),
        ("validation_fraction", validation_fraction),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a finite positive Decimal")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train and validation fractions must sum to less than 1")
    if type(minimum_train_candles) is not int or minimum_train_candles <= 0:
        raise ValueError("minimum_train_candles must be a positive integer")

    count = len(rows)
    train_end = int(Decimal(count) * train_fraction)
    validation_end = train_end + int(Decimal(count) * validation_fraction)
    if train_end < minimum_train_candles:
        raise ValueError(
            f"Insufficient history: training period has {train_end} candles; "
            f"at least {minimum_train_candles} are required"
        )
    if validation_end - train_end < 2 or count - validation_end < 2:
        raise ValueError(
            "Insufficient history: validation and test periods each require "
            "at least two candles"
        )

    timeline_end = rows[-1].available_time
    return ChronologicalSplit(
        train=_period("train", rows, 0, train_end),
        validation=_period("validation", rows, train_end, validation_end),
        test=_period("test", rows, validation_end, count, timeline_end),
    )


def create_walk_forward_periods(
    split: ChronologicalSplit,
    rows: Sequence[FeatureRow],
    folds: int,
) -> tuple[tuple[ResearchPeriod, ResearchPeriod], ...]:
    """Create expanding-train folds inside the pre-test timeline.

    These folds are diagnostics for the already selected configuration. They
    never alter selection and never inspect the test period.
    """
    if type(folds) is not int or folds < 0:
        raise ValueError("walk-forward folds must be a non-negative integer")
    if folds == 0:
        return ()
    available = split.validation.candle_count
    if available < folds:
        raise ValueError("Not enough validation candles for walk-forward folds")

    base, remainder = divmod(available, folds)
    result = []
    fold_start = split.validation.start_index
    for index in range(folds):
        fold_size = base + (1 if index < remainder else 0)
        fold_end = fold_start + fold_size
        training = _period(
            f"walk_forward_{index + 1}_train",
            rows,
            0,
            fold_start,
        )
        validation = _period(
            f"walk_forward_{index + 1}_validation",
            rows,
            fold_start,
            fold_end,
        )
        result.append((training, validation))
        fold_start = fold_end
    return tuple(result)


@dataclass(frozen=True)
class PeriodRestrictedStrategy:
    """Allow signals only inside a period while retaining prior warm-up rows."""

    strategy: Strategy
    period: ResearchPeriod

    @property
    def name(self) -> str:
        return self.strategy.name

    @property
    def minimum_history(self) -> int:
        return self.strategy.minimum_history

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: Sequence[FeatureRow],
    ) -> StrategyDecision:
        current = history[-1]
        if current.open_time < self.period.start_time:
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=current,
                signal=Signal.HOLD,
                explanation="Pre-period feature warm-up; trading is disabled",
                diagnostics={"research_period": self.period.name},
            )
        if current.open_time >= self.period.end_time:
            raise RuntimeError("Strategy received a row beyond its research period")
        return self.strategy.evaluate(symbol, interval, history)


def _period(
    name: str,
    rows: Sequence[FeatureRow],
    start_index: int,
    end_index: int,
    end_time: int | None = None,
) -> ResearchPeriod:
    return ResearchPeriod(
        name=name,
        start_index=start_index,
        end_index=end_index,
        start_time=rows[start_index].open_time,
        end_time=rows[end_index].open_time if end_time is None else end_time,
    )
