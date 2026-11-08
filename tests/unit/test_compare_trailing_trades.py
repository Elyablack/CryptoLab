from __future__ import annotations

from pathlib import Path

import pytest

from scripts.compare_trailing_trades import (
    ExitType,
    ReportParseError,
    format_comparison,
    match_trades,
    parse_baseline_report,
    parse_trailing_report,
)


PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests/fixtures/reports"


def _baseline_report(
    *,
    trade_number: int = 7,
    entry_time: str = "2025-01-01T01:00:00Z",
    exit_time: str = "2025-01-01T05:00:00Z",
    entry_price: str = "100.00",
    exit_price: str = "110.00",
    completed_trades: int = 1,
    open_position: bool = False,
) -> str:
    open_text = "yes" if open_position else "no"
    open_section = ""
    if open_position:
        open_section = """
Trend Following remaining open position:
  Entry times: signal_candle=2025-01-02T00:00:00Z, available=2025-01-02T01:00:00Z, executed=2025-01-02T01:00:00Z
  Entry execution: price=120.00 USDT, fee=1.00 USDT
  Entry cost: 900.00 USDT
  Quantity: 7.50 BTC
  Mark price: 118.00 USDT (last close)
  Unrealized PnL: -15.00 USDT
"""
    return f"""
trend_following: return=9.00%, final=1090.00 USDT, realized=90.00, unrealized=0.00, max_dd=-2.00%, trades={completed_trades}, win_rate=100.00%, fees=2.00, exposure=50.00%, skipped_buy=0, skipped_sell=0, open_position={open_text}

Trend Following executed trades
Trade {trade_number}:
  Entry times: signal_candle=2025-01-01T00:00:00Z, available={entry_time}, executed={entry_time}
  Entry execution: price={entry_price} USDT, fee=1.00 USDT
  Exit times: signal_candle=2025-01-01T04:00:00Z, available={exit_time}, executed={exit_time}
  Exit execution: price={exit_price} USDT, fee=1.00 USDT
  Result: pnl=90.00 USDT, return=9.0000%, holding=4h
  Quantity: 9.00 BTC
{open_section}
Mean Reversion BUY-condition frequency
"""


def _trailing_report(
    *,
    trade_number: int = 99,
    entry_time: str = "2025-01-01T01:00:00Z",
    exit_time: str = "2025-01-01T03:00:00Z",
    trigger: str = "intrabar",
    entry_price: str = "100.00",
    exit_price: str = "105.00",
    completed_trades: int = 1,
    trailing_exits: int = 1,
    open_position: bool = False,
) -> str:
    if trailing_exits:
        exit_line = (
            f"  Exit trailing stop: trigger={trigger}, candle={exit_time}\n"
            "  Stop levels: high_watermark=111.00 USDT, "
            "active_stop=105.00 USDT, market_reference=105.00 USDT\n"
        )
    else:
        exit_line = (
            "  Exit signal available/execution:  "
            f"{exit_time} (0 ms) / {exit_time} (0 ms)\n"
        )
    open_quantity = "7.50" if open_position else "0"
    open_section = ""
    if open_position:
        open_section = """
Remaining open position
  Entry signal available/execution: 2025-01-02T01:00:00Z (0 ms) / 2025-01-02T01:00:00Z (0 ms)
  Entry price: 120.00 USDT
  BTC quantity: 7.50
  Unrealized PnL: -15.00 USDT
"""
    return f"""
Strategy: trend_following
Final equity: 1040.00 USDT
Total return: 4.0000%
Fees paid: 2.00 USDT
Max drawdown: -3.0000%
Completed trades: {completed_trades}
Open base asset: {open_quantity}
Trailing exits: {trailing_exits}

Completed trade details
Trade {trade_number}:
  Entry signal available/execution: {entry_time} (0 ms) / {entry_time} (0 ms)
{exit_line}  Prices: entry={entry_price} USDT, exit={exit_price} USDT
  BTC quantity: 9.00
  Fees: entry=1.00 USDT, exit=1.00 USDT
  Net PnL: 40.00 USDT (4.0000%)
{open_section}
Skipped actionable signals
"""


def test_versioned_report_fixtures_parse_without_local_runtime_reports() -> None:
    baseline = parse_baseline_report(
        (FIXTURE_ROOT / "baseline_sample.txt").read_text()
    )
    trailing = parse_trailing_report(
        (FIXTURE_ROOT / "trailing_sample.txt").read_text()
    )

    assert len(baseline.trades) == 1
    assert len(trailing.trades) == 1
    assert trailing.portfolio.trailing_exits == 1
    assert baseline.open_position is not None
    assert trailing.open_position is None


def test_matches_by_entry_timestamp_not_report_trade_number() -> None:
    baseline = parse_baseline_report(_baseline_report(trade_number=7))
    trailing = parse_trailing_report(_trailing_report(trade_number=99))

    result = match_trades(baseline, trailing)

    assert len(result.matched) == 1
    assert result.matched[0].baseline.report_trade_number == 7
    assert result.matched[0].trailing.report_trade_number == 99
    assert result.matched[0].trailing.exit_type is ExitType.TRAILING_INTRABAR
    assert result.matched[0].trailing.exit_time_is_exact is False
    assert result.matched[0].return_difference_pct_points == pytest.approx(-5)


def test_parses_gap_stop_as_a_candle_open_execution() -> None:
    report = parse_trailing_report(_trailing_report(trigger="gap_open"))

    trade = report.trades[0]
    assert trade.exit_type is ExitType.TRAILING_GAP
    assert trade.exit_time.isoformat() == "2025-01-01T03:00:00+00:00"
    assert trade.exit_time_is_exact is True
    assert trade.holding_hours == 2


def test_unmatched_entries_are_reported_separately() -> None:
    baseline = parse_baseline_report(_baseline_report())
    trailing = parse_trailing_report(
        _trailing_report(entry_time="2025-01-01T02:00:00Z")
    )

    result = match_trades(baseline, trailing)

    assert result.matched == ()
    assert result.baseline_only == baseline.trades
    assert result.trailing_only == trailing.trades


def test_open_position_is_not_counted_as_completed_trade() -> None:
    baseline = parse_baseline_report(_baseline_report(open_position=True))
    trailing = parse_trailing_report(_trailing_report(open_position=True))

    assert len(baseline.trades) == 1
    assert baseline.open_position is not None
    assert baseline.open_position.entry_price == 120
    assert len(trailing.trades) == 1
    assert trailing.open_position is not None
    assert trailing.open_position.unrealized_pnl == -15


@pytest.mark.parametrize(
    "parser, report",
    [
        (
            parse_baseline_report,
            _baseline_report(completed_trades=2),
        ),
        (
            parse_trailing_report,
            _trailing_report().replace(
                "  Net PnL: 40.00 USDT (4.0000%)\n",
                "",
            ),
        ),
    ],
)
def test_rejects_incomplete_or_internally_inconsistent_reports(
    parser,
    report: str,
) -> None:
    with pytest.raises(ReportParseError):
        parser(report)


def test_formatted_report_separates_sections_and_explains_pnl_difference() -> None:
    baseline = parse_baseline_report(_baseline_report())
    trailing = parse_trailing_report(_trailing_report())

    output = format_comparison(
        baseline,
        trailing,
        match_trades(baseline, trailing),
    )

    assert "Portfolio-level comparison" in output
    assert "Matched-entry trade comparison" in output
    assert "Baseline-only completed trades" in output
    assert "Trailing-only completed trades" in output
    assert "First execution-path divergence" in output
    assert "USDT PnL can differ" in output
    assert "intrabar*" in output
