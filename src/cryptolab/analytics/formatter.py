"""Formatting market analysis reports."""

from cryptolab.analytics.models import MarketAnalysisReport


def format_market_report(report: MarketAnalysisReport) -> str:
    """Format report values with explicit per-period volatility units."""
    volatility = (
        f"{report.volatility * 100:.2f}%"
        if report.volatility is not None
        else "N/A"
    )

    return "\n".join(
        [
            "CryptoLab Market Analysis",
            "------------------------------",
            f"Symbol:          {report.symbol}",
            f"Interval:        {report.interval}",
            f"Candles:         {report.candles_analyzed}",
            "",
            "Price and Risk",
            f"Last close:      {report.last_close:.2f}",
            f"Total return:    {report.total_return * 100:.2f}%",
            f"Max drawdown:    {report.max_drawdown * 100:.2f}%",
            (
                "Per-period return volatility "
                f"({report.interval}): {volatility}"
            ),
            "",
            "Trend",
            f"SMA20:           {report.sma20:.2f}",
            f"SMA50:           {report.sma50:.2f}",
            f"EMA20:           {report.ema20:.2f}",
            "",
            "Momentum",
            f"RSI14:           {report.rsi14:.2f}",
            "",
            "Volatility and Volume",
            f"ATR14:           {report.atr14:.2f}",
            f"ATR percent:     {report.atr_percent:.2f}%",
            f"RVOL20:          {report.relative_volume20:.2f}x",
        ]
    )
