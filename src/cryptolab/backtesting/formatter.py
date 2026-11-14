"""Formatting for backtesting reports."""

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

from cryptolab.backtesting.accounting import (
    LotRealization,
    PositionCycle,
    PositionLot,
)
from cryptolab.backtesting.models import (
    BacktestReport,
    SimulatedFill,
    TrailingStopFill,
)
from cryptolab.backtesting.portfolio import PortfolioBacktestReport
from cryptolab.backtesting.sizing import LEGACY_SIZER_NAME
from cryptolab.strategies.models import Signal


def format_backtest_report(
    report: BacktestReport,
    *,
    show_trades: bool = False,
    show_accounting: bool = False,
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
    if report.position_sizer_name != LEGACY_SIZER_NAME:
        execution_model.append(
            f"Position sizing:  {report.position_sizer_name}"
        )
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
    if show_accounting:
        lines.extend(_format_accounting_details(
            report.open_lots,
            report.lot_realizations,
            report.position_cycles,
        ))

    return "\n".join(lines)


def format_portfolio_backtest_report(
    report: PortfolioBacktestReport,
    *,
    strategy_name: str,
    symbols: tuple[str, ...],
    exchange: str,
    market_type: str,
    show_trades: bool = False,
    show_accounting: bool = False,
) -> str:
    """Format one shared-quote multi-symbol portfolio backtest."""
    execution_model = [
        f"Initial cash:     {report.config.initial_cash:.2f} USDT",
        f"Fee per fill:     {report.config.fee_rate * 100:.4f}%",
        f"Adverse slippage: {report.config.slippage_rate * 100:.4f}%",
        f"Position sizing:  {report.position_sizer_name}",
        (
            "Event ordering:   "
            "exchange, market_type, symbol, strategy_name"
        ),
    ]
    if report.config.trailing_stop_rate is not None:
        execution_model.append(
            "Trailing stop:    "
            f"{report.config.trailing_stop_rate * 100:.4f}%"
        )

    lines = [
        "CryptoLab Shared Portfolio Backtest",
        "-----------------------------------",
        f"Strategy:         {strategy_name}",
        f"Exchange:         {exchange}",
        f"Market type:      {market_type}",
        f"Symbols:          {', '.join(symbols)}",
        f"Interval:         {report.interval}",
        "",
        "Execution model",
        *execution_model,
        "",
        "Performance",
        f"Final equity:     {report.final_equity:.8f} USDT",
        f"Quote balance:    {report.final_quote_balance:.8f} USDT",
        f"Total PnL:        {report.total_pnl:.8f} USDT",
        f"Total return:     {report.total_return * 100:.4f}%",
        f"Realized PnL:     {report.realized_pnl:.8f} USDT",
        f"Unrealized PnL:   {report.unrealized_pnl:.8f} USDT",
        (
            "Max drawdown:    "
            f"{report.maximum_equity_drawdown * 100:.4f}%"
        ),
        "",
        "Activity",
        f"Fills:            {len(report.fills)}",
        f"FIFO realizations:{len(report.lot_realizations):>4}",
        f"Position cycles:  {len(report.position_cycles)}",
        f"Open positions:   {len(report.positions)}",
        f"Skipped signals:  {len(report.skipped_decisions)}",
    ]
    reasons = Counter(item.reason for item in report.skipped_decisions)
    for reason, count in sorted(reasons.items()):
        lines.append(f"  {reason}: {count}")

    lines.extend(["", "Open positions"])
    if not report.positions:
        lines.append("  None.")
    for position in report.positions:
        lines.append(
            "  "
            f"{position.instrument.symbol}: quantity={position.quantity}, "
            f"cost_basis={position.cost_basis} USDT, "
            f"mark={position.mark_price} USDT, "
            f"market_value={position.market_value} USDT, "
            f"unrealized_pnl={position.unrealized_pnl:.8f} USDT"
        )

    if show_trades:
        lines.extend(_format_portfolio_trade_details(report))
    if show_accounting:
        lines.extend(_format_accounting_details(
            report.open_lots,
            report.lot_realizations,
            report.position_cycles,
        ))
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
        entry_cost = trade.allocated_entry_cost
        return_percentage = trade.pnl / entry_cost * 100
        entry_fee = trade.entry_fee if trade.entry_fee is not None else entry.fee
        exit_fee = (
            trade.exit_fee
            if trade.exit_fee is not None
            else exit_fill.fee
        )
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
            f"  {base_asset} quantity: {trade.closed_quantity}",
            (
                "  Fees: entry="
                f"{entry_fee} USDT, exit={exit_fee} USDT"
            ),
            (
                f"  Net PnL: {trade.pnl:.8f} USDT "
                f"({return_percentage:.4f}%)"
            ),
        ])

    if report.final_state.base_balance > 0:
        mark_price = report.equity_curve[-1].close_price
        open_lots = report.open_lots
        if len(open_lots) <= 1:
            entry = next(
                fill for fill in reversed(report.fills)
                if fill.side is Signal.BUY
            )
            lines.extend([
                "",
                "Remaining open position",
                (
                    "  Entry signal available/execution: "
                    f"{_format_timestamp(entry.signal_available_time)} / "
                    f"{_format_timestamp(entry.execution_open_time)}"
                ),
                f"  Entry price: {entry.execution_price} USDT",
            ])
        else:
            average_cost = (
                report.final_state.entry_cost
                / report.final_state.base_balance
            )
            lines.extend([
                "",
                "Remaining open position",
                f"  Open FIFO lots: {len(open_lots)}",
                (
                    "  Average cost including entry fees: "
                    f"{average_cost} USDT"
                ),
            ])
        lines.extend([
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


def _format_portfolio_trade_details(
    report: PortfolioBacktestReport,
) -> list[str]:
    lines = ["", "Completed FIFO realization details"]
    if not report.completed_trades:
        lines.append("  None.")
    for index, trade in enumerate(report.completed_trades, start=1):
        entry = trade.entry_fill
        exit_fill = trade.exit_fill
        instrument = entry.instrument
        symbol = "unknown" if instrument is None else instrument.symbol
        return_percentage = trade.pnl / trade.allocated_entry_cost * 100
        if isinstance(exit_fill, SimulatedFill):
            exit_event = (
                f"strategy SELL available="
                f"{_format_timestamp(exit_fill.signal_available_time)}, "
                f"executed={_format_timestamp(exit_fill.execution_open_time)}"
            )
        else:
            exit_event = (
                f"trailing {exit_fill.trigger_kind.value}, candle="
                f"{_format_timestamp(exit_fill.trigger_candle_open_time)}"
            )
        lines.extend([
            f"Realization {index} ({symbol}):",
            (
                "  Entry: available="
                f"{_format_timestamp(entry.signal_available_time)}, "
                f"executed={_format_timestamp(entry.execution_open_time)}, "
                f"price={entry.execution_price} USDT"
            ),
            f"  Exit: {exit_event}, price={exit_fill.execution_price} USDT",
            (
                f"  Quantity={trade.closed_quantity}, "
                f"entry_cost={trade.allocated_entry_cost} USDT, "
                f"pnl={trade.pnl:.8f} USDT ({return_percentage:.4f}%)"
            ),
        ])

    if report.skipped_decisions:
        lines.extend(["", "Skipped actionable signals"])
        for skipped in report.skipped_decisions:
            decision = skipped.decision
            lines.append(
                "  "
                f"{decision.symbol} {decision.signal.value} at "
                f"{_format_timestamp(decision.signal_available_time)}: "
                f"{skipped.reason}"
            )
    return lines


def _format_accounting_details(
    open_lots: Sequence[PositionLot],
    lot_realizations: Sequence[LotRealization],
    position_cycles: Sequence[PositionCycle],
) -> list[str]:
    """Format immutable FIFO audit models without changing calculations."""
    lines = ["", "Accounting details", "Open FIFO lots"]
    if not open_lots:
        lines.append("  None.")
    for lot in open_lots:
        lines.append(
            "  "
            f"{lot.instrument.symbol} {lot.lot_id}: "
            f"opened={_format_timestamp(lot.opened_at)}, "
            f"quantity={lot.remaining_quantity}, "
            f"cost_basis={lot.remaining_cost_basis} USDT, "
            f"remaining_entry_fee={lot.remaining_entry_fee} USDT"
        )

    lines.append("Lot realizations")
    if not lot_realizations:
        lines.append("  None.")
    for realization in lot_realizations:
        lines.append(
            "  "
            f"{realization.instrument.symbol} {realization.realization_id}: "
            f"at={_format_timestamp(realization.realized_at)}, "
            f"quantity={realization.quantity}, "
            f"cost_basis={realization.cost_basis} USDT, "
            f"net_proceeds={realization.net_proceeds} USDT, "
            f"pnl={realization.realized_pnl:.8f} USDT"
        )

    lines.append("Position cycles")
    if not position_cycles:
        lines.append("  None.")
    for cycle in position_cycles:
        closed = (
            "open"
            if cycle.closed_at is None
            else _format_timestamp(cycle.closed_at)
        )
        lines.append(
            "  "
            f"{cycle.instrument.symbol} {cycle.cycle_id}: "
            f"opened={_format_timestamp(cycle.opened_at)}, closed={closed}, "
            f"entries={len(cycle.entry_fill_ids)}, "
            f"exits={len(cycle.exit_fill_ids)}, "
            f"realizations={len(cycle.realization_ids)}, "
            f"realized_pnl={cycle.realized_pnl:.8f} USDT"
        )
    return lines


def _format_timestamp(milliseconds: int) -> str:
    timestamp = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    iso_timestamp = timestamp.isoformat().replace("+00:00", "Z")
    return f"{iso_timestamp} ({milliseconds} ms)"
