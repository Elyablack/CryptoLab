#!/usr/bin/env python3
"""Run the fixed BTC/ETH relative-momentum rotation study."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    BenchmarkReport,
    FixedFractionPositionSizer,
    LegacyAllInAllOutSizer,
    PortfolioBacktestInput,
    PortfolioBacktestReport,
    PositionSizer,
    SimulatedFill,
    calculate_benchmarks,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.market_data.models import Candle
from cryptolab.research.validation import (
    ResearchPeriod,
    create_chronological_split,
)
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.strategies import (
    RelativeMomentumRotationPlan,
    RelativeMomentumRotationPlanner,
    RotationTarget,
    Signal,
)


START_TIME = 1_698_796_800_000
END_TIME = 1_793_491_200_000
EXCHANGE = "bybit"
MARKET_TYPE = "spot"
BTC_SYMBOL = "BTCUSDT"
ETH_SYMBOL = "ETHUSDT"
INTERVAL = "1h"
INITIAL_CASH = Decimal("1000")
PRIMARY_EXPOSURE = Decimal("1")
RISK_SCENARIO_EXPOSURE = Decimal("0.5")
MINIMUM_DEVELOPMENT_REVIEWS = 30
MINIMUM_VALIDATION_REVIEWS = 10
MINIMUM_TARGET_CHANGES = 5
MAXIMUM_DRAWDOWN = Decimal("-0.25")
MAXIMUM_WIN_CONCENTRATION = Decimal("0.25")
DAY_MILLISECONDS = 86_400_000


@dataclass(frozen=True)
class PerformanceMetrics:
    """Comparable period metrics for a strategy or benchmark."""

    final_equity: Decimal
    total_return: Decimal
    cagr: Decimal
    maximum_drawdown: Decimal
    profit_factor: Decimal | None
    completed_trades: int
    win_rate: Decimal | None
    fees_paid: Decimal
    slippage_cost: Decimal
    time_exposure: Decimal
    capital_exposure: Decimal
    weekly_reviews: int | None = None
    target_changes: int | None = None
    asset_rotations: int | None = None
    largest_win_concentration: Decimal | None = None
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")


@dataclass(frozen=True)
class NamedMetrics:
    name: str
    metrics: PerformanceMetrics


@dataclass(frozen=True)
class RotationRun:
    """One isolated portfolio simulation plus its synchronized signal plan."""

    report: PortfolioBacktestReport
    plan: RelativeMomentumRotationPlan
    metrics: PerformanceMetrics


@dataclass(frozen=True)
class PeriodStudy:
    """Primary rotation, risk scenario, and references for one period."""

    period: ResearchPeriod
    primary: RotationRun
    half_exposure: RotationRun
    benchmarks: tuple[NamedMetrics, ...]


@dataclass(frozen=True)
class CriterionCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RotationStudy:
    """Complete fixed study result over aligned BTC and ETH histories."""

    btc_candles: tuple[Candle, ...]
    eth_candles: tuple[Candle, ...]
    periods: tuple[PeriodStudy, ...]
    criteria: tuple[CriterionCheck, ...]

    @property
    def is_candidate(self) -> bool:
        return all(check.passed for check in self.criteria)


def run_study(database_path: Path) -> RotationStudy:
    """Load validated SQLite history and run the predeclared experiment."""
    if not database_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {database_path}")
    repository = SQLiteRepository(database_path)
    btc_candles = _load_validated_candles(repository, BTC_SYMBOL)
    eth_candles = _load_validated_candles(repository, ETH_SYMBOL)
    if [item.open_time for item in btc_candles] != [
        item.open_time for item in eth_candles
    ]:
        raise ValueError("BTC and ETH candle timelines are not aligned")

    builder = FeatureBuilder()
    btc_rows = tuple(builder.build(
        btc_candles,
        interval=INTERVAL,
        as_of_time=END_TIME,
    ))
    eth_rows = tuple(builder.build(
        eth_candles,
        interval=INTERVAL,
        as_of_time=END_TIME,
    ))
    split = create_chronological_split(btc_rows)
    full = ResearchPeriod(
        name="full_range",
        start_index=0,
        end_index=len(btc_rows),
        start_time=START_TIME,
        end_time=END_TIME,
    )
    named_periods = (
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
    periods = tuple(
        _run_period(
            period,
            btc_candles,
            eth_candles,
            btc_rows,
            eth_rows,
        )
        for period in named_periods
    )
    by_name = {item.period.name: item for item in periods}
    criteria = evaluate_candidate(
        by_name["development"],
        by_name["validation"],
    )
    return RotationStudy(
        btc_candles=btc_candles,
        eth_candles=eth_candles,
        periods=periods,
        criteria=criteria,
    )


def evaluate_candidate(
    development: PeriodStudy,
    validation: PeriodStudy,
) -> tuple[CriterionCheck, ...]:
    """Apply only the acceptance gates fixed before inspecting ETH results."""
    dev = development.primary.metrics
    val = validation.primary.metrics
    equal_weight = next(
        item.metrics
        for item in validation.benchmarks
        if item.name == "equal_weight_btc_eth"
    )
    checks: list[CriterionCheck] = []
    for label, metrics, minimum_reviews in (
        ("Development", dev, MINIMUM_DEVELOPMENT_REVIEWS),
        ("Validation", val, MINIMUM_VALIDATION_REVIEWS),
    ):
        checks.extend((
            CriterionCheck(
                f"{label}: positive net return",
                metrics.total_return > 0,
                _percent(metrics.total_return),
            ),
            CriterionCheck(
                f"{label}: max drawdown >= -25%",
                metrics.maximum_drawdown >= MAXIMUM_DRAWDOWN,
                _percent(metrics.maximum_drawdown),
            ),
            CriterionCheck(
                f"{label}: minimum weekly reviews",
                (metrics.weekly_reviews or 0) >= minimum_reviews,
                f"{metrics.weekly_reviews}/{minimum_reviews}",
            ),
            CriterionCheck(
                f"{label}: minimum target changes",
                (metrics.target_changes or 0) >= MINIMUM_TARGET_CHANGES,
                f"{metrics.target_changes}/{MINIMUM_TARGET_CHANGES}",
            ),
            CriterionCheck(
                f"{label}: largest win <= 25% gross profit",
                metrics.largest_win_concentration is not None
                and metrics.largest_win_concentration
                <= MAXIMUM_WIN_CONCENTRATION,
                _optional_percent(metrics.largest_win_concentration),
            ),
        ))

    dominated = (
        val.total_return < equal_weight.total_return
        and val.maximum_drawdown < equal_weight.maximum_drawdown
    )
    checks.append(CriterionCheck(
        "Validation: not dominated by equal-weight return and drawdown",
        not dominated,
        (
            f"rotation={_percent(val.total_return)}/"
            f"{_percent(val.maximum_drawdown)}, equal_weight="
            f"{_percent(equal_weight.total_return)}/"
            f"{_percent(equal_weight.maximum_drawdown)}"
        ),
    ))
    return tuple(checks)


def calculate_rotation_metrics(
    report: PortfolioBacktestReport,
    plan: RelativeMomentumRotationPlan,
    period: ResearchPeriod,
) -> PerformanceMetrics:
    """Calculate accounting, cost, exposure, and decision diagnostics."""
    points = tuple(
        point
        for point in report.equity_curve
        if period.start_time <= point.candle_open_time < period.end_time
    )
    if not points:
        raise ValueError(f"No equity points in period {period.name}")
    if any(point.quote_balance < 0 for point in points):
        raise RuntimeError("Rotation produced a negative quote balance")
    if any(len(point.positions) > 1 for point in points):
        raise RuntimeError("Rotation held more than one active asset")
    intent_ids = [item.intent_id for item in report.order_intents]
    if len(intent_ids) != len(set(intent_ids)):
        raise RuntimeError("Rotation produced duplicate order intents")

    wins = [item.pnl for item in report.completed_trades if item.pnl > 0]
    losses = [item.pnl for item in report.completed_trades if item.pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = Decimal("Infinity")
    else:
        profit_factor = None
    win_rate = (
        None
        if not report.completed_trades
        else Decimal(len(wins)) / Decimal(len(report.completed_trades))
    )
    reviews = tuple(
        item
        for item in plan.reviews
        if period.start_time <= item.candle_open_time < period.end_time
        and item.signal_available_time < period.end_time
    )
    target_changes = sum(item.target_changed for item in reviews)
    assets = {RotationTarget.BTC, RotationTarget.ETH}
    rotations = sum(
        item.target_changed
        and item.previous_target in assets
        and item.target in assets
        for item in reviews
    )
    invested_points = sum(bool(point.positions) for point in points)
    capital_exposure = sum(
        (
            sum(
                (position.market_value for position in point.positions),
                Decimal("0"),
            ) / point.equity
            if point.equity > 0
            else Decimal("0")
        )
        for point in points
    ) / Decimal(len(points))
    final_equity = report.final_equity
    total_return = (final_equity - INITIAL_CASH) / INITIAL_CASH
    return PerformanceMetrics(
        final_equity=final_equity,
        total_return=total_return,
        cagr=_cagr(final_equity, period),
        maximum_drawdown=_maximum_drawdown(
            (point.equity for point in points),
        ),
        profit_factor=profit_factor,
        completed_trades=len(report.completed_trades),
        win_rate=win_rate,
        fees_paid=sum((fill.fee for fill in report.fills), Decimal("0")),
        slippage_cost=_modeled_slippage_cost(report),
        time_exposure=Decimal(invested_points) / Decimal(len(points)),
        capital_exposure=capital_exposure,
        weekly_reviews=len(reviews),
        target_changes=target_changes,
        asset_rotations=rotations,
        largest_win_concentration=(
            None if not wins else max(wins) / gross_profit
        ),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
    )


def equal_weight_benchmark(
    btc_candles: Sequence[Candle],
    eth_candles: Sequence[Candle],
    period: ResearchPeriod,
    config: BacktestConfig,
) -> PerformanceMetrics:
    """Buy BTC and ETH 50/50 at the first open and mark at closes."""
    if not btc_candles or len(btc_candles) != len(eth_candles):
        raise ValueError("Equal-weight benchmark requires aligned histories")
    allocation = config.initial_cash / Decimal("2")
    quantities: list[Decimal] = []
    fees = Decimal("0")
    slippage = Decimal("0")
    for candle in (btc_candles[0], eth_candles[0]):
        notional = allocation / (Decimal("1") + config.fee_rate)
        fee = allocation - notional
        execution_price = candle.open * (
            Decimal("1") + config.slippage_rate
        )
        quantity = notional / execution_price
        quantities.append(quantity)
        fees += fee
        slippage += (execution_price - candle.open) * quantity
    equities = tuple(
        quantities[0] * btc.close + quantities[1] * eth.close
        for btc, eth in zip(btc_candles, eth_candles)
    )
    final_equity = equities[-1]
    return PerformanceMetrics(
        final_equity=final_equity,
        total_return=(final_equity - config.initial_cash) / config.initial_cash,
        cagr=_cagr(final_equity, period),
        maximum_drawdown=_maximum_drawdown(equities),
        profit_factor=None,
        completed_trades=0,
        win_rate=None,
        fees_paid=fees,
        slippage_cost=slippage,
        time_exposure=Decimal("1"),
        capital_exposure=Decimal("1"),
    )


def format_study(study: RotationStudy) -> str:
    """Render the complete audit-oriented experiment result."""
    lines = [
        "CryptoLab BTC/ETH Relative Momentum Rotation — Iteration 2",
        "=" * 62,
        (
            f"Dataset: {EXCHANGE}/{MARKET_TYPE} BTCUSDT+ETHUSDT {INTERVAL}, "
            f"candles={len(study.btc_candles)} each, "
            "range=[2023-11-01, 2026-11-01)"
        ),
        (
            "Fixed rule: review every 7 days; 30-day close momentum; hold "
            "the stronger positive asset, otherwise USDT; next-open execution."
        ),
        (
            "Costs: fee=0.1%/fill, adverse slippage=0.05%/fill. Primary "
            "allocation=100%; 50% is a diagnostic risk scenario only."
        ),
        "Studied Test is reported but is not used for candidate selection.",
    ]
    for period in study.periods:
        lines.extend(_format_period(period))

    lines.extend(["", "Predeclared candidate criteria"])
    for check in study.criteria:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"  [{status}] {check.name}: {check.detail}")
    lines.append("")
    lines.append(
        "FINAL STATUS: CANDIDATE"
        if study.is_candidate
        else "FINAL STATUS: NO CANDIDATE"
    )
    lines.extend(_format_diagnosis(study))
    return "\n".join(lines)


def _run_period(
    period: ResearchPeriod,
    btc_candles: Sequence[Candle],
    eth_candles: Sequence[Candle],
    btc_rows: Sequence[FeatureRow],
    eth_rows: Sequence[FeatureRow],
) -> PeriodStudy:
    history_btc = tuple(btc_rows[: period.end_index])
    history_eth = tuple(eth_rows[: period.end_index])
    plan = RelativeMomentumRotationPlanner().build(
        history_btc,
        history_eth,
        trading_start_time=period.start_time,
        trading_end_time=period.end_time,
    )
    config = BacktestConfig(initial_cash=INITIAL_CASH)
    primary = _run_rotation(
        period,
        history_btc,
        history_eth,
        plan,
        config,
        LegacyAllInAllOutSizer(),
    )
    half = _run_rotation(
        period,
        history_btc,
        history_eth,
        plan,
        config,
        FixedFractionPositionSizer(RISK_SCENARIO_EXPOSURE),
    )
    period_btc = tuple(btc_candles[period.start_index : period.end_index])
    period_eth = tuple(eth_candles[period.start_index : period.end_index])
    btc_benchmark, cash = calculate_benchmarks(
        period_btc,
        BTC_SYMBOL,
        INTERVAL,
        config=config,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=period.end_time,
    )
    eth_benchmark, _ = calculate_benchmarks(
        period_eth,
        ETH_SYMBOL,
        INTERVAL,
        config=config,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=period.end_time,
    )
    benchmarks = (
        NamedMetrics(
            "btc_buy_hold",
            _benchmark_metrics(btc_benchmark, period, period_btc),
        ),
        NamedMetrics(
            "eth_buy_hold",
            _benchmark_metrics(eth_benchmark, period, period_eth),
        ),
        NamedMetrics(
            "equal_weight_btc_eth",
            equal_weight_benchmark(period_btc, period_eth, period, config),
        ),
        NamedMetrics(
            "cash_only",
            _benchmark_metrics(cash, period, period_btc),
        ),
    )
    return PeriodStudy(
        period=period,
        primary=primary,
        half_exposure=half,
        benchmarks=benchmarks,
    )


def _run_rotation(
    period: ResearchPeriod,
    btc_rows: tuple[FeatureRow, ...],
    eth_rows: tuple[FeatureRow, ...],
    plan: RelativeMomentumRotationPlan,
    config: BacktestConfig,
    sizer: PositionSizer,
) -> RotationRun:
    report = BacktestEngine(config, position_sizer=sizer).run_portfolio(
        (
            PortfolioBacktestInput(
                strategy=plan.strategy_for(BTC_SYMBOL),
                symbol=BTC_SYMBOL,
                interval=INTERVAL,
                rows=btc_rows,
                exchange=EXCHANGE,
                market_type=MARKET_TYPE,
            ),
            PortfolioBacktestInput(
                strategy=plan.strategy_for(ETH_SYMBOL),
                symbol=ETH_SYMBOL,
                interval=INTERVAL,
                rows=eth_rows,
                exchange=EXCHANGE,
                market_type=MARKET_TYPE,
            ),
        ),
        as_of_time=period.end_time,
    )
    return RotationRun(
        report=report,
        plan=plan,
        metrics=calculate_rotation_metrics(report, plan, period),
    )


def _load_validated_candles(
    repository: SQLiteRepository,
    symbol: str,
) -> tuple[Candle, ...]:
    quality = DataQualityChecker(repository).check(
        symbol,
        INTERVAL,
        START_TIME,
        END_TIME,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
        as_of_time=END_TIME,
    )
    if not quality.is_valid:
        missing = quality.missing_open_times
        detail = (
            "none"
            if not missing
            else f"{missing[0]}..{missing[-1]}"
        )
        raise ValueError(
            f"{symbol} failed data quality: expected={quality.expected_candles}, "
            f"actual={quality.actual_candles}, missing={quality.missing_candles} "
            f"({detail}), unexpected={quality.unexpected_candles}, "
            f"duplicates={quality.duplicate_candles}, "
            f"invalid={quality.invalid_candles}"
        )
    candles = tuple(repository.get_candles_by_range(
        symbol,
        INTERVAL,
        START_TIME,
        END_TIME,
        exchange=EXCHANGE,
        market_type=MARKET_TYPE,
    ))
    if len(candles) != quality.expected_candles:
        raise RuntimeError(f"{symbol} changed after its quality check")
    return candles


def _benchmark_metrics(
    report: BenchmarkReport,
    period: ResearchPeriod,
    candles: Sequence[Candle],
) -> PerformanceMetrics:
    slippage = Decimal("0")
    if report.exposure_percentage > 0:
        notional = report.config.initial_cash / (
            Decimal("1") + report.config.fee_rate
        )
        execution = candles[0].open * (
            Decimal("1") + report.config.slippage_rate
        )
        quantity = notional / execution
        slippage = (execution - candles[0].open) * quantity
    return PerformanceMetrics(
        final_equity=report.final_equity,
        total_return=report.total_return,
        cagr=_cagr(report.final_equity, period),
        maximum_drawdown=report.maximum_equity_drawdown,
        profit_factor=None,
        completed_trades=report.completed_trade_count,
        win_rate=report.win_rate,
        fees_paid=report.fees_paid,
        slippage_cost=slippage,
        time_exposure=report.exposure_percentage,
        capital_exposure=report.exposure_percentage,
    )


def _maximum_drawdown(equities: Iterable[Decimal]) -> Decimal:
    peak = INITIAL_CASH
    drawdown = Decimal("0")
    for equity in equities:
        if equity > peak:
            peak = equity
        current = (equity - peak) / peak
        if current < drawdown:
            drawdown = current
    return drawdown


def _cagr(final_equity: Decimal, period: ResearchPeriod) -> Decimal:
    years = Decimal(period.end_time - period.start_time) / (
        Decimal("365.25") * Decimal(DAY_MILLISECONDS)
    )
    return (
        (final_equity / INITIAL_CASH).ln() / years
    ).exp() - Decimal("1")


def _modeled_slippage_cost(report: PortfolioBacktestReport) -> Decimal:
    result = Decimal("0")
    for fill in report.fills:
        if not isinstance(fill, SimulatedFill):
            raise RuntimeError("Rotation unexpectedly produced a non-strategy fill")
        difference = (
            fill.execution_price - fill.market_open_price
            if fill.side is Signal.BUY
            else fill.market_open_price - fill.execution_price
        )
        result += difference * fill.base_quantity
    return result


def _format_period(period: PeriodStudy) -> list[str]:
    start = _utc(period.period.start_time)
    end = _utc(period.period.end_time)
    lines = [
        "",
        f"{period.period.name}: [{start}, {end})",
        (
            "model                        equity    return      CAGR "
            "    max_dd      PF trades      fees  slippage  exposure"
        ),
    ]
    rows = [
        NamedMetrics("rotation_100pct", period.primary.metrics),
        NamedMetrics("rotation_50pct", period.half_exposure.metrics),
        *period.benchmarks,
    ]
    for item in rows:
        metrics = item.metrics
        lines.append(
            f"{item.name:<28} "
            f"{metrics.final_equity:>9.2f} "
            f"{_percent(metrics.total_return):>9} "
            f"{_percent(metrics.cagr):>9} "
            f"{_percent(metrics.maximum_drawdown):>10} "
            f"{_optional(metrics.profit_factor):>7} "
            f"{metrics.completed_trades:>6} "
            f"{metrics.fees_paid:>9.2f} "
            f"{metrics.slippage_cost:>9.2f} "
            f"{_percent(metrics.time_exposure):>9}"
        )
    primary = period.primary.metrics
    open_holding = (
        "USDT"
        if not period.primary.report.positions
        else ",".join(
            item.instrument.symbol
            for item in period.primary.report.positions
        )
    )
    lines.append(
        "  rotation audit: "
        f"reviews={primary.weekly_reviews}, "
        f"target_changes={primary.target_changes}, "
        f"BTC<->ETH rotations={primary.asset_rotations}, "
        f"win_rate={_optional_percent(primary.win_rate)}, "
        f"capital_exposure={_percent(primary.capital_exposure)}, "
        f"end_holding={open_holding}, "
        f"realized={period.primary.report.realized_pnl:.2f}, "
        f"unrealized={period.primary.report.unrealized_pnl:.2f}, "
        f"largest_win/gross_profit="
        f"{_optional_percent(primary.largest_win_concentration)}"
    )
    return lines


def _format_diagnosis(study: RotationStudy) -> list[str]:
    full = study.periods[0].primary
    pnl_by_symbol: dict[str, Decimal] = {}
    for trade in full.report.completed_trades:
        instrument = trade.entry_fill.instrument
        if instrument is None:
            raise RuntimeError("Completed trade has no instrument")
        pnl_by_symbol[instrument.symbol] = (
            pnl_by_symbol.get(instrument.symbol, Decimal("0")) + trade.pnl
        )
    target_counts = {target: 0 for target in RotationTarget}
    for review in full.plan.reviews:
        target_counts[review.target] += 1
    return [
        "",
        "Observed full-range attribution (descriptive, not causal)",
        (
            "  Completed-episode PnL: "
            + ", ".join(
                f"{symbol}={pnl:.2f} USDT"
                for symbol, pnl in sorted(pnl_by_symbol.items())
            )
        ),
        (
            "  Weekly targets: "
            + ", ".join(
                f"{target.value}={target_counts[target]}"
                for target in RotationTarget
            )
        ),
        (
            "  Costs are modeled fill by fill. Open end-of-period positions "
            "are marked at the last close without a hypothetical exit fee."
        ),
        (
            "  Profit factor and concentration use completed holding episodes "
            "only; unrealized PnL remains in total return."
        ),
    ]


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")


def _percent(value: Decimal) -> str:
    return f"{value * 100:.4f}%"


def _optional(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    if value.is_infinite():
        return "inf"
    return f"{value:.4f}"


def _optional_percent(value: Decimal | None) -> str:
    return "N/A" if value is None else _percent(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed BTC/ETH relative-momentum study",
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
