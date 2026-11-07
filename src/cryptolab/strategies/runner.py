"""Sequential, look-ahead-safe strategy evaluation."""

from collections.abc import Sequence
from typing import overload

from cryptolab.features.models import FeatureRow
from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.strategies.base import Strategy, make_decision
from cryptolab.strategies.models import Signal, StrategyDecision


class _HistoryView(Sequence[FeatureRow]):
    """Read-only, constant-size view over an immutable feature-row tuple."""

    __slots__ = ("_rows", "_indices")

    def __init__(
        self,
        rows: tuple[FeatureRow, ...],
        indices: range,
    ) -> None:
        self._rows = rows
        self._indices = indices

    def __len__(self) -> int:
        return len(self._indices)

    @overload
    def __getitem__(self, index: int) -> FeatureRow: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[FeatureRow]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> FeatureRow | Sequence[FeatureRow]:
        if isinstance(index, slice):
            return _HistoryView(self._rows, self._indices[index])

        return self._rows[self._indices[index]]


class StrategyRunner:
    """Evaluate strategies over feature prefixes in deterministic order."""

    def __init__(self, strategies: Sequence[Strategy]) -> None:
        if not strategies:
            raise ValueError("At least one strategy is required")

        names = [strategy.name for strategy in strategies]
        if len(names) != len(set(names)):
            raise ValueError("Strategy names must be unique")

        for strategy in strategies:
            if strategy.minimum_history <= 0:
                raise ValueError("Strategy minimum_history must be positive")

        self._strategies = tuple(strategies)

    def run(
        self,
        symbol: str,
        interval: str,
        rows: Sequence[FeatureRow],
        *,
        as_of_time: int | None = None,
    ) -> list[StrategyDecision]:
        """Return one decision per eligible row and strategy.

        Each strategy receives a read-only sequence view ending at the current
        row, so future feature rows are never visible and prefixes are not
        copied. ``as_of_time`` deterministically excludes rows that were not
        available yet.
        """
        self._validate_rows(interval, rows)
        eligible_rows = tuple(
            row
            for row in rows
            if as_of_time is None or row.available_time <= as_of_time
        )
        decisions: list[StrategyDecision] = []

        for index, row in enumerate(eligible_rows):
            history = _HistoryView(eligible_rows, range(index + 1))
            for strategy in self._strategies:
                if len(history) < strategy.minimum_history:
                    decision = make_decision(
                        strategy_name=strategy.name,
                        symbol=symbol,
                        interval=interval,
                        row=row,
                        signal=Signal.HOLD,
                        explanation=(
                            "Warm-up: "
                            f"{len(history)}/{strategy.minimum_history} rows"
                        ),
                        diagnostics={
                            "available_history": len(history),
                            "required_history": strategy.minimum_history,
                        },
                    )
                else:
                    decision = strategy.evaluate(
                        symbol,
                        interval,
                        history,
                    )

                self._validate_decision(
                    decision,
                    strategy,
                    symbol,
                    interval,
                    row,
                )
                decisions.append(decision)

        return decisions

    @staticmethod
    def _validate_rows(
        interval: str,
        rows: Sequence[FeatureRow],
    ) -> None:
        interval_milliseconds = get_interval_milliseconds(interval)
        previous_open_time: int | None = None

        for index, row in enumerate(rows):
            if row.open_time % interval_milliseconds != 0:
                raise ValueError(
                    f"Feature row at index {index} is not interval-aligned"
                )
            if (
                previous_open_time is not None
                and row.open_time - previous_open_time
                != interval_milliseconds
            ):
                raise ValueError(
                    f"Feature row at index {index} is not consecutive"
                )
            if row.close_time != row.open_time + interval_milliseconds - 1:
                raise ValueError(
                    f"Feature row at index {index} has invalid close_time"
                )
            if row.available_time != row.close_time + 1:
                raise ValueError(
                    f"Feature row at index {index} has invalid available_time"
                )
            previous_open_time = row.open_time

    @staticmethod
    def _validate_decision(
        decision: StrategyDecision,
        strategy: Strategy,
        symbol: str,
        interval: str,
        row: FeatureRow,
    ) -> None:
        if decision.strategy_name != strategy.name:
            raise ValueError("Strategy returned an inconsistent strategy name")
        if decision.symbol != symbol or decision.interval != interval:
            raise ValueError("Strategy returned inconsistent market metadata")
        if decision.candle_open_time != row.open_time:
            raise ValueError("Strategy returned an inconsistent candle timestamp")
        if decision.signal_available_time != row.available_time:
            raise ValueError("Strategy returned an inconsistent availability time")
        if decision.reference_price != row.close:
            raise ValueError("Strategy returned an inconsistent reference price")
