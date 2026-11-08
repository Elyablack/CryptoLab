#!/usr/bin/env python3
"""Compare baseline and trailing-stop Trend Following report paths."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterable


DECIMAL_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
TIMESTAMP_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


class ReportParseError(ValueError):
    """Raised when an input report is incomplete or internally inconsistent."""


class ExitType(str, Enum):
    STRATEGY_SELL = "strategy_sell"
    TRAILING_INTRABAR = "trailing_intrabar"
    TRAILING_GAP = "trailing_gap"


@dataclass(frozen=True)
class PortfolioMetrics:
    final_equity: Decimal
    total_return_pct: Decimal
    maximum_drawdown_pct: Decimal
    completed_trades: int
    fees: Decimal
    trailing_exits: int
    has_open_position: bool


@dataclass(frozen=True)
class TradeRecord:
    report_trade_number: int
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_type: ExitType
    exit_price: Decimal
    holding_hours: Decimal | None
    pnl: Decimal
    return_pct: Decimal
    quantity: Decimal

    @property
    def exit_time_is_exact(self) -> bool:
        return self.exit_type is not ExitType.TRAILING_INTRABAR


@dataclass(frozen=True)
class OpenPosition:
    entry_time: datetime
    entry_price: Decimal
    quantity: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class ParsedReport:
    name: str
    portfolio: PortfolioMetrics
    trades: tuple[TradeRecord, ...]
    open_position: OpenPosition | None


@dataclass(frozen=True)
class MatchedTrade:
    baseline: TradeRecord
    trailing: TradeRecord

    @property
    def exit_price_difference(self) -> Decimal:
        return self.trailing.exit_price - self.baseline.exit_price

    @property
    def return_difference_pct_points(self) -> Decimal:
        return self.trailing.return_pct - self.baseline.return_pct

    @property
    def pnl_difference(self) -> Decimal:
        return self.trailing.pnl - self.baseline.pnl


@dataclass(frozen=True)
class TradeMatchResult:
    matched: tuple[MatchedTrade, ...]
    baseline_only: tuple[TradeRecord, ...]
    trailing_only: tuple[TradeRecord, ...]


def parse_baseline_report(text: str) -> ParsedReport:
    """Parse Trend Following from the multi-strategy research report."""
    summary = _required_match(
        r"^trend_following:.*$",
        text,
        "Trend Following portfolio summary",
        flags=re.MULTILINE,
    ).group(0)
    portfolio = PortfolioMetrics(
        final_equity=_decimal_field(r"\bfinal=({value})\s+USDT", summary),
        total_return_pct=_decimal_field(r"\breturn=({value})%", summary),
        maximum_drawdown_pct=_decimal_field(r"\bmax_dd=({value})%", summary),
        completed_trades=_integer_field(r"\btrades=(\d+)", summary),
        fees=_decimal_field(r"\bfees=({value})", summary),
        trailing_exits=0,
        has_open_position=(
            _text_field(r"\bopen_position=(yes|no)", summary) == "yes"
        ),
    )

    trade_section = _section_between(
        text,
        "Trend Following executed trades",
        ("Trend Following remaining open position:",
         "Mean Reversion BUY-condition frequency"),
    )
    trades = tuple(
        _parse_baseline_trade(number, body)
        for number, body in _trade_blocks(trade_section)
    )
    _validate_trade_collection(trades, portfolio.completed_trades, "baseline")

    open_position = None
    if portfolio.has_open_position:
        open_section = _section_between(
            text,
            "Trend Following remaining open position:",
            ("Mean Reversion BUY-condition frequency",),
        )
        open_position = _parse_open_position(open_section, baseline=True)

    return ParsedReport(
        name="baseline",
        portfolio=portfolio,
        trades=trades,
        open_position=open_position,
    )


def parse_trailing_report(text: str) -> ParsedReport:
    """Parse the single-strategy backtest report with stop audit details."""
    strategy = _text_field(r"^Strategy:\s+(\S+)", text, flags=re.MULTILINE)
    if strategy != "trend_following":
        raise ReportParseError(
            f"Trailing report strategy must be trend_following, got {strategy!r}"
        )

    open_base = _decimal_field(
        r"^Open base asset:\s+({value})$",
        text,
        flags=re.MULTILINE,
    )
    portfolio = PortfolioMetrics(
        final_equity=_decimal_field(
            r"^Final equity:\s+({value})\s+USDT$",
            text,
            flags=re.MULTILINE,
        ),
        total_return_pct=_decimal_field(
            r"^Total return:\s+({value})%$",
            text,
            flags=re.MULTILINE,
        ),
        maximum_drawdown_pct=_decimal_field(
            r"^Max drawdown:\s+({value})%$",
            text,
            flags=re.MULTILINE,
        ),
        completed_trades=_integer_field(
            r"^Completed trades:\s+(\d+)$",
            text,
            flags=re.MULTILINE,
        ),
        fees=_decimal_field(
            r"^Fees paid:\s+({value})\s+USDT$",
            text,
            flags=re.MULTILINE,
        ),
        trailing_exits=_integer_field(
            r"^Trailing exits:\s+(\d+)$",
            text,
            flags=re.MULTILINE,
        ),
        has_open_position=open_base > 0,
    )

    trade_section = _section_between(
        text,
        "Completed trade details",
        ("Remaining open position", "Skipped actionable signals"),
    )
    trades = tuple(
        _parse_trailing_trade(number, body)
        for number, body in _trade_blocks(trade_section)
    )
    _validate_trade_collection(trades, portfolio.completed_trades, "trailing")
    actual_trailing_exits = sum(
        trade.exit_type is not ExitType.STRATEGY_SELL
        for trade in trades
    )
    if actual_trailing_exits != portfolio.trailing_exits:
        raise ReportParseError(
            "Trailing exit count mismatch: "
            f"summary={portfolio.trailing_exits}, parsed={actual_trailing_exits}"
        )

    open_position = None
    if portfolio.has_open_position:
        open_section = _section_between(
            text,
            "Remaining open position",
            ("Skipped actionable signals",),
        )
        open_position = _parse_open_position(open_section, baseline=False)

    return ParsedReport(
        name="trailing",
        portfolio=portfolio,
        trades=trades,
        open_position=open_position,
    )


def match_trades(
    baseline: ParsedReport,
    trailing: ParsedReport,
) -> TradeMatchResult:
    """Match completed trades by exact entry execution timestamp."""
    baseline_by_entry = _trades_by_entry(baseline.trades, baseline.name)
    trailing_by_entry = _trades_by_entry(trailing.trades, trailing.name)
    common_entries = sorted(baseline_by_entry.keys() & trailing_by_entry.keys())
    baseline_only_entries = sorted(
        baseline_by_entry.keys() - trailing_by_entry.keys()
    )
    trailing_only_entries = sorted(
        trailing_by_entry.keys() - baseline_by_entry.keys()
    )
    return TradeMatchResult(
        matched=tuple(MatchedTrade(
            baseline=baseline_by_entry[entry],
            trailing=trailing_by_entry[entry],
        ) for entry in common_entries),
        baseline_only=tuple(
            baseline_by_entry[entry] for entry in baseline_only_entries
        ),
        trailing_only=tuple(
            trailing_by_entry[entry] for entry in trailing_only_entries
        ),
    )


def format_comparison(
    baseline: ParsedReport,
    trailing: ParsedReport,
    matches: TradeMatchResult,
) -> str:
    """Create a complete human-readable terminal comparison."""
    lines = [
        "CryptoLab Baseline vs Trailing Stop Trade Comparison",
        "====================================================",
        "",
        "Portfolio-level comparison",
        _render_table(
            ("Metric", "Baseline", "Trailing 5%", "Difference"),
            _portfolio_rows(baseline.portfolio, trailing.portfolio),
        ),
        "",
        "Entry matching",
        f"Matched completed entries:       {len(matches.matched)}",
        f"Baseline-only completed entries: {len(matches.baseline_only)}",
        f"Trailing-only completed entries: {len(matches.trailing_only)}",
        (
            "Baseline open position:          "
            f"{_format_open_summary(baseline.open_position)}"
        ),
        (
            "Trailing open position:          "
            f"{_format_open_summary(trailing.open_position)}"
        ),
        "",
        "First execution-path divergence",
        _first_divergence(baseline, trailing, matches),
        "",
        "Matched-entry trade comparison",
    ]

    matched_rows = []
    for match in matches.matched:
        matched_rows.append((
            _format_time(match.baseline.entry_time),
            _format_pair(match.baseline.entry_price, match.trailing.entry_price, 2),
            _format_exit(match.baseline),
            _format_exit(match.trailing),
            _format_pair(match.baseline.exit_price, match.trailing.exit_price, 2),
            _signed(match.exit_price_difference, 2),
            _format_pair(match.baseline.return_pct, match.trailing.return_pct, 4),
            _signed(match.return_difference_pct_points, 4),
            _format_pair(match.baseline.pnl, match.trailing.pnl, 2),
            _format_holding_pair(match.baseline, match.trailing),
        ))
    lines.append(_render_table(
        (
            "Entry UTC",
            "Entry px B/T",
            "Baseline exit",
            "Trailing exit",
            "Exit px B/T",
            "Δ exit px",
            "Return % B/T",
            "Δ ret pp",
            "PnL B/T",
            "Hold B/T",
        ),
        matched_rows,
    ))

    lines.extend([
        "",
        "Baseline-only completed trades",
        _format_unmatched(matches.baseline_only),
        "",
        "Trailing-only completed trades",
        _format_unmatched(matches.trailing_only),
        "",
        "Largest matched-entry improvements",
        _format_extremes(matches.matched, reverse=True),
        "",
        "Largest matched-entry deteriorations",
        _format_extremes(matches.matched, reverse=False),
        "",
        "Interpretation notes and limitations",
        "- Completed trades are matched only by exact entry execution timestamp; trade numbers are ignored.",
        "- A trailing intrabar timestamp identifies the containing candle start, not an exact fill time.",
        "- A trailing gap exit executes at that candle's open in the backtest model, so its timestamp is exact to the model.",
        "- USDT PnL can differ even when entry/exit prices match because earlier exits change portfolio capital and BTC quantity.",
        "- A matching entry timestamp does not imply matching quantity or invested capital.",
        "- The baseline has an end-of-period open position; it is not counted as a completed baseline trade.",
        "- Portfolio return is compared through final equity, not by summing only completed-trade PnL.",
        "- Hourly OHLC does not reveal the exact intrabar price path or fill time.",
    ])
    return "\n".join(lines)


def _parse_baseline_trade(number: int, body: str) -> TradeRecord:
    entry_time = _timestamp_field(
        rf"Entry times:.*?executed=({TIMESTAMP_PATTERN})",
        body,
    )
    exit_time = _timestamp_field(
        rf"Exit times:.*?executed=({TIMESTAMP_PATTERN})",
        body,
    )
    result = _required_match(
        rf"Result:\s+pnl=({DECIMAL_PATTERN})\s+USDT,\s+"
        rf"return=({DECIMAL_PATTERN})%,\s+holding=({DECIMAL_PATTERN})h",
        body,
        f"baseline trade {number} result",
    )
    return TradeRecord(
        report_trade_number=number,
        entry_time=entry_time,
        entry_price=_decimal_field(r"Entry execution:\s+price=({value})", body),
        exit_time=exit_time,
        exit_type=ExitType.STRATEGY_SELL,
        exit_price=_decimal_field(r"Exit execution:\s+price=({value})", body),
        holding_hours=Decimal(result.group(3)),
        pnl=Decimal(result.group(1)),
        return_pct=Decimal(result.group(2)),
        quantity=_decimal_field(r"Quantity:\s+({value})\s+BTC", body),
    )


def _parse_trailing_trade(number: int, body: str) -> TradeRecord:
    entry_time = _timestamp_field(
        rf"Entry signal available/execution:.*?/\s*"
        rf"({TIMESTAMP_PATTERN})",
        body,
    )
    strategy_exit = re.search(
        rf"Exit signal available/execution:.*?/\s*"
        rf"({TIMESTAMP_PATTERN})",
        body,
    )
    stop_exit = re.search(
        rf"Exit trailing stop:\s+trigger=(intrabar|gap_open),\s+"
        rf"candle=({TIMESTAMP_PATTERN})",
        body,
    )
    if (strategy_exit is None) == (stop_exit is None):
        raise ReportParseError(
            f"Trailing trade {number} must have exactly one exit type"
        )
    if strategy_exit is not None:
        exit_time = _parse_timestamp(strategy_exit.group(1))
        exit_type = ExitType.STRATEGY_SELL
        duration_seconds = Decimal(str(
            (exit_time - entry_time).total_seconds()
        ))
        holding_hours: Decimal | None = duration_seconds / Decimal("3600")
    else:
        assert stop_exit is not None
        exit_time = _parse_timestamp(stop_exit.group(2))
        exit_type = (
            ExitType.TRAILING_INTRABAR
            if stop_exit.group(1) == "intrabar"
            else ExitType.TRAILING_GAP
        )
        if exit_type is ExitType.TRAILING_INTRABAR:
            holding_hours = None
        else:
            duration_seconds = Decimal(str(
                (exit_time - entry_time).total_seconds()
            ))
            holding_hours = duration_seconds / Decimal("3600")

    prices = _required_match(
        rf"Prices:\s+entry=({DECIMAL_PATTERN})\s+USDT,\s+"
        rf"exit=({DECIMAL_PATTERN})\s+USDT",
        body,
        f"trailing trade {number} prices",
    )
    result = _required_match(
        rf"Net PnL:\s+({DECIMAL_PATTERN})\s+USDT\s+"
        rf"\(({DECIMAL_PATTERN})%\)",
        body,
        f"trailing trade {number} result",
    )
    return TradeRecord(
        report_trade_number=number,
        entry_time=entry_time,
        entry_price=Decimal(prices.group(1)),
        exit_time=exit_time,
        exit_type=exit_type,
        exit_price=Decimal(prices.group(2)),
        holding_hours=holding_hours,
        pnl=Decimal(result.group(1)),
        return_pct=Decimal(result.group(2)),
        quantity=_decimal_field(r"BTC quantity:\s+({value})", body),
    )


def _parse_open_position(section: str, *, baseline: bool) -> OpenPosition:
    if baseline:
        entry_time = _timestamp_field(
            rf"Entry times:.*?executed=({TIMESTAMP_PATTERN})",
            section,
        )
        entry_price = _decimal_field(
            r"Entry execution:\s+price=({value})",
            section,
        )
        quantity_pattern = r"Quantity:\s+({value})\s+BTC"
    else:
        entry_time = _timestamp_field(
            rf"Entry signal available/execution:.*?/\s*"
            rf"({TIMESTAMP_PATTERN})",
            section,
        )
        entry_price = _decimal_field(r"Entry price:\s+({value})", section)
        quantity_pattern = r"BTC quantity:\s+({value})"
    return OpenPosition(
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=_decimal_field(quantity_pattern, section),
        unrealized_pnl=_decimal_field(
            r"Unrealized PnL:\s+({value})\s+USDT",
            section,
        ),
    )


def _trade_blocks(section: str) -> tuple[tuple[int, str], ...]:
    markers = list(re.finditer(r"^Trade\s+(\d+):\s*$", section, re.MULTILINE))
    blocks = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(section)
        blocks.append((int(marker.group(1)), section[marker.end():end]))
    return tuple(blocks)


def _validate_trade_collection(
    trades: tuple[TradeRecord, ...],
    expected_count: int,
    report_name: str,
) -> None:
    if len(trades) != expected_count:
        raise ReportParseError(
            f"{report_name} completed trade count mismatch: "
            f"summary={expected_count}, parsed={len(trades)}"
        )
    _trades_by_entry(trades, report_name)


def _trades_by_entry(
    trades: Iterable[TradeRecord],
    report_name: str,
) -> dict[datetime, TradeRecord]:
    result: dict[datetime, TradeRecord] = {}
    for trade in trades:
        if trade.entry_time in result:
            raise ReportParseError(
                f"Duplicate {report_name} entry timestamp: {trade.entry_time}"
            )
        result[trade.entry_time] = trade
    return result


def _section_between(
    text: str,
    start_marker: str,
    end_markers: tuple[str, ...],
) -> str:
    start = text.find(start_marker)
    if start < 0:
        raise ReportParseError(f"Missing section: {start_marker}")
    start += len(start_marker)
    ends = [
        position for marker in end_markers
        if (position := text.find(marker, start)) >= 0
    ]
    end = min(ends) if ends else len(text)
    return text[start:end]


def _required_match(
    pattern: str,
    text: str,
    label: str,
    *,
    flags: int = 0,
) -> re.Match[str]:
    match = re.search(pattern, text, flags)
    if match is None:
        raise ReportParseError(f"Missing or malformed {label}")
    return match


def _decimal_field(
    pattern: str,
    text: str,
    *,
    flags: int = 0,
) -> Decimal:
    expanded = pattern.format(value=DECIMAL_PATTERN)
    match = _required_match(expanded, text, pattern, flags=flags)
    return Decimal(match.group(1))


def _integer_field(pattern: str, text: str, *, flags: int = 0) -> int:
    return int(_required_match(pattern, text, pattern, flags=flags).group(1))


def _text_field(pattern: str, text: str, *, flags: int = 0) -> str:
    return _required_match(pattern, text, pattern, flags=flags).group(1)


def _timestamp_field(pattern: str, text: str) -> datetime:
    return _parse_timestamp(_required_match(pattern, text, pattern).group(1))


def _parse_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReportParseError(f"Invalid UTC timestamp: {value!r}") from error
    if timestamp.utcoffset() is None:
        raise ReportParseError(f"Timestamp must include UTC offset: {value!r}")
    return timestamp


def _portfolio_rows(
    baseline: PortfolioMetrics,
    trailing: PortfolioMetrics,
) -> list[tuple[str, str, str, str]]:
    return [
        (
            "Final equity (USDT)",
            f"{baseline.final_equity:.4f}",
            f"{trailing.final_equity:.4f}",
            _signed(trailing.final_equity - baseline.final_equity, 4),
        ),
        (
            "Total return",
            f"{baseline.total_return_pct:.4f}%",
            f"{trailing.total_return_pct:.4f}%",
            f"{_signed(trailing.total_return_pct - baseline.total_return_pct, 4)} pp",
        ),
        (
            "Maximum drawdown",
            f"{baseline.maximum_drawdown_pct:.4f}%",
            f"{trailing.maximum_drawdown_pct:.4f}%",
            f"{_signed(trailing.maximum_drawdown_pct - baseline.maximum_drawdown_pct, 4)} pp",
        ),
        (
            "Completed trades",
            str(baseline.completed_trades),
            str(trailing.completed_trades),
            f"{trailing.completed_trades - baseline.completed_trades:+d}",
        ),
        (
            "Fees (USDT)",
            f"{baseline.fees:.4f}",
            f"{trailing.fees:.4f}",
            _signed(trailing.fees - baseline.fees, 4),
        ),
        (
            "Trailing exits",
            str(baseline.trailing_exits),
            str(trailing.trailing_exits),
            f"{trailing.trailing_exits - baseline.trailing_exits:+d}",
        ),
    ]


def _first_divergence(
    baseline: ParsedReport,
    trailing: ParsedReport,
    matches: TradeMatchResult,
) -> str:
    candidates: list[tuple[datetime, str]] = []
    for match in matches.matched:
        baseline_trade = match.baseline
        trailing_trade = match.trailing
        if baseline_trade.entry_price != trailing_trade.entry_price:
            candidates.append((
                baseline_trade.entry_time,
                "Matched entry prices differ at "
                f"{_format_time(baseline_trade.entry_time)}: "
                f"baseline={baseline_trade.entry_price}, "
                f"trailing={trailing_trade.entry_price}.",
            ))
        if (
            baseline_trade.exit_type != trailing_trade.exit_type
            or baseline_trade.exit_time != trailing_trade.exit_time
            or baseline_trade.exit_price != trailing_trade.exit_price
        ):
            divergence_time = min(
                baseline_trade.exit_time,
                trailing_trade.exit_time,
            )
            candidates.append((
                divergence_time,
                f"Entry {_format_time(baseline_trade.entry_time)} first "
                f"diverges at {_format_time(divergence_time)}: baseline exits "
                f"{_format_exit(baseline_trade)} at {baseline_trade.exit_price}, "
                f"trailing exits {_format_exit(trailing_trade)} at "
                f"{trailing_trade.exit_price}.",
            ))

    baseline_open_time = (
        None if baseline.open_position is None
        else baseline.open_position.entry_time
    )
    trailing_open_time = (
        None if trailing.open_position is None
        else trailing.open_position.entry_time
    )
    for trade in matches.baseline_only:
        if trade.entry_time == trailing_open_time:
            candidates.append((
                trade.exit_time,
                f"Baseline closes entry {_format_time(trade.entry_time)} at "
                f"{_format_exit(trade)}, while trailing remains open.",
            ))
        else:
            candidates.append((
                trade.entry_time,
                f"Baseline-only entry at {_format_time(trade.entry_time)}.",
            ))
    for trade in matches.trailing_only:
        if trade.entry_time == baseline_open_time:
            candidates.append((
                trade.exit_time,
                f"Trailing closes entry {_format_time(trade.entry_time)} at "
                f"{_format_exit(trade)}, while baseline remains open.",
            ))
        else:
            candidates.append((
                trade.entry_time,
                f"Trailing-only entry at {_format_time(trade.entry_time)}.",
            ))
    if not candidates:
        return "No execution-path divergence detected."
    return min(candidates, key=lambda candidate: candidate[0])[1]


def _format_unmatched(trades: tuple[TradeRecord, ...]) -> str:
    if not trades:
        return "None"
    rows = [(
        str(trade.report_trade_number),
        _format_time(trade.entry_time),
        f"{trade.entry_price:.2f}",
        _format_exit(trade),
        f"{trade.exit_price:.2f}",
        f"{trade.return_pct:.4f}%",
        f"{trade.pnl:.2f}",
    ) for trade in trades]
    return _render_table(
        ("Report #", "Entry UTC", "Entry px", "Exit", "Exit px", "Return", "PnL"),
        rows,
    )


def _format_extremes(
    matches: tuple[MatchedTrade, ...],
    *,
    reverse: bool,
) -> str:
    if not matches:
        return "None"
    ordered = sorted(
        matches,
        key=lambda match: match.return_difference_pct_points,
        reverse=reverse,
    )[:5]
    rows = [(
        _format_time(match.baseline.entry_time),
        _signed(match.return_difference_pct_points, 4),
        _signed(match.exit_price_difference, 2),
        _signed(match.pnl_difference, 2),
        match.trailing.exit_type.value,
    ) for match in ordered]
    return _render_table(
        ("Entry UTC", "Δ return pp", "Δ exit px", "Δ PnL", "Trailing exit"),
        rows,
    )


def _format_open_summary(position: OpenPosition | None) -> str:
    if position is None:
        return "none"
    return (
        f"entry={_format_time(position.entry_time)}, "
        f"price={position.entry_price:.2f}, quantity={position.quantity}, "
        f"unrealized={position.unrealized_pnl:.2f} USDT"
    )


def _format_exit(trade: TradeRecord) -> str:
    suffix = {
        ExitType.STRATEGY_SELL: "strategy",
        ExitType.TRAILING_INTRABAR: "intrabar*",
        ExitType.TRAILING_GAP: "gap_open",
    }[trade.exit_type]
    return f"{_format_time(trade.exit_time)} {suffix}"


def _format_holding_pair(
    baseline: TradeRecord,
    trailing: TradeRecord,
) -> str:
    baseline_holding = _format_holding(baseline.holding_hours)
    trailing_holding = _format_holding(trailing.holding_hours)
    return f"{baseline_holding}/{trailing_holding}"


def _format_holding(value: Decimal | None) -> str:
    if value is None:
        return "n/a*"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}h"


def _format_pair(first: Decimal, second: Decimal, places: int) -> str:
    return f"{first:.{places}f}/{second:.{places}f}"


def _signed(value: Decimal, places: int) -> str:
    return f"{value:+.{places}f}"


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%MZ")


def _render_table(
    headers: tuple[str, ...],
    rows: Iterable[tuple[str, ...]],
) -> str:
    materialized = list(rows)
    widths = [len(header) for header in headers]
    for row in materialized:
        if len(row) != len(headers):
            raise ValueError("Table row width does not match headers")
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render(row: tuple[str, ...]) -> str:
        return " | ".join(
            value.ljust(widths[index])
            for index, value in enumerate(row)
        )

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join([
        render(headers),
        separator,
        *(render(row) for row in materialized),
    ])


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare baseline and trailing-stop Trend Following report paths"
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("reports/baseline-18m.txt"),
    )
    parser.add_argument(
        "--trailing",
        type=Path,
        default=Path("reports/trend-trailing-5pct.txt"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _create_parser().parse_args(argv)
    try:
        baseline = parse_baseline_report(args.baseline.read_text())
        trailing = parse_trailing_report(args.trailing.read_text())
        matches = match_trades(baseline, trailing)
    except (OSError, ReportParseError) as error:
        print(f"Comparison failed: {error}", file=sys.stderr)
        return 1

    print(format_comparison(baseline, trailing, matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
