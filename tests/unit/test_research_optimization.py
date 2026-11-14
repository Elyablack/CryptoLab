from dataclasses import replace
from collections.abc import Sequence
from decimal import Decimal

import pytest

from cryptolab.backtesting import BacktestConfig, BacktestEngine
from cryptolab.features.builder import FeatureBuilder
from cryptolab.market_data.models import Candle
from cryptolab.research.models import OptimizationMetrics
from cryptolab.research.optimization import (
    StrategyOptimizationRunner,
    _metrics_from_backtest,
)
from cryptolab.research.optimization_formatter import (
    optimization_report_to_csv,
    optimization_report_to_json,
)
from cryptolab.research.parameter_grid import (
    StrategyParameterGrid,
    create_strategy,
    default_parameter_grids,
    grid_from_mapping,
)
from cryptolab.research.validation import (
    PeriodRestrictedStrategy,
    ResearchPeriod,
    create_chronological_split,
    create_walk_forward_periods,
)
from cryptolab.strategies import Signal, TrendFollowingStrategy
from cryptolab.strategies.base import make_decision
from cryptolab.features.models import FeatureRow


HOUR = 3_600_000


def make_candles(count: int) -> list[Candle]:
    candles = []
    for index in range(count):
        open_time = index * HOUR
        price = Decimal("100") + Decimal(index % 7) / Decimal("10")
        candles.append(Candle(
            open_time=open_time,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("10") + index,
            close_time=open_time + HOUR - 1,
            quote_volume=price * (Decimal("10") + index),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return candles


def trend_grid(*buy_rsi_min: str) -> StrategyParameterGrid:
    return grid_from_mapping(
        TrendFollowingStrategy.name,
        {"buy_rsi_min": list(buy_rsi_min)},
    )


def metrics(total_return: str, trades: int = 3) -> OptimizationMetrics:
    value = Decimal(total_return)
    return OptimizationMetrics(
        total_return=value,
        final_equity=Decimal("1000") * (1 + value),
        total_pnl=Decimal("1000") * value,
        realized_pnl=Decimal("1000") * value,
        unrealized_pnl=Decimal("0"),
        maximum_drawdown=Decimal("-0.02"),
        fills=trades * 2,
        completed_trades=trades,
        win_rate=Decimal("0.5"),
        profit_factor=Decimal("1.2"),
        fees_paid=Decimal("2"),
        exposure_percentage=Decimal("0.4"),
        skipped_buys=0,
        skipped_sells=0,
        has_open_position=False,
    )


def test_default_parameter_grids_are_bounded_deterministic_and_valid() -> None:
    grids = default_parameter_grids()
    first = [grid.combinations(100) for grid in grids]
    second = [grid.combinations(100) for grid in grids]

    assert first == second
    assert [len(items) for items in first] == [32, 16, 16]
    assert sum(len(items) for items in first) == 64
    assert all(create_strategy(item) for items in first for item in items)
    with pytest.raises(ValueError, match="limit is 1"):
        grids[0].combinations(1)


def test_custom_grid_converts_types_and_rejects_invalid_parameters() -> None:
    grid = grid_from_mapping(
        "mean_reversion",
        {"minimum_sma_deviation": ["0.02"], "recovery_window": [3]},
    )
    parameters = grid.combinations(1)[0]

    assert parameters.as_dict() == {
        "minimum_sma_deviation": Decimal("0.02"),
        "recovery_window": 3,
    }
    with pytest.raises(ValueError, match="Unknown parameters"):
        grid_from_mapping("mean_reversion", {"future_leak": [1]})
    with pytest.raises(ValueError, match="requires an integer"):
        grid_from_mapping("mean_reversion", {"recovery_window": ["3"]})


def test_chronological_split_and_walk_forward_are_nonoverlapping() -> None:
    rows = FeatureBuilder().build(
        make_candles(120),
        interval="1h",
        as_of_time=120 * HOUR,
    )
    split = create_chronological_split(rows)

    assert (split.train.candle_count, split.validation.candle_count) == (72, 24)
    assert split.test.candle_count == 24
    assert split.train.end_time == split.validation.start_time
    assert split.validation.end_time == split.test.start_time
    folds = create_walk_forward_periods(split, rows, 3)
    assert [item[1].candle_count for item in folds] == [8, 8, 8]
    assert folds[-1][1].end_time == split.test.start_time


def test_period_gate_uses_earlier_rows_only_as_warmup() -> None:
    rows = FeatureBuilder().build(
        make_candles(70), interval="1h", as_of_time=70 * HOUR
    )
    split = create_chronological_split(
        rows,
        train_fraction=Decimal("0.8"),
        validation_fraction=Decimal("0.1"),
    )

    class AlwaysBuy:
        name = "always_buy"
        minimum_history = 1

        def evaluate(self, symbol, interval, history):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=history[-1],
                signal=Signal.BUY,
                explanation="test",
            )

    gated = PeriodRestrictedStrategy(AlwaysBuy(), split.validation)
    before = gated.evaluate("BTCUSDT", "1h", rows[: split.train.end_index])
    inside = gated.evaluate(
        "BTCUSDT", "1h", rows[: split.validation.start_index + 1]
    )

    assert before.signal is Signal.HOLD
    assert inside.signal is Signal.BUY


def test_future_test_candles_cannot_change_train_or_validation_results() -> None:
    candles = make_candles(120)
    runner = StrategyOptimizationRunner()
    arguments = dict(
        symbol="BTCUSDT",
        interval="1h",
        grids=(trend_grid("50"),),
        as_of_time=120 * HOUR,
        minimum_completed_trades=0,
        max_combinations=1,
    )
    first = runner.run(candles=candles, **arguments)
    changed = list(candles)
    for index in range(96, 120):
        price = Decimal("1000") + index
        changed[index] = replace(
            changed[index],
            open=price,
            high=price + 2,
            low=price - 2,
            close=price,
            quote_volume=price * changed[index].volume,
        )
    second = runner.run(candles=changed, **arguments)

    assert first.strategies[0].evaluations == second.strategies[0].evaluations
    assert first.benchmarks[:4] == second.benchmarks[:4]


def test_test_period_is_not_used_to_select_configuration() -> None:
    class RecordingRunner(StrategyOptimizationRunner):
        def __init__(self) -> None:
            super().__init__(BacktestConfig())
            self.calls = []

        def _evaluate(
            self,
            parameters,
            period,
            symbol,
            interval,
            rows,
            exchange,
            market_type,
        ):
            value = parameters.as_dict()["buy_rsi_min"]
            self.calls.append((period.name, value))
            if period.name == "validation":
                return metrics("0.20" if value == Decimal("45") else "0.01")
            if period.name == "test":
                return metrics("9" if value == Decimal("40") else "-0.50")
            return metrics("0.05")

    runner = RecordingRunner()
    report = runner.run(
        "BTCUSDT",
        "1h",
        make_candles(120),
        (trend_grid("40", "45", "50"),),
        as_of_time=120 * HOUR,
        max_combinations=3,
    )

    result = report.strategies[0]
    assert result.selected_parameters.as_dict()["buy_rsi_min"] == Decimal("45")
    assert ("test", Decimal("40")) not in runner.calls
    assert ("test", Decimal("45")) in runner.calls
    assert ("test", Decimal("50")) in runner.calls  # unchanged baseline


def test_no_eligible_configuration_is_reported_as_inconclusive() -> None:
    report = StrategyOptimizationRunner().run(
        "BTCUSDT",
        "1h",
        make_candles(120),
        (trend_grid("50"),),
        as_of_time=120 * HOUR,
        minimum_completed_trades=1,
        max_combinations=1,
    )

    result = report.strategies[0]
    assert result.selected_parameters is None
    assert result.selected_test is None
    assert "No configuration met" in result.warnings[0]


def test_machine_readable_reports_are_deterministic() -> None:
    report = StrategyOptimizationRunner().run(
        "BTCUSDT",
        "1h",
        make_candles(120),
        (trend_grid("50"),),
        as_of_time=120 * HOUR,
        minimum_completed_trades=0,
        max_combinations=1,
    )

    json_output = optimization_report_to_json(report)
    csv_output = optimization_report_to_csv(report)
    assert json_output == optimization_report_to_json(report)
    assert '"strategy_name": "trend_following"' in json_output
    assert "strategy,rank,eligible,selection_score" in csv_output
    assert "trend_following,1,true" in csv_output


def test_optimization_metrics_reconcile_trades_profit_factor_and_exposure() -> None:
    prices = [Decimal(value) for value in ("100", "100", "110", "100", "90")]
    candles = []
    for index, price in enumerate(prices):
        candles.append(Candle(
            open_time=index * HOUR,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("10"),
            close_time=(index + 1) * HOUR - 1,
            quote_volume=price * 10,
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    rows = FeatureBuilder().build(candles, interval="1h", as_of_time=5 * HOUR)

    class ScriptedStrategy:
        name = "scripted"
        minimum_history = 1
        signals = (Signal.BUY, Signal.SELL, Signal.BUY, Signal.SELL, Signal.HOLD)

        def evaluate(
            self,
            symbol: str,
            interval: str,
            history: Sequence[FeatureRow],
        ):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=history[-1],
                signal=self.signals[len(history) - 1],
                explanation="scripted",
            )

    report = BacktestEngine(BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )).run(ScriptedStrategy(), "BTCUSDT", "1h", rows, as_of_time=5 * HOUR)
    result = _metrics_from_backtest(
        report,
        ResearchPeriod("all", 0, 5, 0, 5 * HOUR),
    )

    assert result.completed_trades == 2
    assert result.realized_pnl == Decimal("-10")
    assert result.total_pnl == result.realized_pnl + result.unrealized_pnl
    assert result.profit_factor == Decimal("100") / Decimal("110")
    assert result.win_rate == Decimal("0.5")
    assert result.exposure_percentage == Decimal("0.4")
    assert result.maximum_drawdown == Decimal("-0.1")


def test_split_rejects_insufficient_history() -> None:
    rows = FeatureBuilder().build(
        make_candles(50), interval="1h", as_of_time=50 * HOUR
    )
    with pytest.raises(ValueError, match="Insufficient history"):
        create_chronological_split(rows)
