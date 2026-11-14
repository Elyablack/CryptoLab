"""Bounded, chronological strategy-parameter research."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Sequence

from cryptolab.backtesting import BacktestConfig, BacktestEngine, calculate_benchmarks
from cryptolab.backtesting.benchmarks import BenchmarkReport
from cryptolab.backtesting.models import BacktestReport, EquityPoint
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.markets import DEFAULT_EXCHANGE, SPOT_MARKET_TYPE
from cryptolab.market_data.models import Candle
from cryptolab.research.models import (
    ConfigurationEvaluation,
    OptimizationMetrics,
    PeriodBenchmark,
    StrategyOptimizationReport,
    StrategyOptimizationResult,
    WalkForwardEvaluation,
)
from cryptolab.research.parameter_grid import (
    ParameterSet,
    StrategyParameterGrid,
    baseline_parameter_set,
    create_strategy,
)
from cryptolab.research.validation import (
    ChronologicalSplit,
    PeriodRestrictedStrategy,
    ResearchPeriod,
    create_chronological_split,
    create_walk_forward_periods,
)
from cryptolab.strategies.models import Signal


class StrategyOptimizationRunner:
    """Evaluate bounded grids without allowing the test set into selection."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self._config = config or BacktestConfig()

    def run(
        self,
        symbol: str,
        interval: str,
        candles: Sequence[Candle],
        grids: Sequence[StrategyParameterGrid],
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
        as_of_time: int | None = None,
        train_fraction: Decimal = Decimal("0.6"),
        validation_fraction: Decimal = Decimal("0.2"),
        minimum_completed_trades: int = 3,
        max_combinations: int = 100,
        walk_forward_folds: int = 0,
    ) -> StrategyOptimizationReport:
        if not candles:
            raise ValueError("At least one candle is required for optimization")
        if not grids:
            raise ValueError("At least one strategy parameter grid is required")
        if (
            type(minimum_completed_trades) is not int
            or minimum_completed_trades < 0
        ):
            raise ValueError("minimum_completed_trades must be non-negative")

        effective_as_of = (
            candles[-1].close_time + 1 if as_of_time is None else as_of_time
        )
        rows = tuple(FeatureBuilder().build(
            candles,
            interval=interval,
            as_of_time=effective_as_of,
        ))
        split = create_chronological_split(
            rows,
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
        )
        parameter_sets = [
            (grid, grid.combinations(max_combinations)) for grid in grids
        ]
        total_combinations = sum(len(items) for _, items in parameter_sets)
        if total_combinations > max_combinations:
            raise ValueError(
                f"Selected grids contain {total_combinations} combinations; "
                f"global limit is {max_combinations}"
            )

        results = tuple(
            self._optimize_strategy(
                symbol,
                interval,
                rows,
                split,
                grid.strategy_name,
                combinations,
                exchange,
                market_type,
                minimum_completed_trades,
                walk_forward_folds,
            )
            for grid, combinations in parameter_sets
        )
        benchmarks = self._calculate_period_benchmarks(
            candles,
            symbol,
            interval,
            split,
            exchange,
            market_type,
        )
        return StrategyOptimizationReport(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            interval=interval,
            start_time=candles[0].open_time,
            end_time=candles[-1].close_time + 1,
            candle_count=len(candles),
            split=split,
            minimum_completed_trades=minimum_completed_trades,
            config=self._config,
            strategies=results,
            benchmarks=benchmarks,
        )

    def _optimize_strategy(
        self,
        symbol: str,
        interval: str,
        rows: tuple[FeatureRow, ...],
        split: ChronologicalSplit,
        strategy_name: str,
        parameter_sets: Sequence[ParameterSet],
        exchange: str,
        market_type: str,
        minimum_completed_trades: int,
        walk_forward_folds: int,
    ) -> StrategyOptimizationResult:
        evaluations = []
        for parameters in parameter_sets:
            train = self._evaluate(
                parameters,
                split.train,
                symbol,
                interval,
                rows,
                exchange,
                market_type,
            )
            validation = self._evaluate(
                parameters,
                split.validation,
                symbol,
                interval,
                rows,
                exchange,
                market_type,
            )
            eligible = (
                train.completed_trades >= minimum_completed_trades
                and validation.completed_trades >= minimum_completed_trades
            )
            evaluations.append(ConfigurationEvaluation(
                parameters=parameters,
                train=train,
                validation=validation,
                selection_score=self._selection_score(train, validation),
                eligible=eligible,
                validation_rank=0,
            ))

        ordered = sorted(
            evaluations,
            key=lambda item: (
                -item.selection_score,
                item.parameters.identifier,
            ),
        )
        ranked = tuple(
            replace(item, validation_rank=index)
            for index, item in enumerate(ordered, start=1)
        )
        eligible = [item for item in ranked if item.eligible]
        selected = eligible[0].parameters if eligible else None
        baseline = baseline_parameter_set(strategy_name)
        baseline_test = self._evaluate(
            baseline,
            split.test,
            symbol,
            interval,
            rows,
            exchange,
            market_type,
        )
        selected_test = (
            None
            if selected is None
            else self._evaluate(
                selected,
                split.test,
                symbol,
                interval,
                rows,
                exchange,
                market_type,
            )
        )
        walk_forward = (
            ()
            if selected is None
            else tuple(
                WalkForwardEvaluation(
                    train_period=train_period,
                    validation_period=validation_period,
                    train=self._evaluate(
                        selected,
                        train_period,
                        symbol,
                        interval,
                        rows,
                        exchange,
                        market_type,
                    ),
                    validation=self._evaluate(
                        selected,
                        validation_period,
                        symbol,
                        interval,
                        rows,
                        exchange,
                        market_type,
                    ),
                )
                for train_period, validation_period in create_walk_forward_periods(
                    split,
                    rows,
                    walk_forward_folds,
                )
            )
        )
        return StrategyOptimizationResult(
            strategy_name=strategy_name,
            evaluations=ranked,
            baseline_parameters=baseline,
            baseline_test=baseline_test,
            selected_parameters=selected,
            selected_test=selected_test,
            walk_forward=walk_forward,
            warnings=self._warnings(eligible, selected_test),
        )

    def _evaluate(
        self,
        parameters: ParameterSet,
        period: ResearchPeriod,
        symbol: str,
        interval: str,
        rows: Sequence[FeatureRow],
        exchange: str,
        market_type: str,
    ) -> OptimizationMetrics:
        strategy = PeriodRestrictedStrategy(create_strategy(parameters), period)
        report = BacktestEngine(self._config).run(
            strategy,
            symbol,
            interval,
            rows,
            exchange=exchange,
            market_type=market_type,
            as_of_time=period.end_time,
        )
        return _metrics_from_backtest(report, period)

    @staticmethod
    def _selection_score(
        train: OptimizationMetrics,
        validation: OptimizationMetrics,
    ) -> Decimal:
        """Balance validation return, drawdown, and cross-period stability."""
        instability = abs(train.total_return - validation.total_return)
        return (
            validation.total_return
            + Decimal("0.5") * validation.maximum_drawdown
            - Decimal("0.25") * instability
        )

    @staticmethod
    def _warnings(
        eligible: Sequence[ConfigurationEvaluation],
        selected_test: OptimizationMetrics | None,
    ) -> tuple[str, ...]:
        warnings = []
        if not eligible:
            warnings.append(
                "No configuration met the minimum completed-trade criterion; "
                "no optimized configuration was selected or tested."
            )
        else:
            selected = eligible[0]
            if (
                selected.train.total_return > 0
                and selected.validation.total_return < 0
            ) or (
                selected.train.total_return < 0
                and selected.validation.total_return > 0
            ):
                warnings.append(
                    "Selected train and validation returns have opposite signs."
                )
            if selected.validation.completed_trades < 5:
                warnings.append(
                    "Selected validation evidence contains fewer than five trades."
                )
        if selected_test is not None and selected_test.completed_trades < 5:
            warnings.append(
                "Holdout evidence contains fewer than five completed trades; "
                "conclusions are weak."
            )
        return tuple(warnings)

    def _calculate_period_benchmarks(
        self,
        candles: Sequence[Candle],
        symbol: str,
        interval: str,
        split: ChronologicalSplit,
        exchange: str,
        market_type: str,
    ) -> tuple[PeriodBenchmark, ...]:
        result = []
        for period in (split.train, split.validation, split.test):
            period_candles = candles[period.start_index : period.end_index]
            for report in calculate_benchmarks(
                period_candles,
                symbol,
                interval,
                config=self._config,
                exchange=exchange,
                market_type=market_type,
                as_of_time=period.end_time,
            ):
                result.append(PeriodBenchmark(
                    period=period,
                    name=report.name,
                    metrics=_metrics_from_benchmark(report),
                ))
        return tuple(result)


def _metrics_from_backtest(
    report: BacktestReport,
    period: ResearchPeriod,
) -> OptimizationMetrics:
    points = tuple(
        point
        for point in report.equity_curve
        if period.start_time <= point.candle_open_time < period.end_time
    )
    if len(points) != period.candle_count:
        raise RuntimeError("Backtest equity points are not aligned to the period")
    profits = [trade.pnl for trade in report.completed_trades if trade.pnl > 0]
    losses = [trade.pnl for trade in report.completed_trades if trade.pnl < 0]
    profit_factor = (
        None
        if not losses
        else sum(profits, Decimal("0")) / abs(sum(losses, Decimal("0")))
    )
    exposed = sum(point.base_balance > 0 for point in points)
    return OptimizationMetrics(
        total_return=report.total_return,
        final_equity=report.final_equity,
        total_pnl=report.total_pnl,
        realized_pnl=report.realized_pnl,
        unrealized_pnl=report.unrealized_pnl,
        maximum_drawdown=_maximum_drawdown(points, report.config.initial_cash),
        fills=len(report.fills),
        completed_trades=len(report.completed_trades),
        win_rate=report.win_rate,
        profit_factor=profit_factor,
        fees_paid=report.fees_paid,
        exposure_percentage=Decimal(exposed) / Decimal(len(points)),
        skipped_buys=sum(
            item.decision.signal is Signal.BUY
            for item in report.skipped_decisions
        ),
        skipped_sells=sum(
            item.decision.signal is Signal.SELL
            for item in report.skipped_decisions
        ),
        has_open_position=report.final_state.base_balance > 0,
    )


def _metrics_from_benchmark(report: BenchmarkReport) -> OptimizationMetrics:
    return OptimizationMetrics(
        total_return=report.total_return,
        final_equity=report.final_equity,
        total_pnl=report.total_pnl,
        realized_pnl=report.realized_pnl,
        unrealized_pnl=report.unrealized_pnl,
        maximum_drawdown=report.maximum_equity_drawdown,
        fills=1 if report.has_open_position else 0,
        completed_trades=report.completed_trade_count,
        win_rate=report.win_rate,
        profit_factor=None,
        fees_paid=report.fees_paid,
        exposure_percentage=report.exposure_percentage,
        skipped_buys=0,
        skipped_sells=0,
        has_open_position=report.has_open_position,
    )


def _maximum_drawdown(
    points: Sequence[EquityPoint],
    initial_cash: Decimal,
) -> Decimal:
    peak = initial_cash
    maximum = Decimal("0")
    for point in points:
        peak = max(peak, point.equity)
        drawdown = (point.equity - peak) / peak
        maximum = min(maximum, drawdown)
    return maximum
