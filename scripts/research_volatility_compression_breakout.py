#!/usr/bin/env python3
"""Run the fixed Volatility Compression Breakout v1 research study."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    BacktestReport,
    ProtectiveStopFill,
    SimulatedFill,
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
    BreakoutStrategy,
    Signal,
    StrategyDecision,
    StrategyRunner,
    VolatilityCompressionBreakoutStrategy,
)


START_TIME = 1_698_796_800_000
END_TIME = 1_793_491_200_000
EXCHANGE = "bybit"
MARKET_TYPE = "spot"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
STOP_LOSS_RATE = Decimal("0.03")
MINIMUM_PERIOD_TRADES = 30
MAXIMUM_DRAWDOWN = Decimal("-0.25")
MAXIMUM_WIN_CONCENTRATION = Decimal("0.25")

CONDITION_KEYS = (
    ("compression", "compression_confirmed"),
    ("upper_breakout", "upper_breakout_confirmed"),
    ("new_breakout_event", "new_upper_breakout"),
    ("bullish_candle", "bullish_candle_confirmed"),
    ("atr_expansion", "atr_expansion_confirmed"),
    ("relative_volume", "volume_confirmed"),
)


@dataclass(frozen=True)
class StudyMetrics:
    """Comparable deterministic metrics for one isolated period."""

    final_equity: Decimal
    total_return: Decimal
    maximum_drawdown: Decimal
    profit_factor: Decimal | None
    completed_trades: int
    win_rate: Decimal | None
    fees_paid: Decimal
    slippage_cost: Decimal
    average_trade_pnl: Decimal | None
    maximum_win_concentration: Decimal | None
    exit_attribution: tuple[tuple[str, int, Decimal], ...]


@dataclass(frozen=True)
class SignalDiagnostics:
    """Frequency and compatibility diagnostics for predeclared BUY filters."""

    evaluated_rows: int
    buy_signals: int
    sell_signals: int
    condition_passes: tuple[tuple[str, int], ...]
    sequential_blockers: tuple[tuple[str, int], ...]
    compression_and_expansion: int
    compression_only: int
    expansion_only: int
    neither_compression_nor_expansion: int


@dataclass(frozen=True)
class PeriodResult:
    """Strategy, benchmark, and signal evidence for one period."""

    period: ResearchPeriod
    metrics: StudyMetrics
    buy_and_hold_return: Decimal
    cash_return: Decimal
    diagnostics: SignalDiagnostics
    breakout_reference: StudyMetrics
    zero_cost_metrics: StudyMetrics


@dataclass(frozen=True)
class CompressionBreakoutStudy:
    """Complete fixed study without parameter selection or Test tuning."""

    candles: tuple[Candle, ...]
    rows: tuple[FeatureRow, ...]
    periods: tuple[PeriodResult, ...]
    zero_cost_full_metrics: StudyMetrics
    candidate_failures: tuple[str, ...]

    @property
    def is_candidate(self) -> bool:
        return not self.candidate_failures


def fixed_config(*, zero_cost: bool = False) -> BacktestConfig:
    """Return the immutable execution assumptions approved for this study."""
    return BacktestConfig(
        fee_rate=Decimal("0") if zero_cost else Decimal("0.001"),
        slippage_rate=Decimal("0") if zero_cost else Decimal("0.0005"),
        stop_loss_rate=STOP_LOSS_RATE,
    )


def run_study(database_path: Path) -> CompressionBreakoutStudy:
    """Load validated local history and execute the single fixed hypothesis."""
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
            f"duplicates={quality.duplicate_candles}, "
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
    strategy = VolatilityCompressionBreakoutStrategy()
    if strategy.minimum_history != 158:
        raise RuntimeError("Approved minimum history unexpectedly changed")

    decisions = tuple(StrategyRunner([strategy]).run(
        SYMBOL,
        INTERVAL,
        rows,
        as_of_time=END_TIME,
    ))
    split = create_chronological_split(rows)
    full = ResearchPeriod(
        name="full_range",
        start_index=0,
        end_index=len(rows),
        start_time=START_TIME,
        end_time=END_TIME,
    )
    periods = tuple(
        _run_period(
            period,
            candles,
            rows,
            decisions,
            strategy,
        )
        for period in (
            full,
            ResearchPeriod(
                name="development",
                start_index=split.train.start_index,
                end_index=split.train.end_index,
                start_time=split.train.start_time,
                end_time=split.train.end_time,
            ),
            split.validation,
            ResearchPeriod(
                name="studied_test",
                start_index=split.test.start_index,
                end_index=split.test.end_index,
                start_time=split.test.start_time,
                end_time=split.test.end_time,
            ),
        )
    )
    by_name = {item.period.name: item for item in periods}

    failures = evaluate_candidate(
        by_name["development"].metrics,
        by_name["validation"].metrics,
    )
    return CompressionBreakoutStudy(
        candles=candles,
        rows=rows,
        periods=periods,
        zero_cost_full_metrics=periods[0].zero_cost_metrics,
        candidate_failures=failures,
    )


def evaluate_candidate(
    development: StudyMetrics,
    validation: StudyMetrics,
) -> tuple[str, ...]:
    """Apply only the gates fixed before any candidate backtest was run."""
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
            failures.append(f"{name}: drawdown is worse than -25%")
        if (
            metrics.maximum_win_concentration is None
            or metrics.maximum_win_concentration > MAXIMUM_WIN_CONCENTRATION
        ):
            failures.append(
                f"{name}: largest win exceeds 25% of gross profit"
            )
    return tuple(failures)


def calculate_signal_diagnostics(
    decisions: Sequence[StrategyDecision],
) -> SignalDiagnostics:
    """Count independent filters and first blockers without changing signals."""
    evaluated = [
        item
        for item in decisions
        if "compression_confirmed" in item.diagnostics
    ]
    condition_passes = tuple(
        (
            name,
            sum(item.diagnostics[key] is True for item in evaluated),
        )
        for name, key in CONDITION_KEYS
    )
    blockers = Counter(
        str(item.diagnostics["buy_blocker"])
        for item in evaluated
        if item.diagnostics["buy_blocker"] != "none"
    )
    compatibility = Counter()
    for item in evaluated:
        compression = item.diagnostics["compression_confirmed"] is True
        expansion = item.diagnostics["atr_expansion_confirmed"] is True
        if compression and expansion:
            compatibility["both"] += 1
        elif compression:
            compatibility["compression_only"] += 1
        elif expansion:
            compatibility["expansion_only"] += 1
        else:
            compatibility["neither"] += 1

    return SignalDiagnostics(
        evaluated_rows=len(evaluated),
        buy_signals=sum(item.signal is Signal.BUY for item in decisions),
        sell_signals=sum(item.signal is Signal.SELL for item in decisions),
        condition_passes=condition_passes,
        sequential_blockers=tuple(sorted(blockers.items())),
        compression_and_expansion=compatibility["both"],
        compression_only=compatibility["compression_only"],
        expansion_only=compatibility["expansion_only"],
        neither_compression_nor_expansion=compatibility["neither"],
    )


def calculate_metrics(report: BacktestReport) -> StudyMetrics:
    """Derive trade and cost metrics from the authoritative backtest report."""
    wins = [trade.pnl for trade in report.completed_trades if trade.pnl > 0]
    losses = [trade.pnl for trade in report.completed_trades if trade.pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    profit_factor = (
        None if not losses else gross_profit / abs(sum(losses, Decimal("0")))
    )
    average_trade = (
        None
        if not report.completed_trades
        else sum(
            (trade.pnl for trade in report.completed_trades),
            Decimal("0"),
        ) / Decimal(report.completed_trade_count)
    )
    concentration = None if not wins else max(wins) / gross_profit
    attribution: dict[str, tuple[int, Decimal]] = {}
    for trade in report.completed_trades:
        reason = (
            f"protective_{trade.exit_fill.trigger_kind.value}"
            if isinstance(trade.exit_fill, ProtectiveStopFill)
            else "strategy_sell"
        )
        count, pnl = attribution.get(reason, (0, Decimal("0")))
        attribution[reason] = (count + 1, pnl + trade.pnl)

    return StudyMetrics(
        final_equity=report.final_equity,
        total_return=report.total_return,
        maximum_drawdown=report.maximum_equity_drawdown,
        profit_factor=profit_factor,
        completed_trades=report.completed_trade_count,
        win_rate=report.win_rate,
        fees_paid=report.fees_paid,
        slippage_cost=_modeled_slippage_cost(report),
        average_trade_pnl=average_trade,
        maximum_win_concentration=concentration,
        exit_attribution=tuple(
            (name, count, pnl)
            for name, (count, pnl) in sorted(attribution.items())
        ),
    )


def format_study(study: CompressionBreakoutStudy) -> str:
    """Render the complete reproducible evidence and gate decision."""
    lines = [
        "CryptoLab — Volatility Compression Breakout v1",
        "=" * 51,
        (
            f"Dataset: {EXCHANGE}/{MARKET_TYPE} {SYMBOL} {INTERVAL}, "
            f"candles={len(study.candles)}, range=[2023-11-01, 2026-11-01)"
        ),
        (
            "Fixed parameters: baseline=120h, compression=24h, "
            "compression_ratio<=0.70, ATR expansion>=1.25, RVOL>=1.25, "
            "exit channel=12h, protective stop=3%"
        ),
        (
            "Execution: next-open, initial=1000 USDT, fee=0.1%/fill, "
            "adverse slippage=0.05%/fill"
        ),
        (
            "Candidate gates: positive return and PF>1 on Development and "
            "Validation; >=30 trades; max DD >=-25%; largest win <=25% "
            "of gross profit"
        ),
        "Studied Test is descriptive only and is not used for selection.",
        "",
        "Performance",
        (
            "period          return     final_eq      max_dd      PF trades "
            " win_rate      fees  slippage  avg_trade max_win  buy_hold cash"
        ),
    ]
    for result in study.periods:
        metrics = result.metrics
        lines.append(
            f"{result.period.name:<14} "
            f"{_percent(metrics.total_return):>9} "
            f"{metrics.final_equity:>12.4f} "
            f"{_percent(metrics.maximum_drawdown):>11} "
            f"{_optional(metrics.profit_factor):>7} "
            f"{metrics.completed_trades:>6} "
            f"{_optional_percent(metrics.win_rate):>9} "
            f"{metrics.fees_paid:>9.2f} "
            f"{metrics.slippage_cost:>9.2f} "
            f"{_optional(metrics.average_trade_pnl):>10} "
            f"{_optional_percent(metrics.maximum_win_concentration):>8} "
            f"{_percent(result.buy_and_hold_return):>9} "
            f"{_percent(result.cash_return):>5}"
        )

    lines.extend(["", "Signal diagnostics"])
    for result in study.periods:
        diagnostics = result.diagnostics
        lines.append(
            f"  {result.period.name}: evaluated={diagnostics.evaluated_rows}, "
            f"BUY={diagnostics.buy_signals}, SELL={diagnostics.sell_signals}"
        )
        lines.append(
            "    independent passes: "
            + ", ".join(
                f"{name}={count}"
                for name, count in diagnostics.condition_passes
            )
        )
        lines.append(
            "    first blockers: "
            + ", ".join(
                f"{name}={count}"
                for name, count in diagnostics.sequential_blockers
            )
        )
        lines.append(
            "    compression/expansion: "
            f"both={diagnostics.compression_and_expansion}, "
            f"compression_only={diagnostics.compression_only}, "
            f"expansion_only={diagnostics.expansion_only}, "
            "neither="
            f"{diagnostics.neither_compression_nor_expansion}"
        )

    lines.extend([
        "",
        "Existing BreakoutStrategy reference",
        "  Same fee, slippage, all-in sizing, and 3% protective stop; "
        "reference strategy parameters are unchanged.",
        "period          return      max_dd      PF trades",
    ])
    for result in study.periods:
        metrics = result.breakout_reference
        lines.append(
            f"{result.period.name:<14} "
            f"{_percent(metrics.total_return):>9} "
            f"{_percent(metrics.maximum_drawdown):>11} "
            f"{_optional(metrics.profit_factor):>7} "
            f"{metrics.completed_trades:>6}"
        )

    full = study.periods[0]
    lines.extend(["", "Exit attribution (full range)"])
    for reason, count, pnl in full.metrics.exit_attribution:
        lines.append(f"  {reason}: {count} trades, {pnl:.2f} USDT")

    lines.extend(["", "Cost diagnosis"])
    for result in study.periods:
        metrics = result.metrics
        zero = result.zero_cost_metrics
        lines.append(
            f"  {result.period.name}: with_costs="
            f"{_percent(metrics.total_return)}, zero_cost="
            f"{_percent(zero.total_return)}, fees={metrics.fees_paid:.2f}, "
            f"slippage={metrics.slippage_cost:.2f}, "
            f"PF={_optional(metrics.profit_factor)}/"
            f"{_optional(zero.profit_factor)}"
        )
    lines.extend(["", "Candidate decision"])
    if study.candidate_failures:
        lines.extend(f"  FAIL: {failure}" for failure in study.candidate_failures)
        lines.append("  RESULT: NO_CANDIDATE")
    else:
        lines.append("  All predeclared gates passed")
        lines.append("  RESULT: CANDIDATE")
    return "\n".join(lines)


def _run_period(
    period: ResearchPeriod,
    candles: Sequence[Candle],
    rows: Sequence[FeatureRow],
    decisions: Sequence[StrategyDecision],
    strategy: VolatilityCompressionBreakoutStrategy,
) -> PeriodResult:
    report = BacktestEngine(fixed_config()).run(
        PeriodRestrictedStrategy(strategy, period),
        SYMBOL,
        INTERVAL,
        rows[: period.end_index],
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=period.end_time,
    )
    breakout_reference_report = BacktestEngine(fixed_config()).run(
        PeriodRestrictedStrategy(BreakoutStrategy(), period),
        SYMBOL,
        INTERVAL,
        rows[: period.end_index],
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=period.end_time,
    )
    zero_cost_report = BacktestEngine(fixed_config(zero_cost=True)).run(
        PeriodRestrictedStrategy(strategy, period),
        SYMBOL,
        INTERVAL,
        rows[: period.end_index],
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=period.end_time,
    )
    buy_and_hold, cash = calculate_benchmarks(
        candles[period.start_index : period.end_index],
        SYMBOL,
        INTERVAL,
        config=fixed_config(),
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=period.end_time,
    )
    period_decisions = tuple(
        item
        for item in decisions
        if period.start_time <= item.candle_open_time < period.end_time
    )
    return PeriodResult(
        period=period,
        metrics=calculate_metrics(report),
        buy_and_hold_return=buy_and_hold.total_return,
        cash_return=cash.total_return,
        diagnostics=calculate_signal_diagnostics(period_decisions),
        breakout_reference=calculate_metrics(breakout_reference_report),
        zero_cost_metrics=calculate_metrics(zero_cost_report),
    )


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


def _percent(value: Decimal) -> str:
    return f"{value * 100:.4f}%"


def _optional(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _optional_percent(value: Decimal | None) -> str:
    return "N/A" if value is None else _percent(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed Volatility Compression Breakout v1 study",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/market_data.db"),
    )
    args = parser.parse_args(argv)
    try:
        study = run_study(args.database)
    except Exception as error:
        parser.exit(1, f"Research failed: {error}\n")
    print(format_study(study))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
