"""Formatting for backtesting reports."""

from collections import Counter
from datetime import UTC, datetime

from cryptolab.backtesting.models import (
    BacktestReport,
    SimulatedFill,
    TrailingStopFill,
)
from cryptolab.strategies.models import Signal


def format_backtest_report(
    report: BacktestReport,
    *,
    show_trades: bool = False,
) -> str:
    """Format one independent strategy backtest with explicit units."""
    win_rate = (
        "N/A"
        if report.win_rate is None
        else f"{report.win_rate * 100:.2f}%"
    )
    execution_model = [
        f"Initial cash:     {report.config.initial_cash:.2f} USDT",
        f"Fee per fill:     {report.config.fee_rate * 100:.4f}%",
        f"Adverse slippage: {report.config.slippage_rate * 100:.4f}%",
    ]
    if report.config.trailing_stop_rate is not None:
        execution_model.append(
            "Trailing stop:    "
            f"{report.config.trailing_stop_rate * 100:.4f}%"
        )

    activity = [
        f"Fills:            {len(report.fills)}",
        f"Completed trades: {report.completed_trade_count}",
        f"Win rate:         {win_rate}",
        f"Open base asset:  {report.final_state.base_balance}",
        f"Skipped signals:  {len(report.skipped_decisions)}",
    ]
    if report.config.trailing_stop_rate is not None:
        trailing_exits = sum(
            isinstance(fill, TrailingStopFill)
            for fill in report.fills
        )
        activity.append(f"Trailing exits:   {trailing_exits}")

    lines = [
        "CryptoLab Backtest",
        "------------------------------",
        f"Strategy:         {report.strategy_name}",
        f"Exchange:         {report.exchange}",
        f"Market type:      {report.market_type}",
        f"Symbol:           {report.symbol}",
        f"Interval:         {report.interval}",
        "",
        "Execution model",
        *execution_model,
        "",
        "Performance",
        f"Final equity:     {report.final_equity:.8f} USDT",
        f"Total PnL:        {report.total_pnl:.8f} USDT",
        f"Total return:     {report.total_return * 100:.4f}%",
        f"Realized PnL:     {report.realized_pnl:.8f} USDT",
        f"Unrealized PnL:   {report.unrealized_pnl:.8f} USDT",
        f"Fees paid:        {report.fees_paid:.8f} USDT",
        (
            "Max drawdown:    "
            f"{report.maximum_equity_drawdown * 100:.4f}%"
        ),
        "",
        "Activity",
        *activity,
    ]

    reasons = Counter(item.reason for item in report.skipped_decisions)
    for reason, count in sorted(reasons.items()):
        lines.append(f"  {reason}: {count}")

    if show_trades:
        lines.extend(_format_trade_details(report))

    return "\n".join(lines)


def _format_trade_details(report: BacktestReport) -> list[str]:
    """Format completed trades, an open position, and skipped actions."""
    base_asset = report.symbol.removesuffix("USDT")
    lines = ["", "Completed trade details"]

    if not report.completed_trades:
        lines.append("  No completed trades.")
    for index, trade in enumerate(report.completed_trades, start=1):
        entry = trade.entry_fill
        exit_fill = trade.exit_fill
        entry_cost = entry.quote_notional + entry.fee
        return_percentage = trade.pnl / entry_cost * 100
        if isinstance(exit_fill, SimulatedFill):
            exit_details = [
                (
                    "  Exit signal available/execution:  "
                    f"{_format_timestamp(exit_fill.signal_available_time)} / "
                    f"{_format_timestamp(exit_fill.execution_open_time)}"
                ),
            ]
        else:
            exit_details = [
                (
                    "  Exit trailing stop: "
                    f"trigger={exit_fill.trigger_kind.value}, candle="
                    f"{_format_timestamp(exit_fill.trigger_candle_open_time)}"
                ),
                (
                    "  Stop levels: high_watermark="
                    f"{exit_fill.high_watermark} USDT, "
                    f"active_stop={exit_fill.active_stop_price} USDT, "
                    "market_reference="
                    f"{exit_fill.market_reference_price} USDT"
                ),
            ]
        lines.extend([
            f"Trade {index}:",
            (
                "  Entry signal available/execution: "
                f"{_format_timestamp(entry.signal_available_time)} / "
                f"{_format_timestamp(entry.execution_open_time)}"
            ),
            *exit_details,
            (
                "  Prices: entry="
                f"{entry.execution_price} USDT, "
                f"exit={exit_fill.execution_price} USDT"
            ),
            f"  {base_asset} quantity: {entry.base_quantity}",
            (
                "  Fees: entry="
                f"{entry.fee} USDT, exit={exit_fill.fee} USDT"
            ),
            (
                f"  Net PnL: {trade.pnl:.8f} USDT "
                f"({return_percentage:.4f}%)"
            ),
        ])

    if report.final_state.base_balance > 0:
        entry = next(
            fill for fill in reversed(report.fills)
            if fill.side is Signal.BUY
        )
        mark_price = report.equity_curve[-1].close_price
        lines.extend([
            "",
            "Remaining open position",
            (
                "  Entry signal available/execution: "
                f"{_format_timestamp(entry.signal_available_time)} / "
                f"{_format_timestamp(entry.execution_open_time)}"
            ),
            f"  Entry price: {entry.execution_price} USDT",
            f"  Entry cost: {report.final_state.entry_cost} USDT",
            f"  {base_asset} quantity: {report.final_state.base_balance}",
            f"  Mark price: {mark_price} USDT (last candle close)",
            f"  Unrealized PnL: {report.unrealized_pnl:.8f} USDT",
        ])

    if report.skipped_decisions:
        lines.extend(["", "Skipped actionable signals"])
        for skipped in report.skipped_decisions:
            decision = skipped.decision
            lines.append(
                "  "
                f"{decision.signal.value} at "
                f"{_format_timestamp(decision.signal_available_time)}: "
                f"{skipped.reason}"
            )

    return lines


def _format_timestamp(milliseconds: int) -> str:
    timestamp = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    iso_timestamp = timestamp.isoformat().replace("+00:00", "Z")
    return f"{iso_timestamp} ({milliseconds} ms)"
