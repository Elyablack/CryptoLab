#!/usr/bin/env python3
"""Audit the fixed BTC/ETH rotation without changing its trading rules."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from cryptolab.analytics.returns import (
    calculate_log_returns,
    calculate_volatility,
)
from cryptolab.backtesting import SimulatedFill
from cryptolab.market_data.models import Candle
from cryptolab.research.validation import ResearchPeriod
from cryptolab.strategies import RotationTarget, Signal
from cryptolab.strategies.relative_momentum import HOUR_MILLISECONDS

try:
    from scripts import research_relative_momentum as rotation_research
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    import research_relative_momentum as rotation_research


BTC_SYMBOL = rotation_research.BTC_SYMBOL
ETH_SYMBOL = rotation_research.ETH_SYMBOL
INITIAL_CASH = rotation_research.INITIAL_CASH
PeriodStudy = rotation_research.PeriodStudy
RotationStudy = rotation_research.RotationStudy
run_study = rotation_research.run_study


LOOKBACK_CANDLES = 30 * 24
ANNUAL_HOURS = Decimal("8766")


@dataclass(frozen=True)
class EpisodeRecord:
    """One closed or marked-open position episode in one isolated run."""

    run_scope: str
    episode_number: int
    status: str
    research_period: str
    exit_period: str | None
    crosses_period_boundary: bool
    symbol: str
    entry_signal_time_ms: int
    entry_signal_time_utc: str
    entry_time_ms: int
    entry_time_utc: str
    entry_market_open: Decimal
    entry_execution_price: Decimal
    entry_btc_momentum: Decimal
    entry_eth_momentum: Decimal
    exit_signal_time_ms: int | None
    exit_signal_time_utc: str | None
    exit_time_ms: int | None
    exit_time_utc: str | None
    exit_market_open_or_mark: Decimal | None
    exit_execution_price: Decimal | None
    holding_hours: Decimal
    exit_reason: str
    base_quantity: Decimal
    entry_quote_notional: Decimal
    equity_before_entry: Decimal
    equity_after_exit_or_mark: Decimal
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    entry_fee: Decimal
    exit_fee: Decimal | None
    total_fees: Decimal
    modeled_slippage: Decimal
    episode_return: Decimal
    asset_market_return: Decimal
    mae: Decimal
    mfe: Decimal


@dataclass(frozen=True)
class ValidationDecisionRecord:
    """One fixed weekly review and any fills caused by its target change."""

    decision_number: int
    decision_time_ms: int
    decision_time_utc: str
    signal_available_time_ms: int
    signal_available_time_utc: str
    btc_momentum: Decimal
    eth_momentum: Decimal
    leading_asset: str
    previous_target: str
    selected_target: str
    target_changed: bool
    actual_trade: bool
    executed_orders: str
    execution_time_ms: int | None
    execution_time_utc: str | None
    equity_after_execution: Decimal | None


@dataclass(frozen=True)
class RegimeMetrics:
    period: str
    btc_hourly_volatility: Decimal
    eth_hourly_volatility: Decimal
    btc_annualized_volatility: Decimal
    eth_annualized_volatility: Decimal
    btc_positive_momentum_share: Decimal
    btc_negative_momentum_share: Decimal
    eth_positive_momentum_share: Decimal
    eth_negative_momentum_share: Decimal
    btc_momentum_sign_changes: int
    eth_momentum_sign_changes: int
    momentum_observations: int
    weekly_leader_changes: int
    weekly_leader_change_rate: Decimal
    target_changes: int
    target_change_rate: Decimal
    btc_hours: int
    eth_hours: int
    usdt_hours: int
    usdt_spell_count: int
    average_usdt_spell_hours: Decimal
    maximum_usdt_spell_hours: int


@dataclass(frozen=True)
class DrawdownWindow:
    peak_time_ms: int
    peak_time_utc: str
    peak_equity: Decimal
    trough_time_ms: int
    trough_time_utc: str
    trough_equity: Decimal
    drawdown: Decimal
    recovery_time_ms: int | None
    recovery_time_utc: str | None
    overlapping_episode_numbers: tuple[int, ...]


@dataclass(frozen=True)
class CashInterval:
    decision_time_utc: str
    cash_start_time_utc: str
    cash_end_time_utc: str
    duration_hours: Decimal
    btc_return_after_transition: Decimal
    eth_return_after_transition: Decimal


@dataclass(frozen=True)
class PnlDecomposition:
    scope: str
    btc_net_pnl: Decimal
    eth_net_pnl: Decimal
    gross_trading_pnl: Decimal
    closed_net_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    modeled_slippage: Decimal
    fee_share_of_abs_gross: Decimal | None
    slippage_share_of_abs_gross: Decimal | None


@dataclass(frozen=True)
class IntegrityAudit:
    scope: str
    no_lookahead: bool
    next_open_alignment: bool
    sell_before_buy: bool
    fees_reconcile: bool
    pnl_reconciles: bool
    non_negative_cash: bool
    at_most_one_asset: bool
    unique_intents: bool

    @property
    def passed(self) -> bool:
        return all(
            value
            for name, value in asdict(self).items()
            if name != "scope"
        )


@dataclass(frozen=True)
class FailureAnalysis:
    study: RotationStudy
    episodes: tuple[EpisodeRecord, ...]
    validation_decisions: tuple[ValidationDecisionRecord, ...]
    regimes: tuple[RegimeMetrics, ...]
    validation_drawdown: DrawdownWindow
    validation_cash_intervals: tuple[CashInterval, ...]
    decompositions: tuple[PnlDecomposition, ...]
    audits: tuple[IntegrityAudit, ...]


def run_analysis(
    database_path: Path,
    episodes_path: Path,
    decisions_path: Path,
) -> FailureAnalysis:
    """Run the existing study, derive diagnostics, and write both CSVs."""
    study = run_study(database_path)
    period_ranges = tuple(item.period for item in study.periods[1:])
    candle_maps = {
        BTC_SYMBOL: {item.open_time: item for item in study.btc_candles},
        ETH_SYMBOL: {item.open_time: item for item in study.eth_candles},
    }
    episodes = tuple(
        record
        for period_study in study.periods
        for record in build_episode_records(
            period_study,
            candle_maps,
            period_ranges,
        )
    )
    validation = _period_by_name(study, "validation")
    decisions = build_validation_decisions(validation)
    full = _period_by_name(study, "full_range")
    development = _period_by_name(study, "development")
    regimes = (
        calculate_regime_metrics(study, development),
        calculate_regime_metrics(study, validation),
    )
    validation_episodes = tuple(
        item for item in episodes if item.run_scope == "validation"
    )
    drawdown = calculate_drawdown_window(validation, validation_episodes)
    cash_intervals = calculate_cash_intervals(
        validation,
        candle_maps,
    )
    decompositions = tuple(
        calculate_pnl_decomposition(
            item,
            tuple(
                episode
                for episode in episodes
                if episode.run_scope == item.period.name
            ),
        )
        for item in (full, development, validation, _period_by_name(
            study,
            "studied_test",
        ))
    )
    audits = tuple(
        audit_run(item, study)
        for item in study.periods
    )
    if not all(item.passed for item in audits):
        raise RuntimeError("At least one rotation integrity audit failed")
    _write_csv(episodes_path, episodes)
    _write_csv(decisions_path, decisions)
    return FailureAnalysis(
        study=study,
        episodes=episodes,
        validation_decisions=decisions,
        regimes=regimes,
        validation_drawdown=drawdown,
        validation_cash_intervals=cash_intervals,
        decompositions=decompositions,
        audits=audits,
    )


def build_episode_records(
    period_study: PeriodStudy,
    candle_maps: dict[str, dict[int, Candle]],
    period_ranges: Sequence[ResearchPeriod],
) -> tuple[EpisodeRecord, ...]:
    """Build closed episodes and append any marked-open position."""
    report = period_study.primary.report
    review_by_time = {
        item.candle_open_time: item
        for item in period_study.primary.plan.reviews
    }
    records = [
        _closed_episode(
            period_study,
            index,
            trade,
            candle_maps,
            period_ranges,
            review_by_time,
        )
        for index, trade in enumerate(report.completed_trades, start=1)
    ]
    open_cycles = [item for item in report.position_cycles if item.is_open]
    if len(open_cycles) > 1:
        raise RuntimeError("Rotation has more than one open position cycle")
    if open_cycles:
        records.append(_open_episode(
            period_study,
            len(records) + 1,
            open_cycles[0].entry_fill_ids[0],
            candle_maps,
            period_ranges,
            review_by_time,
        ))
    closed_net = sum(
        (item.net_pnl for item in records if item.net_pnl is not None),
        Decimal("0"),
    )
    if closed_net != report.realized_pnl:
        raise RuntimeError(
            f"{period_study.period.name} episode PnL does not reconcile"
        )
    open_unrealized = sum(
        (
            item.unrealized_pnl
            for item in records
            if item.unrealized_pnl is not None
        ),
        Decimal("0"),
    )
    if open_unrealized != report.unrealized_pnl:
        raise RuntimeError(
            f"{period_study.period.name} unrealized PnL does not reconcile"
        )
    return tuple(records)


def build_validation_decisions(
    validation: PeriodStudy,
) -> tuple[ValidationDecisionRecord, ...]:
    """Return every weekly review, including reviews without an order."""
    fills_by_signal: dict[int, list[SimulatedFill]] = {}
    for fill in validation.primary.report.fills:
        if not isinstance(fill, SimulatedFill):
            raise RuntimeError("Rotation emitted a non-strategy fill")
        fills_by_signal.setdefault(fill.signal_open_time, []).append(fill)
    records = []
    for index, review in enumerate(validation.primary.plan.reviews, start=1):
        fills = fills_by_signal.get(review.candle_open_time, [])
        execution_times = {item.execution_open_time for item in fills}
        if len(execution_times) > 1:
            raise RuntimeError("One review executed at multiple opens")
        equity_after = _equity_after_fills(fills, review.target)
        records.append(ValidationDecisionRecord(
            decision_number=index,
            decision_time_ms=review.candle_open_time,
            decision_time_utc=_utc(review.candle_open_time),
            signal_available_time_ms=review.signal_available_time,
            signal_available_time_utc=_utc(review.signal_available_time),
            btc_momentum=review.btc_momentum,
            eth_momentum=review.eth_momentum,
            leading_asset=_leading_asset(
                review.btc_momentum,
                review.eth_momentum,
            ),
            previous_target=review.previous_target.value,
            selected_target=review.target.value,
            target_changed=review.target_changed,
            actual_trade=bool(fills),
            executed_orders=";".join(
                f"{item.side.value}:{_fill_symbol(item)}"
                for item in fills
            ),
            execution_time_ms=(
                None if not fills else fills[0].execution_open_time
            ),
            execution_time_utc=(
                None if not fills else _utc(fills[0].execution_open_time)
            ),
            equity_after_execution=equity_after,
        ))
    return tuple(records)


def calculate_regime_metrics(
    study: RotationStudy,
    period_study: PeriodStudy,
) -> RegimeMetrics:
    """Describe prices and fixed signals without simulating alternatives."""
    period = period_study.period
    btc = study.btc_candles[period.start_index : period.end_index]
    eth = study.eth_candles[period.start_index : period.end_index]
    btc_vol = calculate_volatility(calculate_log_returns(btc))
    eth_vol = calculate_volatility(calculate_log_returns(eth))
    if btc_vol is None or eth_vol is None:
        raise RuntimeError("Insufficient candles for volatility")
    btc_momentum = _rolling_momentum(study.btc_candles, period)
    eth_momentum = _rolling_momentum(study.eth_candles, period)
    leaders = [
        _leading_asset(item.btc_momentum, item.eth_momentum)
        for item in period_study.primary.plan.reviews
    ]
    leader_changes = sum(
        current != previous
        for previous, current in zip(leaders, leaders[1:])
    )
    reviews = period_study.primary.plan.reviews
    target_changes = sum(item.target_changed for item in reviews)
    state_hours = _state_hours(period_study)
    cash_spells = _state_spells(period_study, RotationTarget.CASH.value)
    return RegimeMetrics(
        period=period.name,
        btc_hourly_volatility=btc_vol,
        eth_hourly_volatility=eth_vol,
        btc_annualized_volatility=btc_vol * ANNUAL_HOURS.sqrt(),
        eth_annualized_volatility=eth_vol * ANNUAL_HOURS.sqrt(),
        btc_positive_momentum_share=_positive_share(btc_momentum),
        btc_negative_momentum_share=_negative_share(btc_momentum),
        eth_positive_momentum_share=_positive_share(eth_momentum),
        eth_negative_momentum_share=_negative_share(eth_momentum),
        btc_momentum_sign_changes=_sign_changes(btc_momentum),
        eth_momentum_sign_changes=_sign_changes(eth_momentum),
        momentum_observations=len(btc_momentum),
        weekly_leader_changes=leader_changes,
        weekly_leader_change_rate=(
            Decimal(leader_changes) / Decimal(len(leaders) - 1)
            if len(leaders) > 1
            else Decimal("0")
        ),
        target_changes=target_changes,
        target_change_rate=(
            Decimal(target_changes) / Decimal(len(reviews))
            if reviews
            else Decimal("0")
        ),
        btc_hours=state_hours[BTC_SYMBOL],
        eth_hours=state_hours[ETH_SYMBOL],
        usdt_hours=state_hours[RotationTarget.CASH.value],
        usdt_spell_count=len(cash_spells),
        average_usdt_spell_hours=(
            Decimal(sum(cash_spells)) / Decimal(len(cash_spells))
            if cash_spells
            else Decimal("0")
        ),
        maximum_usdt_spell_hours=max(cash_spells, default=0),
    )


def calculate_drawdown_window(
    validation: PeriodStudy,
    episodes: Sequence[EpisodeRecord],
) -> DrawdownWindow:
    """Find peak, trough, and subsequent recovery on close-equity points."""
    period = validation.period
    points = [
        item
        for item in validation.primary.report.equity_curve
        if period.start_time <= item.candle_open_time < period.end_time
    ]
    peak = INITIAL_CASH
    peak_time = period.start_time
    worst = Decimal("0")
    trough = INITIAL_CASH
    trough_time = period.start_time
    drawdown_peak = peak
    drawdown_peak_time = peak_time
    for point in points:
        timestamp = point.available_time
        if point.equity > peak:
            peak = point.equity
            peak_time = timestamp
        drawdown = (point.equity - peak) / peak
        if drawdown < worst:
            worst = drawdown
            trough = point.equity
            trough_time = timestamp
            drawdown_peak = peak
            drawdown_peak_time = peak_time
    recovery_time = next(
        (
            item.available_time
            for item in points
            if item.available_time > trough_time
            and item.equity >= drawdown_peak
        ),
        None,
    )
    overlapping = tuple(
        item.episode_number
        for item in episodes
        if item.entry_time_ms <= trough_time
        and (item.exit_time_ms is None or item.exit_time_ms >= drawdown_peak_time)
    )
    return DrawdownWindow(
        peak_time_ms=drawdown_peak_time,
        peak_time_utc=_utc(drawdown_peak_time),
        peak_equity=drawdown_peak,
        trough_time_ms=trough_time,
        trough_time_utc=_utc(trough_time),
        trough_equity=trough,
        drawdown=worst,
        recovery_time_ms=recovery_time,
        recovery_time_utc=(None if recovery_time is None else _utc(recovery_time)),
        overlapping_episode_numbers=overlapping,
    )


def calculate_cash_intervals(
    validation: PeriodStudy,
    candle_maps: dict[str, dict[int, Candle]],
) -> tuple[CashInterval, ...]:
    """Describe asset moves while the actual target remained USDT."""
    changes = [
        item
        for item in validation.primary.plan.reviews
        if item.target_changed
    ]
    intervals = []
    for index, review in enumerate(changes):
        if review.target is not RotationTarget.CASH:
            continue
        start = review.signal_available_time
        next_change = changes[index + 1] if index + 1 < len(changes) else None
        end = (
            validation.period.end_time
            if next_change is None
            else next_change.signal_available_time
        )
        returns = {}
        for symbol in (BTC_SYMBOL, ETH_SYMBOL):
            start_price = candle_maps[symbol][start].open
            if end < validation.period.end_time:
                end_price = candle_maps[symbol][end].open
            else:
                last_open = end - HOUR_MILLISECONDS
                end_price = candle_maps[symbol][last_open].close
            returns[symbol] = end_price / start_price - Decimal("1")
        intervals.append(CashInterval(
            decision_time_utc=_utc(review.candle_open_time),
            cash_start_time_utc=_utc(start),
            cash_end_time_utc=_utc(end),
            duration_hours=Decimal(end - start) / Decimal(HOUR_MILLISECONDS),
            btc_return_after_transition=returns[BTC_SYMBOL],
            eth_return_after_transition=returns[ETH_SYMBOL],
        ))
    return tuple(intervals)


def calculate_pnl_decomposition(
    period_study: PeriodStudy,
    episodes: Sequence[EpisodeRecord],
) -> PnlDecomposition:
    """Reconcile execution-price PnL, fees, and marked open positions."""
    report = period_study.primary.report
    net_by_symbol = {BTC_SYMBOL: Decimal("0"), ETH_SYMBOL: Decimal("0")}
    gross = Decimal("0")
    for item in episodes:
        if item.net_pnl is not None:
            net_by_symbol[item.symbol] += item.net_pnl
        if item.unrealized_pnl is not None:
            net_by_symbol[item.symbol] += item.unrealized_pnl
        if item.gross_pnl is not None:
            gross += item.gross_pnl
        elif item.unrealized_pnl is not None:
            gross += item.unrealized_pnl + item.entry_fee
    fees = sum((item.fee for item in report.fills), Decimal("0"))
    slippage = sum((item.modeled_slippage for item in episodes), Decimal("0"))
    if not _decimal_close(gross - fees, report.total_pnl):
        raise RuntimeError(
            f"{period_study.period.name} gross PnL and fees do not reconcile"
        )
    denominator = abs(gross)
    return PnlDecomposition(
        scope=period_study.period.name,
        btc_net_pnl=net_by_symbol[BTC_SYMBOL],
        eth_net_pnl=net_by_symbol[ETH_SYMBOL],
        gross_trading_pnl=gross,
        closed_net_pnl=report.realized_pnl,
        unrealized_pnl=report.unrealized_pnl,
        fees=fees,
        modeled_slippage=slippage,
        fee_share_of_abs_gross=(None if denominator == 0 else fees / denominator),
        slippage_share_of_abs_gross=(
            None if denominator == 0 else slippage / denominator
        ),
    )


def audit_run(period_study: PeriodStudy, study: RotationStudy) -> IntegrityAudit:
    """Check timestamp, accounting, order, and holding invariants."""
    report = period_study.primary.report
    rows_by_time = {
        item.open_time: (btc, eth)
        for item, btc, eth in zip(
            study.btc_candles,
            study.btc_candles,
            study.eth_candles,
        )
    }
    no_lookahead = True
    for review in period_study.primary.plan.reviews:
        index = (review.candle_open_time - study.btc_candles[0].open_time) // (
            HOUR_MILLISECONDS
        )
        if index < LOOKBACK_CANDLES:
            no_lookahead = False
            break
        btc_now, eth_now = rows_by_time[review.candle_open_time]
        btc_past = study.btc_candles[index - LOOKBACK_CANDLES]
        eth_past = study.eth_candles[index - LOOKBACK_CANDLES]
        no_lookahead &= review.btc_momentum == (
            btc_now.close / btc_past.close - Decimal("1")
        )
        no_lookahead &= review.eth_momentum == (
            eth_now.close / eth_past.close - Decimal("1")
        )
    fills = [item for item in report.fills if isinstance(item, SimulatedFill)]
    next_open = all(
        item.execution_open_time == item.signal_available_time
        and item.signal_available_time
        == item.signal_open_time + HOUR_MILLISECONDS
        for item in fills
    )
    grouped: dict[int, list[SimulatedFill]] = {}
    for fill in fills:
        grouped.setdefault(fill.signal_open_time, []).append(fill)
    sell_before_buy = all(
        [item.side for item in items] in (
            [Signal.BUY],
            [Signal.SELL],
            [Signal.SELL, Signal.BUY],
        )
        for items in grouped.values()
    )
    fees = sum((item.fee for item in fills), Decimal("0"))
    closed = sum(
        (item.pnl for item in report.completed_trades),
        Decimal("0"),
    )
    intent_ids = [item.intent_id for item in report.order_intents]
    return IntegrityAudit(
        scope=period_study.period.name,
        no_lookahead=no_lookahead,
        next_open_alignment=next_open,
        sell_before_buy=sell_before_buy,
        fees_reconcile=_decimal_close(
            fees,
            sum((item.fee for item in report.execution_fills), Decimal("0")),
        ),
        pnl_reconciles=(
            closed == report.realized_pnl
            and report.total_pnl == report.realized_pnl + report.unrealized_pnl
            and report.total_pnl == report.final_equity - INITIAL_CASH
        ),
        non_negative_cash=all(
            item.quote_balance >= 0 for item in report.equity_curve
        ),
        at_most_one_asset=all(
            len(item.positions) <= 1 for item in report.equity_curve
        ),
        unique_intents=len(intent_ids) == len(set(intent_ids)),
    )


def format_summary(analysis: FailureAnalysis) -> str:
    """Render the key quantitative diagnostics used by the Markdown report."""
    lines = ["Rotation failure analysis", "=" * 40]
    for decomposition in analysis.decompositions:
        lines.append(
            f"{decomposition.scope}: BTC net={decomposition.btc_net_pnl:.6f}, "
            f"ETH net={decomposition.eth_net_pnl:.6f}, "
            f"gross={decomposition.gross_trading_pnl:.6f}, "
            f"fees={decomposition.fees:.6f}, "
            f"slippage={decomposition.modeled_slippage:.6f}, "
            f"unrealized={decomposition.unrealized_pnl:.6f}"
        )
    lines.extend(["", "Five largest full-range losses"])
    full_losses = sorted(
        (
            item
            for item in analysis.episodes
            if item.run_scope == "full_range" and item.net_pnl is not None
        ),
        key=lambda item: item.net_pnl or Decimal("0"),
    )[:5]
    for item in full_losses:
        lines.append(
            f"#{item.episode_number} {item.symbol} "
            f"{item.entry_time_utc} -> {item.exit_time_utc}: "
            f"gross={item.gross_pnl:.4f}, fees={item.total_fees:.4f}, "
            f"slippage={item.modeled_slippage:.4f}, net={item.net_pnl:.4f}, "
            f"MAE={_percent(item.mae)}, MFE={_percent(item.mfe)}, "
            f"BTCmom={_percent(item.entry_btc_momentum)}, "
            f"ETHmom={_percent(item.entry_eth_momentum)}"
        )
    lines.extend(["", "Regime comparison"])
    for item in analysis.regimes:
        lines.append(
            f"{item.period}: BTC ann.vol={_percent(item.btc_annualized_volatility)}, "
            f"ETH ann.vol={_percent(item.eth_annualized_volatility)}, "
            f"BTC mom+={_percent(item.btc_positive_momentum_share)}, "
            f"ETH mom+={_percent(item.eth_positive_momentum_share)}, "
            f"leader changes={item.weekly_leader_changes}, "
            f"target changes={item.target_changes}, "
            f"hours BTC/ETH/USDT={item.btc_hours}/{item.eth_hours}/{item.usdt_hours}, "
            f"USDT spells={item.usdt_spell_count}, "
            f"max={item.maximum_usdt_spell_hours}h"
        )
    dd = analysis.validation_drawdown
    lines.extend([
        "",
        (
            "Validation max drawdown: "
            f"{dd.peak_time_utc} {dd.peak_equity:.4f} -> "
            f"{dd.trough_time_utc} {dd.trough_equity:.4f} "
            f"({_percent(dd.drawdown)}), recovery="
            f"{dd.recovery_time_utc or 'not recovered'}; episodes="
            f"{dd.overlapping_episode_numbers}"
        ),
        "Validation USDT intervals:",
    ])
    for item in analysis.validation_cash_intervals:
        lines.append(
            f"  {item.cash_start_time_utc} -> {item.cash_end_time_utc} "
            f"({item.duration_hours}h): BTC={_percent(item.btc_return_after_transition)}, "
            f"ETH={_percent(item.eth_return_after_transition)}"
        )
    lines.append("")
    lines.extend(
        f"audit {item.scope}: {'PASS' if item.passed else 'FAIL'}"
        for item in analysis.audits
    )
    return "\n".join(lines)


def _closed_episode(
    period_study: PeriodStudy,
    episode_number: int,
    trade: object,
    candle_maps: dict[str, dict[int, Candle]],
    period_ranges: Sequence[ResearchPeriod],
    review_by_time: dict[int, object],
) -> EpisodeRecord:
    entry = trade.entry_fill
    exit_fill = trade.exit_fill
    if not isinstance(entry, SimulatedFill) or not isinstance(
        exit_fill,
        SimulatedFill,
    ):
        raise RuntimeError("Rotation episodes require strategy fills")
    symbol = _fill_symbol(entry)
    if _fill_symbol(exit_fill) != symbol:
        raise RuntimeError("Episode entry and exit symbols differ")
    entry_review = review_by_time[entry.signal_open_time]
    exit_review = review_by_time[exit_fill.signal_open_time]
    candles = candle_maps[symbol]
    lows, highs = _excursion_prices(
        candles,
        entry.execution_open_time,
        exit_fill.execution_open_time,
    )
    entry_cost = entry.quote_notional + entry.fee
    gross = exit_fill.quote_notional - entry.quote_notional
    net = exit_fill.quote_notional - exit_fill.fee - entry_cost
    if net != trade.pnl:
        raise RuntimeError("Trade and fill PnL differ")
    entry_period = _period_name(entry.execution_open_time, period_ranges)
    exit_period = _period_name(exit_fill.execution_open_time, period_ranges)
    return EpisodeRecord(
        run_scope=period_study.period.name,
        episode_number=episode_number,
        status="closed",
        research_period=(
            period_study.period.name
            if period_study.period.name != "full_range"
            else entry_period
        ),
        exit_period=exit_period,
        crosses_period_boundary=entry_period != exit_period,
        symbol=symbol,
        entry_signal_time_ms=entry.signal_open_time,
        entry_signal_time_utc=_utc(entry.signal_open_time),
        entry_time_ms=entry.execution_open_time,
        entry_time_utc=_utc(entry.execution_open_time),
        entry_market_open=entry.market_open_price,
        entry_execution_price=entry.execution_price,
        entry_btc_momentum=entry_review.btc_momentum,
        entry_eth_momentum=entry_review.eth_momentum,
        exit_signal_time_ms=exit_fill.signal_open_time,
        exit_signal_time_utc=_utc(exit_fill.signal_open_time),
        exit_time_ms=exit_fill.execution_open_time,
        exit_time_utc=_utc(exit_fill.execution_open_time),
        exit_market_open_or_mark=exit_fill.market_open_price,
        exit_execution_price=exit_fill.execution_price,
        holding_hours=Decimal(
            exit_fill.execution_open_time - entry.execution_open_time
        ) / Decimal(HOUR_MILLISECONDS),
        exit_reason=_exit_reason(exit_review.target),
        base_quantity=entry.base_quantity,
        entry_quote_notional=entry.quote_notional,
        equity_before_entry=entry.quote_balance_after + entry_cost,
        equity_after_exit_or_mark=exit_fill.quote_balance_after,
        gross_pnl=gross,
        net_pnl=net,
        unrealized_pnl=None,
        entry_fee=entry.fee,
        exit_fee=exit_fill.fee,
        total_fees=entry.fee + exit_fill.fee,
        modeled_slippage=(
            _fill_slippage(entry) + _fill_slippage(exit_fill)
        ),
        episode_return=net / entry_cost,
        asset_market_return=(
            exit_fill.market_open_price / entry.market_open_price
            - Decimal("1")
        ),
        mae=min(lows) / entry.execution_price - Decimal("1"),
        mfe=max(highs) / entry.execution_price - Decimal("1"),
    )


def _open_episode(
    period_study: PeriodStudy,
    episode_number: int,
    entry_fill_id: str,
    candle_maps: dict[str, dict[int, Candle]],
    period_ranges: Sequence[ResearchPeriod],
    review_by_time: dict[int, object],
) -> EpisodeRecord:
    report = period_study.primary.report
    entry = next(
        item
        for item in report.fills
        if isinstance(item, SimulatedFill) and item.fill_id == entry_fill_id
    )
    symbol = _fill_symbol(entry)
    position = next(
        item for item in report.positions if item.instrument.symbol == symbol
    )
    entry_review = review_by_time[entry.signal_open_time]
    last_open = period_study.period.end_time - HOUR_MILLISECONDS
    mark = candle_maps[symbol][last_open].close
    lows, highs = _excursion_prices(
        candle_maps[symbol],
        entry.execution_open_time,
        period_study.period.end_time,
    )
    entry_cost = entry.quote_notional + entry.fee
    return EpisodeRecord(
        run_scope=period_study.period.name,
        episode_number=episode_number,
        status="open",
        research_period=period_study.period.name,
        exit_period=None,
        crosses_period_boundary=False,
        symbol=symbol,
        entry_signal_time_ms=entry.signal_open_time,
        entry_signal_time_utc=_utc(entry.signal_open_time),
        entry_time_ms=entry.execution_open_time,
        entry_time_utc=_utc(entry.execution_open_time),
        entry_market_open=entry.market_open_price,
        entry_execution_price=entry.execution_price,
        entry_btc_momentum=entry_review.btc_momentum,
        entry_eth_momentum=entry_review.eth_momentum,
        exit_signal_time_ms=None,
        exit_signal_time_utc=None,
        exit_time_ms=None,
        exit_time_utc=None,
        exit_market_open_or_mark=mark,
        exit_execution_price=None,
        holding_hours=Decimal(
            period_study.period.end_time - entry.execution_open_time
        ) / Decimal(HOUR_MILLISECONDS),
        exit_reason="open_mark_to_market",
        base_quantity=entry.base_quantity,
        entry_quote_notional=entry.quote_notional,
        equity_before_entry=entry.quote_balance_after + entry_cost,
        equity_after_exit_or_mark=report.final_equity,
        gross_pnl=None,
        net_pnl=None,
        unrealized_pnl=position.unrealized_pnl,
        entry_fee=entry.fee,
        exit_fee=None,
        total_fees=entry.fee,
        modeled_slippage=_fill_slippage(entry),
        episode_return=position.unrealized_pnl / entry_cost,
        asset_market_return=mark / entry.market_open_price - Decimal("1"),
        mae=min(lows) / entry.execution_price - Decimal("1"),
        mfe=max(highs) / entry.execution_price - Decimal("1"),
    )


def _period_by_name(study: RotationStudy, name: str) -> PeriodStudy:
    return next(item for item in study.periods if item.period.name == name)


def _period_name(timestamp: int, periods: Sequence[ResearchPeriod]) -> str:
    for period in periods:
        if period.start_time <= timestamp < period.end_time:
            return period.name
    return "outside_periods"


def _fill_symbol(fill: SimulatedFill) -> str:
    if fill.instrument is None:
        raise RuntimeError("Fill has no instrument")
    return fill.instrument.symbol


def _exit_reason(target: RotationTarget) -> str:
    if target is RotationTarget.CASH:
        return "transition_to_USDT"
    return f"rotation_to_{target.value}"


def _fill_slippage(fill: SimulatedFill) -> Decimal:
    difference = (
        fill.execution_price - fill.market_open_price
        if fill.side is Signal.BUY
        else fill.market_open_price - fill.execution_price
    )
    return difference * fill.base_quantity


def _excursion_prices(
    candles: dict[int, Candle],
    start_time: int,
    end_time: int,
) -> tuple[list[Decimal], list[Decimal]]:
    held = [
        candle
        for timestamp, candle in candles.items()
        if start_time <= timestamp < end_time
    ]
    if not held:
        raise RuntimeError("Episode contains no held candles")
    return (
        [item.low for item in held],
        [item.high for item in held],
    )


def _equity_after_fills(
    fills: Sequence[SimulatedFill],
    target: RotationTarget,
) -> Decimal | None:
    if not fills:
        return None
    last = fills[-1]
    if target is RotationTarget.CASH:
        return last.quote_balance_after
    if last.side is not Signal.BUY:
        raise RuntimeError("Asset target does not end with a BUY")
    return last.quote_balance_after + last.quote_notional


def _rolling_momentum(
    candles: Sequence[Candle],
    period: ResearchPeriod,
) -> tuple[Decimal, ...]:
    start = max(period.start_index, LOOKBACK_CANDLES)
    return tuple(
        candles[index].close / candles[index - LOOKBACK_CANDLES].close
        - Decimal("1")
        for index in range(start, period.end_index)
    )


def _positive_share(values: Sequence[Decimal]) -> Decimal:
    return Decimal(sum(item > 0 for item in values)) / Decimal(len(values))


def _negative_share(values: Sequence[Decimal]) -> Decimal:
    return Decimal(sum(item < 0 for item in values)) / Decimal(len(values))


def _sign_changes(values: Sequence[Decimal]) -> int:
    return sum(
        previous * current < 0
        for previous, current in zip(values, values[1:])
    )


def _leading_asset(btc: Decimal, eth: Decimal) -> str:
    if btc > eth:
        return BTC_SYMBOL
    if eth > btc:
        return ETH_SYMBOL
    return "TIE"


def _state_hours(period_study: PeriodStudy) -> dict[str, int]:
    result = {BTC_SYMBOL: 0, ETH_SYMBOL: 0, RotationTarget.CASH.value: 0}
    period = period_study.period
    for point in period_study.primary.report.equity_curve:
        if not period.start_time <= point.candle_open_time < period.end_time:
            continue
        state = (
            RotationTarget.CASH.value
            if not point.positions
            else point.positions[0].instrument.symbol
        )
        result[state] += 1
    return result


def _state_spells(period_study: PeriodStudy, target: str) -> tuple[int, ...]:
    period = period_study.period
    states = []
    for point in period_study.primary.report.equity_curve:
        if not period.start_time <= point.candle_open_time < period.end_time:
            continue
        states.append(
            RotationTarget.CASH.value
            if not point.positions
            else point.positions[0].instrument.symbol
        )
    spells = []
    current = 0
    for state in states:
        if state == target:
            current += 1
        elif current:
            spells.append(current)
            current = 0
    if current:
        spells.append(current)
    return tuple(spells)


def _write_csv(path: Path, records: Iterable[object]) -> None:
    values = tuple(records)
    if not values:
        raise ValueError(f"Cannot write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_csv_row(asdict(item)) for item in values]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _csv_row(row: dict[str, object]) -> dict[str, object]:
    return {
        key: "" if value is None else str(value)
        for key, value in row.items()
    }


def _decimal_close(left: Decimal, right: Decimal) -> bool:
    scale = max(Decimal("1"), abs(left), abs(right))
    return abs(left - right) <= scale * Decimal("1e-24")


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def _percent(value: Decimal) -> str:
    return f"{value * Decimal('100'):.4f}%"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the fixed BTC/ETH rotation failure",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/market_data.db"),
    )
    parser.add_argument(
        "--episodes-output",
        type=Path,
        default=Path("reports/rotation-episodes.csv"),
    )
    parser.add_argument(
        "--decisions-output",
        type=Path,
        default=Path("reports/rotation-validation-decisions.csv"),
    )
    args = parser.parse_args(argv)
    try:
        analysis = run_analysis(
            args.database,
            args.episodes_output,
            args.decisions_output,
        )
    except Exception as error:
        parser.exit(1, f"Analysis failed: {error}\n")
    print(format_summary(analysis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
