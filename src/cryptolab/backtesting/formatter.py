"""Formatting for backtesting reports."""

from collections import Counter

from cryptolab.backtesting.models import BacktestReport


def format_backtest_report(report: BacktestReport) -> str:
    """Format one independent strategy backtest with explicit units."""
    win_rate = (
        "N/A"
        if report.win_rate is None
        else f"{report.win_rate * 100:.2f}%"
    )
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
        f"Initial cash:     {report.config.initial_cash:.2f} USDT",
        f"Fee per fill:     {report.config.fee_rate * 100:.4f}%",
        f"Adverse slippage: {report.config.slippage_rate * 100:.4f}%",
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
        f"Fills:            {len(report.fills)}",
        f"Completed trades: {report.completed_trade_count}",
        f"Win rate:         {win_rate}",
        f"Open base asset:  {report.final_state.base_balance}",
        f"Skipped signals:  {len(report.skipped_decisions)}",
    ]

    reasons = Counter(item.reason for item in report.skipped_decisions)
    for reason, count in sorted(reasons.items()):
        lines.append(f"  {reason}: {count}")

    return "\n".join(lines)
