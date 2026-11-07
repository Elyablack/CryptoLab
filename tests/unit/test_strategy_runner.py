from dataclasses import replace
from decimal import Decimal

import pytest

from cryptolab.features.models import FeatureRow
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal
from cryptolab.strategies.runner import StrategyRunner


HOUR_MILLISECONDS = 3_600_000


def make_rows(count: int) -> list[FeatureRow]:
    rows = []
    for index in range(count):
        open_time = index * HOUR_MILLISECONDS
        rows.append(
            FeatureRow(
                open_time=open_time,
                close_time=open_time + HOUR_MILLISECONDS - 1,
                available_time=open_time + HOUR_MILLISECONDS,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10"),
                simple_return=Decimal("0"),
                log_return=Decimal("0"),
                sma20=Decimal("100"),
                sma50=Decimal("100"),
                ema20=Decimal("100"),
                rsi14=Decimal("50"),
                atr14=Decimal("2"),
                rvol20=Decimal("1"),
            )
        )
    return rows


class SpyStrategy:
    def __init__(self, name: str = "spy", minimum_history: int = 1) -> None:
        self.name = name
        self.minimum_history = minimum_history
        self.history_lengths: list[int] = []

    def evaluate(
        self,
        symbol: str,
        interval: str,
        history: tuple[FeatureRow, ...],
    ):
        self.history_lengths.append(len(history))
        return make_decision(
            strategy_name=self.name,
            symbol=symbol,
            interval=interval,
            row=history[-1],
            signal=Signal.HOLD,
            explanation="spy",
        )


def test_runner_never_exposes_future_rows_to_strategy() -> None:
    strategy = SpyStrategy()
    rows = make_rows(5)

    decisions = StrategyRunner([strategy]).run("BTCUSDT", "1h", rows)

    assert strategy.history_lengths == [1, 2, 3, 4, 5]
    assert [decision.candle_open_time for decision in decisions] == [
        row.open_time for row in rows
    ]


def test_runner_emits_explicit_warm_up_holds() -> None:
    strategy = SpyStrategy(minimum_history=3)

    decisions = StrategyRunner([strategy]).run(
        "BTCUSDT",
        "1h",
        make_rows(4),
    )

    assert [decision.signal for decision in decisions[:2]] == [
        Signal.HOLD,
        Signal.HOLD,
    ]
    assert decisions[0].explanation.startswith("Warm-up")
    assert strategy.history_lengths == [3, 4]


def test_runner_respects_strategy_and_row_order() -> None:
    first = SpyStrategy(name="first")
    second = SpyStrategy(name="second")

    decisions = StrategyRunner([first, second]).run(
        "BTCUSDT",
        "1h",
        make_rows(2),
    )

    assert [decision.strategy_name for decision in decisions] == [
        "first",
        "second",
        "first",
        "second",
    ]


def test_runner_as_of_time_excludes_unavailable_rows() -> None:
    rows = make_rows(5)

    decisions = StrategyRunner([SpyStrategy()]).run(
        "BTCUSDT",
        "1h",
        rows,
        as_of_time=rows[2].available_time,
    )

    assert len(decisions) == 3
    assert decisions[-1].candle_open_time == rows[2].open_time


def test_runner_is_deterministic() -> None:
    rows = make_rows(5)
    runner = StrategyRunner([SpyStrategy()])

    first = runner.run("BTCUSDT", "1h", rows)
    second = runner.run("BTCUSDT", "1h", rows)

    assert first == second


def test_runner_rejects_invalid_timeline() -> None:
    rows = make_rows(3)
    rows[2] = replace(
        rows[2],
        open_time=rows[2].open_time + HOUR_MILLISECONDS,
        close_time=rows[2].close_time + HOUR_MILLISECONDS,
        available_time=rows[2].available_time + HOUR_MILLISECONDS,
    )

    with pytest.raises(ValueError, match="not consecutive"):
        StrategyRunner([SpyStrategy()]).run("BTCUSDT", "1h", rows)

    invalid_availability = make_rows(1)
    invalid_availability[0] = replace(
        invalid_availability[0],
        available_time=invalid_availability[0].close_time,
    )
    with pytest.raises(ValueError, match="invalid available_time"):
        StrategyRunner([SpyStrategy()]).run(
            "BTCUSDT",
            "1h",
            invalid_availability,
        )


def test_runner_rejects_empty_or_duplicate_strategy_set() -> None:
    with pytest.raises(ValueError, match="At least one"):
        StrategyRunner([])

    with pytest.raises(ValueError, match="unique"):
        StrategyRunner([SpyStrategy(), SpyStrategy()])
