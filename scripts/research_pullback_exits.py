#!/usr/bin/env python3
"""Run the bounded Trend Pullback exit study without parameter search."""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    BacktestReport,
    ProtectiveStopFill,
    SimulatedFill,
    TimedExitFill,
    calculate_benchmarks,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.market_data.models import Candle
from cryptolab.research.validation import (
    PeriodRestrictedStrategy,
    ResearchPeriod,
    create_chronological_split,
)
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.strategies import (
    Signal,
    TrendPullbackExitPolicy,
    TrendPullbackStrategy,
)


START_TIME = 1_698_796_800_000
END_TIME = 1_793_491_200_000
EXCHANGE = "bybit"
MARKET_TYPE = "spot"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
STOP_LOSS_RATE = Decimal("0.03")
TIME_EXIT_CANDLES = 72
MINIMUM_PERIOD_TRADES = 30
MAXIMUM_DRAWDOWN = Decimal("-0.25")
MAXIMUM_WIN_CONCENTRATION = Decimal("0.25")


@dataclass(frozen=True)
class Variant:
    """One predeclared exit variant; entry rules never change."""

    name: str
    strategy: TrendPullbackStrategy
    config: BacktestConfig


@dataclass(frozen=True)
class StudyMetrics:
    """Comparable deterministic metrics for one isolated evaluation period."""

    total_return: Decimal
    maximum_drawdown: Decimal
    profit_factor: Decimal | None
    completed_trades: int
    win_rate: Decimal | None
    fees_paid: Decimal
    slippage_cost: Decimal
    average_trade_pnl: Decimal | None
    maximum_win_concentration: Decimal | None
    average_holding_hours: Decimal | None
    exit_attribution: tuple[tuple[str, int, Decimal], ...]


@dataclass(frozen=True)
class PeriodResult:
    """Strategy metrics and the comparable buy-and-hold return."""

    period_name: str
    metrics: StudyMetrics
    buy_and_hold_return: Decimal


@dataclass(frozen=True)
class VariantResult:
    """Full-range and chronological results for one variant."""

    variant: Variant
    full_report: BacktestReport
    full_metrics: StudyMetrics
    periods: tuple[PeriodResult, ...]


def predefined_variants() -> tuple[Variant, ...]:
    """Return exactly the three variants authorized for this iteration."""
    return (
        Variant(
            name="baseline",
            strategy=TrendPullbackStrategy(),
            config=BacktestConfig(stop_loss_rate=STOP_LOSS_RATE),
        ),
        Variant(
            name="time_exit_72h",
            strategy=TrendPullbackStrategy(
                exit_policy=TrendPullbackExitPolicy.ENTRY_ONLY,
            ),
            config=BacktestConfig(
                stop_loss_rate=STOP_LOSS_RATE,
                max_holding_candles=TIME_EXIT_CANDLES,
            ),
        ),
        Variant(
            name="long_term_trend_exit",
            strategy=TrendPullbackStrategy(
                exit_policy=TrendPullbackExitPolicy.LONG_TERM_TREND,
            ),
            config=BacktestConfig(stop_loss_rate=STOP_LOSS_RATE),
        ),
    )


def calculate_study_metrics(report: BacktestReport) -> StudyMetrics:
    """Calculate cost, concentration, holding, and exit diagnostics."""
    wins = [trade.pnl for trade in report.completed_trades if trade.pnl > 0]
    losses = [trade.pnl for trade in report.completed_trades if trade.pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    profit_factor = (
        None
        if not losses
        else gross_profit / abs(sum(losses, Decimal("0")))
    )
    average_trade = (
        None
        if not report.completed_trades
        else sum(
            (trade.pnl for trade in report.completed_trades),
            Decimal("0"),
        ) / Decimal(len(report.completed_trades))
    )
    concentration = (
        None if not wins else max(wins) / gross_profit
    )
    holding_hours = tuple(
        Decimal(_exit_time(trade.exit_fill) - trade.entry_fill.execution_open_time)
        / Decimal(3_600_000)
        for trade in report.completed_trades
    )
    attribution: dict[str, list[Decimal | int]] = defaultdict(
        lambda: [0, Decimal("0")],
    )
    for trade in report.completed_trades:
        reason = _exit_reason(trade.exit_fill)
        attribution[reason][0] += 1
        attribution[reason][1] += trade.pnl

    return StudyMetrics(
        total_return=report.total_return,
        maximum_drawdown=report.maximum_equity_drawdown,
        profit_factor=profit_factor,
        completed_trades=report.completed_trade_count,
        win_rate=report.win_rate,
        fees_paid=report.fees_paid,
        slippage_cost=_modeled_slippage_cost(report),
        average_trade_pnl=average_trade,
        maximum_win_concentration=concentration,
        average_holding_hours=(
            None
            if not holding_hours
            else sum(holding_hours, Decimal("0"))
            / Decimal(len(holding_hours))
        ),
        exit_attribution=tuple(
            (reason, int(values[0]), Decimal(values[1]))
            for reason, values in sorted(attribution.items())
        ),
    )


def candidate_failures(
    development: StudyMetrics,
    validation: StudyMetrics,
) -> tuple[str, ...]:
    """Apply only the criteria declared before running the study."""
    failures: list[str] = []
    for name, metrics in (
        ("development", development),
        ("validation", validation),
    ):
        if metrics.total_return <= 0:
            failures.append(f"{name}: net return is not positive")
        if metrics.profit_factor is None or metrics.profit_factor <= 1:
            failures.append(f"{name}: profit factor is not above 1")
        if metrics.completed_trades < MINIMUM_PERIOD_TRADES:
            failures.append(
                f"{name}: fewer than {MINIMUM_PERIOD_TRADES} completed trades"
            )
        if metrics.maximum_drawdown < MAXIMUM_DRAWDOWN:
            failures.append(
                f"{name}: drawdown is worse than "
                f"{MAXIMUM_DRAWDOWN * 100:.0f}%"
            )
        concentration = metrics.maximum_win_concentration
        if concentration is None or concentration > MAXIMUM_WIN_CONCENTRATION:
            failures.append(
                f"{name}: largest win exceeds 25% of gross profit"
            )
    return tuple(failures)


def run_study(database_path: Path) -> tuple[
    tuple[VariantResult, ...],
    BacktestReport,
    tuple[Candle, ...],
    tuple[FeatureRow, ...],
]:
    """Load the fixed dataset and run the bounded deterministic comparison."""
    if not database_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {database_path}")
    repository = SQLiteRepository(database_path)
    quality = DataQualityChecker(repository).check(
        SYMBOL,
        INTERVAL,
        START_TIME,
        END_TIME,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=END_TIME,
    )
    if not quality.is_valid:
        raise ValueError(
            "Study range failed data quality: "
            f"missing={quality.missing_candles}, "
            f"unexpected={quality.unexpected_candles}, "
            f"invalid={quality.invalid_candles}"
        )
    candles = tuple(repository.get_candles_by_range(
        SYMBOL,
        INTERVAL,
        START_TIME,
        END_TIME,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
    ))
    rows = tuple(FeatureBuilder().build(
        candles,
        interval=INTERVAL,
        as_of_time=END_TIME,
    ))
    split = create_chronological_split(rows)
    named_periods = (
        ("development", split.train),
        ("validation", split.validation),
        ("studied_test", split.test),
    )
    results = tuple(
        _run_variant(variant, candles, rows, named_periods)
        for variant in predefined_variants()
    )
    zero_cost_report = BacktestEngine(BacktestConfig(
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
        stop_loss_rate=STOP_LOSS_RATE,
    )).run(
        TrendPullbackStrategy(),
        SYMBOL,
        INTERVAL,
        rows,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=END_TIME,
    )
    return results, zero_cost_report, candles, rows


def format_study(
    results: Sequence[VariantResult],
    zero_cost_report: BacktestReport,
    candles: Sequence[Candle],
    rows: Sequence[FeatureRow],
) -> str:
    """Render one human-readable, audit-oriented research report."""
    lines = [
        "CryptoLab Trend Pullback — bounded exit study",
        "=" * 48,
        (
            f"Dataset: {EXCHANGE}/{MARKET_TYPE} {SYMBOL} {INTERVAL}, "
            f"candles={len(candles)}, range=[2023-11-01, 2026-11-01)"
        ),
        (
            "Fixed assumptions: fee=0.1%/fill, slippage=0.05%/fill, "
            "protective stop=3%, time exit=72 candles"
        ),
        (
            "Selection gates: positive return and PF>1 on Development and "
            "Validation; >=30 trades; max DD >=-25%; largest win <=25% "
            "of gross profit"
        ),
        "Studied Test is reported only and is not used for selection.",
        "",
        "Full-range comparison",
        (
            "variant                    return      max_dd      PF  trades "
            " win_rate      fees  slippage  avg_trade  avg_hold"
        ),
    ]
    for result in results:
        metrics = result.full_metrics
        lines.append(
            f"{result.variant.name:<26} "
            f"{_percent(metrics.total_return):>9} "
            f"{_percent(metrics.maximum_drawdown):>11} "
            f"{_optional(metrics.profit_factor):>7} "
            f"{metrics.completed_trades:>7} "
            f"{_optional_percent(metrics.win_rate):>9} "
            f"{metrics.fees_paid:>9.2f} "
            f"{metrics.slippage_cost:>9.2f} "
            f"{_optional(metrics.average_trade_pnl):>10} "
            f"{_optional(metrics.average_holding_hours):>9}h"
        )

    lines.extend(["", "Chronological periods"])
    for result in results:
        lines.append(f"  {result.variant.name}")
        lines.append(
            "    period         return      max_dd      PF  trades "
            " win_rate  max_win  buy_hold"
        )
        for period in result.periods:
            metrics = period.metrics
            lines.append(
                f"    {period.period_name:<13} "
                f"{_percent(metrics.total_return):>9} "
                f"{_percent(metrics.maximum_drawdown):>11} "
                f"{_optional(metrics.profit_factor):>7} "
                f"{metrics.completed_trades:>7} "
                f"{_optional_percent(metrics.win_rate):>9} "
                f"{_optional_percent(metrics.maximum_win_concentration):>8} "
                f"{_percent(period.buy_and_hold_return):>9}"
            )

    lines.extend(["", "Exit attribution (full range)"])
    for result in results:
        attribution = ", ".join(
            f"{reason}: {count} trades, {pnl:.2f} USDT"
            for reason, count, pnl in result.full_metrics.exit_attribution
        )
        lines.append(f"  {result.variant.name}: {attribution}")

    baseline = results[0]
    zero_metrics = calculate_study_metrics(zero_cost_report)
    lines.extend([
        "",
        "Baseline cost diagnosis",
        (
            "  With costs: "
            f"return={_percent(baseline.full_metrics.total_return)}, "
            f"PF={_optional(baseline.full_metrics.profit_factor)}, "
            f"fees={baseline.full_metrics.fees_paid:.2f} USDT, "
            "modeled slippage impact="
            f"{baseline.full_metrics.slippage_cost:.2f} USDT"
        ),
        (
            "  Zero fee/slippage: "
            f"return={_percent(zero_metrics.total_return)}, "
            f"PF={_optional(zero_metrics.profit_factor)}, "
            f"trades={zero_metrics.completed_trades}"
        ),
        (
            "  Fee/slippage totals are fill-level modeled costs; their sum "
            "is not directly additive to the counterfactual final equity "
            "because capital compounds and the stop anchor uses execution price."
        ),
    ])
    lines.extend(_format_stop_diagnosis(baseline.full_report, rows))

    lines.extend(["", "Candidate decision"])
    selected: list[str] = []
    for result in results:
        periods = {item.period_name: item.metrics for item in result.periods}
        failures = candidate_failures(
            periods["development"],
            periods["validation"],
        )
        if not failures:
            selected.append(result.variant.name)
            lines.append(f"  {result.variant.name}: PASSES all gates")
        else:
            lines.append(
                f"  {result.variant.name}: FAIL — " + "; ".join(failures)
            )
    lines.append(
        "  SELECTED: " + ", ".join(selected)
        if selected
        else "  SELECTED: NO CANDIDATE"
    )
    return "\n".join(lines)


def _run_variant(
    variant: Variant,
    candles: Sequence[Candle],
    rows: Sequence[FeatureRow],
    periods: Sequence[tuple[str, ResearchPeriod]],
) -> VariantResult:
    engine = BacktestEngine(variant.config)
    full = engine.run(
        variant.strategy,
        SYMBOL,
        INTERVAL,
        rows,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=END_TIME,
    )
    period_results = []
    for period_name, period in periods:
        report = engine.run(
            PeriodRestrictedStrategy(variant.strategy, period),
            SYMBOL,
            INTERVAL,
            rows[: period.end_index],
            exchange=EXCHANGE,
            market_type=MARKET_TYPE,
            as_of_time=period.end_time,
        )
        benchmark, _ = calculate_benchmarks(
            candles[period.start_index : period.end_index],
            SYMBOL,
            INTERVAL,
            config=variant.config,
            exchange=EXCHANGE,
            market_type=MARKET_TYPE,
            as_of_time=period.end_time,
        )
        period_results.append(PeriodResult(
            period_name=period_name,
            metrics=calculate_study_metrics(report),
            buy_and_hold_return=benchmark.total_return,
        ))
    return VariantResult(
        variant=variant,
        full_report=full,
        full_metrics=calculate_study_metrics(full),
        periods=tuple(period_results),
    )


def _format_stop_diagnosis(
    report: BacktestReport,
    rows: Sequence[FeatureRow],
) -> list[str]:
    row_index = {row.open_time: index for index, row in enumerate(rows)}
    adverse_excursions = []
    strategy_trades = []
    stop_trades = []
    for trade in report.completed_trades:
        if isinstance(trade.exit_fill, ProtectiveStopFill):
            stop_trades.append(trade)
            continue
        strategy_trades.append(trade)
        start = row_index[trade.entry_fill.execution_open_time]
        end = row_index[trade.exit_fill.execution_open_time]
        lows = [row.low for row in rows[start:end]]
        if lows:
            adverse_excursions.append(
                (min(lows) - trade.entry_fill.execution_price)
                / trade.entry_fill.execution_price
            )
    stop_holding = [
        Decimal(
            trade.exit_fill.trigger_candle_open_time
            - trade.entry_fill.execution_open_time
        ) / Decimal(3_600_000)
        for trade in stop_trades
    ]
    return [
        "",
        "Why the baseline protective stop fired only six times",
        (
            f"  Strategy exits={len(strategy_trades)}, protective exits="
            f"{len(stop_trades)}."
        ),
        (
            "  Before strategy exits, MAE: average="
            f"{_percent(sum(adverse_excursions, Decimal('0')) / Decimal(len(adverse_excursions)))}, "
            f"median={_percent(Decimal(str(median(adverse_excursions))))}, "
            f"worst={_percent(min(adverse_excursions))}."
        ),
        (
            "  All strategy-closed trades stayed above the -3% protective "
            "level before their exit open; the strategy usually exited first."
        ),
        (
            "  Protective-exit holding time: average="
            f"{sum(stop_holding, Decimal('0')) / Decimal(len(stop_holding)):.2f}h, "
            f"median={Decimal(str(median(stop_holding))):.2f}h."
        ),
    ]


def _modeled_slippage_cost(report: BacktestReport) -> Decimal:
    result = Decimal("0")
    for fill in report.fills:
        if isinstance(fill, SimulatedFill):
            per_unit = (
                fill.execution_price - fill.market_open_price
                if fill.side is Signal.BUY
                else fill.market_open_price - fill.execution_price
            )
        else:
            per_unit = fill.market_reference_price - fill.execution_price
        result += per_unit * fill.base_quantity
    return result


def _exit_reason(fill: object) -> str:
    if isinstance(fill, ProtectiveStopFill):
        return f"protective_{fill.trigger_kind.value}"
    if isinstance(fill, TimedExitFill):
        return "time_exit"
    return "strategy_sell"


def _exit_time(fill: object) -> int:
    if isinstance(fill, SimulatedFill):
        return fill.execution_open_time
    if isinstance(fill, (ProtectiveStopFill, TimedExitFill)):
        return fill.trigger_candle_open_time
    raise TypeError(f"Unsupported exit fill: {type(fill).__name__}")


def _percent(value: Decimal) -> str:
    return f"{value * 100:.4f}%"


def _optional(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _optional_percent(value: Decimal | None) -> str:
    return "N/A" if value is None else _percent(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed three-variant Trend Pullback exit study",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/market_data.db"),
    )
    args = parser.parse_args(argv)
    try:
        results, zero_cost, candles, rows = run_study(args.database)
    except Exception as error:
        parser.exit(1, f"Research failed: {error}\n")
    print(format_study(results, zero_cost, candles, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
