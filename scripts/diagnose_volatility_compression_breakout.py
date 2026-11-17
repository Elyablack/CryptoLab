#!/usr/bin/env python3
"""Diagnose Volatility Compression Breakout v1 without using Studied Test."""

from __future__ import annotations

import argparse
from collections import Counter
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
    calculate_benchmarks,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.market_data.models import Candle
from cryptolab.research.validation import PeriodRestrictedStrategy, ResearchPeriod
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.strategies import (
    Signal,
    StrategyDecision,
    StrategyRunner,
    VolatilityCompressionBreakoutStrategy,
)
START_TIME = 1_698_796_800_000
DEVELOPMENT_END_TIME = 1_755_612_000_000
VALIDATION_END_TIME = 1_774_548_000_000
DEVELOPMENT_CANDLES = 15_782
VALIDATION_CANDLES = 5_260
EXCHANGE = "bybit"
MARKET_TYPE = "spot"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
STOP_LOSS_RATE = Decimal("0.03")
HORIZONS_HOURS = (6, 12, 24, 48)
HOUR_MILLISECONDS = 3_600_000


@dataclass(frozen=True)
class DurationSummary:
    mean_hours: Decimal | None
    median_hours: Decimal | None
    minimum_hours: Decimal | None
    maximum_hours: Decimal | None


@dataclass(frozen=True)
class ExecutionBridge:
    """Explain the transition from research BUY events to closed trades."""

    buy_events: int
    next_open_eligible: int
    buy_fills: int
    skipped_while_holding: int
    skipped_without_next_candle: int
    other_skipped: tuple[tuple[str, int], ...]
    completed_trades: int
    open_positions_at_end: int
    open_position_duration_hours: Decimal | None


@dataclass(frozen=True)
class PortfolioDiagnostics:
    period: ResearchPeriod
    total_return: Decimal
    cash_return: Decimal
    buy_and_hold_return: Decimal
    exposure: Decimal
    durations: DurationSummary
    bridge: ExecutionBridge


@dataclass(frozen=True)
class EventHorizonSummary:
    period_name: str
    event_group: str
    horizon_hours: int
    observations: int
    overlap_clusters: int
    observations_in_overlapping_clusters: int
    maximum_cluster_size: int
    mean_return: Decimal | None
    median_return: Decimal | None
    positive_share: Decimal | None
    mean_mfe: Decimal | None
    mean_mae: Decimal | None


@dataclass(frozen=True)
class JointFilterDiagnostics:
    period_name: str
    compressed_breakouts: int
    bullish_candle_pass: int
    atr_pass: int
    rvol_pass: int
    both_pass: int
    atr_only: int
    rvol_only: int
    neither_pass: int
    independent_atr_failures: int
    independent_rvol_failures: int
    atr_first_atr_rejections: int
    atr_first_rvol_rejections: int
    rvol_first_rvol_rejections: int
    rvol_first_atr_rejections: int
    all_v1_buy_conditions: int


@dataclass(frozen=True)
class DiagnosticStudy:
    candles: tuple[Candle, ...]
    portfolios: tuple[PortfolioDiagnostics, ...]
    event_summaries: tuple[EventHorizonSummary, ...]
    joint_filters: tuple[JointFilterDiagnostics, ...]


def fixed_config() -> BacktestConfig:
    """Preserve the approved v1 costs and protective-stop assumptions."""
    return BacktestConfig(stop_loss_rate=STOP_LOSS_RATE)


def run_diagnostics(database_path: Path) -> DiagnosticStudy:
    """Run Development/Validation diagnostics and never load Studied Test."""
    if not database_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {database_path}")
    repository = SQLiteRepository(database_path)
    quality = DataQualityChecker(repository).check(
        SYMBOL,
        INTERVAL,
        START_TIME,
        VALIDATION_END_TIME,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=VALIDATION_END_TIME,
    )
    if not quality.is_valid:
        raise ValueError(
            "Diagnostic range failed data quality: "
            f"missing={quality.missing_candles}, "
            f"unexpected={quality.unexpected_candles}, "
            f"duplicates={quality.duplicate_candles}, "
            f"invalid={quality.invalid_candles}"
        )
    candles = tuple(repository.get_candles_by_range(
        SYMBOL,
        INTERVAL,
        START_TIME,
        VALIDATION_END_TIME,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
    ))
    expected = DEVELOPMENT_CANDLES + VALIDATION_CANDLES
    if len(candles) != expected:
        raise RuntimeError(f"Expected {expected} candles, got {len(candles)}")
    rows = tuple(FeatureBuilder().build(
        candles,
        interval=INTERVAL,
        as_of_time=VALIDATION_END_TIME,
    ))
    periods = (
        ResearchPeriod(
            name="development",
            start_index=0,
            end_index=DEVELOPMENT_CANDLES,
            start_time=START_TIME,
            end_time=DEVELOPMENT_END_TIME,
        ),
        ResearchPeriod(
            name="validation",
            start_index=DEVELOPMENT_CANDLES,
            end_index=expected,
            start_time=DEVELOPMENT_END_TIME,
            end_time=VALIDATION_END_TIME,
        ),
    )
    strategy = VolatilityCompressionBreakoutStrategy()
    decisions = tuple(StrategyRunner([strategy]).run(
        SYMBOL,
        INTERVAL,
        rows,
        as_of_time=VALIDATION_END_TIME,
    ))
    index_by_time = {row.open_time: index for index, row in enumerate(rows)}

    portfolios = []
    summaries = []
    filters = []
    for period in periods:
        period_decisions = tuple(
            item
            for item in decisions
            if period.start_time <= item.candle_open_time < period.end_time
        )
        report = BacktestEngine(fixed_config()).run(
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
        portfolios.append(PortfolioDiagnostics(
            period=period,
            total_return=report.total_return,
            cash_return=cash.total_return,
            buy_and_hold_return=buy_and_hold.total_return,
            exposure=report.exposure_percentage,
            durations=calculate_duration_summary(report),
            bridge=calculate_execution_bridge(
                report,
                period_decisions,
                index_by_time,
                period,
            ),
        ))
        group_indices = event_groups(period_decisions, index_by_time)
        for group_name, event_indices in group_indices.items():
            for horizon in HORIZONS_HOURS:
                summaries.append(summarize_event_horizon(
                    rows,
                    event_indices,
                    period,
                    horizon,
                    group_name,
                ))
        filters.append(calculate_joint_filter_diagnostics(
            period.name,
            period_decisions,
        ))

    return DiagnosticStudy(
        candles=candles,
        portfolios=tuple(portfolios),
        event_summaries=tuple(summaries),
        joint_filters=tuple(filters),
    )


def calculate_duration_summary(report: BacktestReport) -> DurationSummary:
    """Summarize completed holding times; open positions are kept separate."""
    durations = tuple(
        Decimal(_exit_time(trade.exit_fill) - trade.entry_fill.execution_open_time)
        / Decimal(HOUR_MILLISECONDS)
        for trade in report.completed_trades
    )
    if not durations:
        return DurationSummary(None, None, None, None)
    return DurationSummary(
        mean_hours=sum(durations, Decimal("0")) / Decimal(len(durations)),
        median_hours=Decimal(median(durations)),
        minimum_hours=min(durations),
        maximum_hours=max(durations),
    )


def calculate_execution_bridge(
    report: BacktestReport,
    decisions: Sequence[StrategyDecision],
    index_by_time: dict[int, int],
    period: ResearchPeriod,
) -> ExecutionBridge:
    """Reconcile BUY market events with fills, skips, and open positions."""
    buy_decisions = tuple(item for item in decisions if item.signal is Signal.BUY)
    next_open_eligible = sum(
        index_by_time[item.candle_open_time] + 1 < period.end_index
        for item in buy_decisions
    )
    buy_fills = tuple(
        fill
        for fill in report.fills
        if isinstance(fill, SimulatedFill) and fill.side is Signal.BUY
    )
    buy_skips = tuple(
        item for item in report.skipped_decisions if item.decision.signal is Signal.BUY
    )
    reasons = Counter(item.reason for item in buy_skips)
    holding_reason = (
        "BUY ignored because the portfolio already holds the base asset"
    )
    no_next_reason = "no next candle is available for execution"
    holding = reasons.pop(holding_reason, 0)
    no_next = reasons.pop(no_next_reason, 0)
    if len(buy_fills) + len(buy_skips) != len(buy_decisions):
        raise RuntimeError("BUY event reconciliation is incomplete")
    open_positions = int(report.final_state.base_balance > 0)
    if len(buy_fills) - report.completed_trade_count != open_positions:
        raise RuntimeError("BUY fills do not reconcile with completed trades")
    open_duration = None
    if open_positions:
        entry = buy_fills[-1].execution_open_time
        open_duration = Decimal(period.end_time - entry) / Decimal(
            HOUR_MILLISECONDS
        )
    return ExecutionBridge(
        buy_events=len(buy_decisions),
        next_open_eligible=next_open_eligible,
        buy_fills=len(buy_fills),
        skipped_while_holding=holding,
        skipped_without_next_candle=no_next,
        other_skipped=tuple(sorted(reasons.items())),
        completed_trades=report.completed_trade_count,
        open_positions_at_end=open_positions,
        open_position_duration_hours=open_duration,
    )


def event_groups(
    decisions: Sequence[StrategyDecision],
    index_by_time: dict[int, int],
) -> dict[str, tuple[int, ...]]:
    """Return nested, past-only event groups for descriptive comparison."""
    evaluable = tuple(
        item for item in decisions if "new_upper_breakout" in item.diagnostics
    )
    return {
        "all_new_upper_breakouts": tuple(
            index_by_time[item.candle_open_time]
            for item in evaluable
            if item.diagnostics["new_upper_breakout"] is True
        ),
        "compressed_new_upper_breakouts": tuple(
            index_by_time[item.candle_open_time]
            for item in evaluable
            if item.diagnostics["new_upper_breakout"] is True
            and item.diagnostics["compression_confirmed"] is True
        ),
        "all_v1_buy_conditions": tuple(
            index_by_time[item.candle_open_time]
            for item in evaluable
            if item.signal is Signal.BUY
        ),
    }


def summarize_event_horizon(
    rows: Sequence[FeatureRow],
    signal_indices: Sequence[int],
    period: ResearchPeriod,
    horizon_hours: int,
    event_group: str,
) -> EventHorizonSummary:
    """Measure next-open forward paths and retain explicit overlap metadata."""
    if horizon_hours not in HORIZONS_HOURS:
        raise ValueError(f"Unsupported predeclared horizon: {horizon_hours}")
    observations: list[tuple[int, Decimal, Decimal, Decimal]] = []
    for signal_index in signal_indices:
        entry_index = signal_index + 1
        end_index = entry_index + horizon_hours
        if entry_index < period.start_index or end_index > period.end_index:
            continue
        entry_price = rows[entry_index].open
        future = rows[entry_index:end_index]
        outcome = future[-1].close / entry_price - 1
        mfe = max(row.high for row in future) / entry_price - 1
        mae = min(row.low for row in future) / entry_price - 1
        observations.append((entry_index, outcome, mfe, mae))

    returns = [item[1] for item in observations]
    mfes = [item[2] for item in observations]
    maes = [item[3] for item in observations]
    cluster_sizes = _overlap_cluster_sizes(
        [item[0] for item in observations],
        horizon_hours,
    )
    return EventHorizonSummary(
        period_name=period.name,
        event_group=event_group,
        horizon_hours=horizon_hours,
        observations=len(observations),
        overlap_clusters=len(cluster_sizes),
        observations_in_overlapping_clusters=sum(
            size for size in cluster_sizes if size > 1
        ),
        maximum_cluster_size=max(cluster_sizes, default=0),
        mean_return=_mean(returns),
        median_return=(None if not returns else Decimal(median(returns))),
        positive_share=(
            None
            if not returns
            else Decimal(sum(value > 0 for value in returns))
            / Decimal(len(returns))
        ),
        mean_mfe=_mean(mfes),
        mean_mae=_mean(maes),
    )


def calculate_joint_filter_diagnostics(
    period_name: str,
    decisions: Sequence[StrategyDecision],
) -> JointFilterDiagnostics:
    """Show order-independent and order-dependent ATR/RVOL rejection counts."""
    events = tuple(
        item
        for item in decisions
        if item.diagnostics.get("new_upper_breakout") is True
        and item.diagnostics.get("compression_confirmed") is True
    )
    atr_pass = sum(
        item.diagnostics["atr_expansion_confirmed"] is True for item in events
    )
    rvol_pass = sum(item.diagnostics["volume_confirmed"] is True for item in events)
    both = sum(
        item.diagnostics["atr_expansion_confirmed"] is True
        and item.diagnostics["volume_confirmed"] is True
        for item in events
    )
    total = len(events)
    return JointFilterDiagnostics(
        period_name=period_name,
        compressed_breakouts=total,
        bullish_candle_pass=sum(
            item.diagnostics["bullish_candle_confirmed"] is True for item in events
        ),
        atr_pass=atr_pass,
        rvol_pass=rvol_pass,
        both_pass=both,
        atr_only=atr_pass - both,
        rvol_only=rvol_pass - both,
        neither_pass=total - atr_pass - rvol_pass + both,
        independent_atr_failures=total - atr_pass,
        independent_rvol_failures=total - rvol_pass,
        atr_first_atr_rejections=total - atr_pass,
        atr_first_rvol_rejections=atr_pass - both,
        rvol_first_rvol_rejections=total - rvol_pass,
        rvol_first_atr_rejections=rvol_pass - both,
        all_v1_buy_conditions=sum(item.signal is Signal.BUY for item in events),
    )


def format_diagnostics(study: DiagnosticStudy) -> str:
    lines = [
        "CryptoLab — Volatility Compression Breakout v1 diagnostics",
        "=" * 61,
        (
            f"Dataset used: {EXCHANGE}/{MARKET_TYPE} {SYMBOL} {INTERVAL}, "
            f"candles={len(study.candles)}, Development + Validation only"
        ),
        "Studied Test was not loaded or evaluated.",
        "Predeclared event horizons: 6h, 12h, 24h, 48h from next open.",
        "",
        "Portfolio diagnostics",
        (
            "period          return       cash   buy_hold exposure "
            "mean_hold median_hold min_hold max_hold"
        ),
    ]
    for item in study.portfolios:
        duration = item.durations
        lines.append(
            f"{item.period.name:<14} "
            f"{_percent(item.total_return):>9} "
            f"{_percent(item.cash_return):>9} "
            f"{_percent(item.buy_and_hold_return):>10} "
            f"{_percent(item.exposure):>8} "
            f"{_hours(duration.mean_hours):>9} "
            f"{_hours(duration.median_hours):>11} "
            f"{_hours(duration.minimum_hours):>8} "
            f"{_hours(duration.maximum_hours):>8}"
        )

    lines.extend(["", "BUY-event execution bridge"])
    for item in study.portfolios:
        bridge = item.bridge
        lines.append(
            f"  {item.period.name}: events={bridge.buy_events}, "
            f"next_open_eligible={bridge.next_open_eligible}, "
            f"fills={bridge.buy_fills}, "
            f"skipped_holding={bridge.skipped_while_holding}, "
            f"skipped_no_next={bridge.skipped_without_next_candle}, "
            f"completed={bridge.completed_trades}, "
            f"open_end={bridge.open_positions_at_end}, "
            f"open_duration={_hours(bridge.open_position_duration_hours)}"
        )
        for reason, count in bridge.other_skipped:
            lines.append(f"    other skip: {reason} = {count}")

    lines.extend([
        "",
        "Descriptive next-open event study (not executable-strategy returns)",
        (
            "period       group                            h    N clusters "
            "overlap_N max_cluster      mean    median positive       MFE       MAE"
        ),
    ])
    for item in study.event_summaries:
        lines.append(
            f"{item.period_name:<12} "
            f"{item.event_group:<32} "
            f"{item.horizon_hours:>2} "
            f"{item.observations:>4} "
            f"{item.overlap_clusters:>8} "
            f"{item.observations_in_overlapping_clusters:>9} "
            f"{item.maximum_cluster_size:>11} "
            f"{_optional_percent(item.mean_return):>9} "
            f"{_optional_percent(item.median_return):>9} "
            f"{_optional_percent(item.positive_share):>8} "
            f"{_optional_percent(item.mean_mfe):>9} "
            f"{_optional_percent(item.mean_mae):>9}"
        )

    lines.extend(["", "ATR expansion / RVOL on compressed new breakouts"])
    for item in study.joint_filters:
        lines.extend([
            (
                f"  {item.period_name}: events={item.compressed_breakouts}, "
                f"bullish={item.bullish_candle_pass}, ATR_pass={item.atr_pass}, "
                f"RVOL_pass={item.rvol_pass}, both={item.both_pass}, "
                f"ATR_only={item.atr_only}, RVOL_only={item.rvol_only}, "
                f"neither={item.neither_pass}, v1_BUY={item.all_v1_buy_conditions}"
            ),
            (
                "    independent failures: "
                f"ATR={item.independent_atr_failures}, "
                f"RVOL={item.independent_rvol_failures}"
            ),
            (
                "    sequential ATR->RVOL: "
                f"ATR rejects={item.atr_first_atr_rejections}, "
                f"then RVOL rejects={item.atr_first_rvol_rejections}"
            ),
            (
                "    sequential RVOL->ATR: "
                f"RVOL rejects={item.rvol_first_rvol_rejections}, "
                f"then ATR rejects={item.rvol_first_atr_rejections}"
            ),
        ])

    lines.extend([
        "",
        "Interpretation limits",
        "  Event groups are nested, not independent treatment/control samples.",
        "  clusters are connected overlap groups, not statistical independence.",
        "  Forward event outcomes ignore portfolio state, fees, stops, and exits.",
        "  Validation has already been reused in CryptoLab research.",
        "  No horizon or threshold is selected from this diagnostic output.",
    ])
    return "\n".join(lines)


def _overlap_cluster_sizes(
    entry_indices: Sequence[int],
    horizon_steps: int,
) -> tuple[int, ...]:
    if not entry_indices:
        return ()
    sizes: list[int] = []
    cluster_end = -1
    for entry_index in sorted(entry_indices):
        if not sizes or entry_index >= cluster_end:
            sizes.append(1)
            cluster_end = entry_index + horizon_steps
        else:
            sizes[-1] += 1
            cluster_end = max(cluster_end, entry_index + horizon_steps)
    return tuple(sizes)


def _exit_time(fill: object) -> int:
    if isinstance(fill, SimulatedFill):
        return fill.execution_open_time
    if isinstance(fill, ProtectiveStopFill):
        return fill.trigger_candle_open_time
    raise TypeError(f"Unsupported exit fill: {type(fill).__name__}")


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _percent(value: Decimal) -> str:
    return f"{value * 100:.4f}%"


def _optional_percent(value: Decimal | None) -> str:
    return "N/A" if value is None else _percent(value)


def _hours(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:.2f}h"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose Volatility Compression Breakout v1 on Development and "
            "Validation only"
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/market_data.db"),
    )
    args = parser.parse_args(argv)
    try:
        study = run_diagnostics(args.database)
    except Exception as error:
        parser.exit(1, f"Diagnostics failed: {error}\n")
    print(format_diagnostics(study))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
