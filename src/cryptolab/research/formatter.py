"""Human-readable formatting for evidence-oriented strategy research."""

from datetime import UTC, datetime
from decimal import Decimal

from cryptolab.research.models import StrategyResearchReport
from cryptolab.strategies.models import Signal, StrategyDecision


def format_research_report(report: StrategyResearchReport) -> str:
    """Format baseline metrics and the evidence behind strategy actions."""
    base_asset = report.symbol.removesuffix("USDT")
    lines = [
        "CryptoLab Strategy Research",
        "================================",
        (
            f"Dataset: {report.exchange}/{report.market_type} "
            f"{report.symbol} {report.interval}"
        ),
        (
            f"Coverage: {_format_timestamp(report.start_time)} to "
            f"{_format_timestamp(report.end_time)} (exclusive)"
        ),
        f"Candles: {report.candle_count}",
        "",
        "Baseline performance",
    ]
    for backtest in report.strategy_reports:
        skipped_buy = sum(
            item.decision.signal is Signal.BUY
            for item in backtest.skipped_decisions
        )
        skipped_sell = sum(
            item.decision.signal is Signal.SELL
            for item in backtest.skipped_decisions
        )
        lines.append(
            _format_performance_line(
                backtest.strategy_name,
                backtest.total_return,
                backtest.final_equity,
                backtest.realized_pnl,
                backtest.unrealized_pnl,
                backtest.maximum_equity_drawdown,
                backtest.completed_trade_count,
                backtest.win_rate,
                backtest.fees_paid,
                backtest.exposure_percentage,
                skipped_buy,
                skipped_sell,
                backtest.final_state.base_balance > 0,
            )
        )
    for benchmark in report.benchmarks:
        lines.append(
            _format_performance_line(
                benchmark.name,
                benchmark.total_return,
                benchmark.final_equity,
                benchmark.realized_pnl,
                benchmark.unrealized_pnl,
                benchmark.maximum_equity_drawdown,
                benchmark.completed_trade_count,
                benchmark.win_rate,
                benchmark.fees_paid,
                benchmark.exposure_percentage,
                0,
                0,
                benchmark.has_open_position,
            )
        )

    trend_report = report.strategy_reports[0]
    lines.extend([
        "",
        "Trend Following executed trades",
    ])
    if not report.trend_trades:
        lines.append("No completed Trend Following trades.")
    for index, diagnosis in enumerate(report.trend_trades, start=1):
        trade = diagnosis.trade
        lines.extend([
            f"Trade {index}:",
            *_format_decision_and_fill(
                "Entry",
                diagnosis.entry_decision,
                trade.entry_fill.execution_open_time,
                trade.entry_fill.execution_price,
                trade.entry_fill.fee,
                trend_report.config.fee_rate,
                trend_report.config.slippage_rate,
            ),
            *_format_decision_and_fill(
                "Exit",
                diagnosis.exit_decision,
                trade.exit_fill.execution_open_time,
                trade.exit_fill.execution_price,
                trade.exit_fill.fee,
                trend_report.config.fee_rate,
                trend_report.config.slippage_rate,
            ),
            (
                f"  Result: pnl={trade.pnl:.8f} USDT, "
                f"return={diagnosis.return_percentage * 100:.4f}%, "
                "holding="
                f"{Decimal(diagnosis.holding_milliseconds) / Decimal(3_600_000)}h"
            ),
            f"  Quantity: {trade.entry_fill.base_quantity} {base_asset}",
            f"  Exit reason: {diagnosis.exit_decision.explanation}",
        ])

    if report.trend_open_position is not None:
        position = report.trend_open_position
        lines.extend([
            "",
            "Trend Following remaining open position:",
            *_format_decision_and_fill(
                "Entry",
                position.entry_decision,
                position.entry_fill.execution_open_time,
                position.entry_fill.execution_price,
                position.entry_fill.fee,
                trend_report.config.fee_rate,
                trend_report.config.slippage_rate,
            ),
            f"  Entry cost: {position.entry_cost} USDT",
            f"  Quantity: {position.entry_fill.base_quantity} {base_asset}",
            f"  Mark price: {position.mark_price} USDT (last close)",
            f"  Unrealized PnL: {position.unrealized_pnl:.8f} USDT",
        ])

    lines.extend(["", "Mean Reversion BUY-condition frequency"])
    for condition in report.mean_reversion_conditions:
        lines.append(
            f"  {condition.name}: {condition.satisfied}/"
            f"{condition.evaluated} ({condition.percentage * 100:.2f}%)"
        )

    breakout = report.breakout_diagnosis
    lines.extend([
        "",
        "Breakout signal diagnosis",
        f"  BUY research signals: {breakout.buy_signals}",
        f"  SELL research signals: {breakout.sell_signals}",
        (
            "  BUY skipped while already holding: "
            f"{breakout.skipped_buy_while_holding}"
        ),
        f"  SELL skipped while flat: {breakout.skipped_sell_while_flat}",
        f"  New breakout events: {breakout.new_events}",
        f"  Continued breakout events: {breakout.continued_events}",
        (
            "  Note: a SELL is a downside market-event signal. In the "
            "long-only portfolio it cannot open a short position."
        ),
    ])
    return "\n".join(lines)


def _format_performance_line(
    name: str,
    total_return: Decimal,
    final_equity: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    maximum_drawdown: Decimal,
    trades: int,
    win_rate: Decimal | None,
    fees: Decimal,
    exposure: Decimal,
    skipped_buy: int,
    skipped_sell: int,
    has_open_position: bool,
) -> str:
    formatted_win_rate = (
        "N/A" if win_rate is None else f"{win_rate * 100:.2f}%"
    )
    return (
        f"{name}: return={total_return * 100:.4f}%, "
        f"final={final_equity:.8f} USDT, "
        f"realized={realized_pnl:.8f}, unrealized={unrealized_pnl:.8f}, "
        f"max_dd={maximum_drawdown * 100:.4f}%, trades={trades}, "
        f"win_rate={formatted_win_rate}, fees={fees:.8f}, "
        f"exposure={exposure * 100:.2f}%, "
        f"skipped_buy={skipped_buy}, skipped_sell={skipped_sell}, "
        f"open_position={'yes' if has_open_position else 'no'}"
    )


def _format_decision_and_fill(
    label: str,
    decision: StrategyDecision,
    execution_time: int,
    execution_price: Decimal,
    fee: Decimal,
    fee_rate: Decimal,
    slippage_rate: Decimal,
) -> list[str]:
    diagnostics = decision.diagnostics
    return [
        (
            f"  {label} times: signal_candle="
            f"{_format_timestamp(decision.candle_open_time)}, "
            f"available={_format_timestamp(decision.signal_available_time)}, "
            f"executed={_format_timestamp(execution_time)}"
        ),
        (
            f"  {label} execution: price={execution_price} USDT, "
            f"fee={fee} USDT, fee_rate={fee_rate * 100}%, "
            f"adverse_slippage={slippage_rate * 100}%"
        ),
        (
            f"  {label} indicators: close={diagnostics.get('close')}, "
            f"sma20={diagnostics.get('sma20')}, "
            f"sma50={diagnostics.get('sma50')}, "
            f"rsi14={diagnostics.get('rsi14')}, "
            f"rvol20={diagnostics.get('rvol20')}"
        ),
        (
            f"  {label} diagnostics: "
            f"volume_confirmed={diagnostics.get('volume_confirmed')}, "
            f"exit_rsi_confirmed={diagnostics.get('exit_rsi_confirmed')}, "
            f"exit_policy={diagnostics.get('exit_policy')}"
        ),
    ]


def _format_timestamp(milliseconds: int) -> str:
    timestamp = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    return timestamp.isoformat().replace("+00:00", "Z")

