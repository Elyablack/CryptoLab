from decimal import Decimal

from cryptolab.analytics.formatter import format_market_report
from cryptolab.analytics.models import MarketAnalysisReport


def test_format_market_report_uses_explicit_units() -> None:
    report = MarketAnalysisReport(
        symbol="BTCUSDT",
        interval="1h",
        candles_analyzed=100,
        last_open_time=0,
        last_close=Decimal("200"),
        total_return=Decimal("0.1234"),
        volatility=Decimal("0.0125"),
        max_drawdown=Decimal("-0.2"),
        sma20=Decimal("190"),
        sma50=Decimal("180"),
        ema20=Decimal("192"),
        rsi14=Decimal("55.5"),
        atr14=Decimal("5"),
        relative_volume20=Decimal("1.25"),
    )

    output = format_market_report(report)

    assert "Total return:    12.34%" in output
    assert "Max drawdown:    -20.00%" in output
    assert "Per-period return volatility (1h): 1.25%" in output
    assert "annual" not in output.lower()
    assert "ATR14:           5.00" in output
    assert "ATR percent:     2.50%" in output
    assert "RVOL20:          1.25x" in output


def test_format_market_report_handles_unavailable_volatility() -> None:
    report = MarketAnalysisReport(
        symbol="BTCUSDT",
        interval="1h",
        candles_analyzed=1,
        last_open_time=0,
        last_close=Decimal("100"),
        total_return=Decimal("0"),
        volatility=None,
        max_drawdown=Decimal("0"),
        sma20=Decimal("100"),
        sma50=Decimal("100"),
        ema20=Decimal("100"),
        rsi14=Decimal("50"),
        atr14=Decimal("1"),
        relative_volume20=Decimal("1"),
    )

    assert "Per-period return volatility (1h): N/A" in format_market_report(report)

