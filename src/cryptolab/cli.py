"""Command-line interface for CryptoLab."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptolab.analytics.analyzer import MarketAnalyzer
from cryptolab.analytics.formatter import format_market_report
from cryptolab.market_data.client import BinanceSpotClient
from cryptolab.market_data.collector import HistoricalDataCollector
from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.storage.sqlite_repository import SQLiteRepository


DEFAULT_DATABASE_PATH = Path("data/market_data.db")


def main(argv: list[str] | None = None) -> int:
    """Run the CryptoLab command-line interface."""
    parser = _create_parser()
    args = parser.parse_args(argv)

    if args.end <= args.start:
        parser.error("--end must be later than --start")

    start_time = int(args.start.timestamp() * 1000)
    end_time = int(args.end.timestamp() * 1000)
    database_path: Path = args.database

    if args.command == "collect":
        return _run_collect(args, start_time, end_time, database_path)

    if args.command == "quality":
        return _run_quality(args, start_time, end_time, database_path)

    if args.command == "analyze":
        return _run_analyze(args, start_time, end_time, database_path)

    parser.error(f"unsupported command: {args.command}")
    return 2


def _run_collect(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Collect historical candles and store them in SQLite."""
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)

        repository = SQLiteRepository(database_path)
        repository.init_schema()

        client = BinanceSpotClient()
        collector = HistoricalDataCollector(client, repository)

        collector.collect(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
        )

    except Exception as error:
        print(f"Collection failed: {error}", file=sys.stderr)
        return 1

    print(
        "Collection completed: "
        f"symbol={args.symbol}, interval={args.interval}, "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )
    return 0


def _run_quality(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Check completeness of stored historical candles."""
    try:
        repository = SQLiteRepository(database_path)

        report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
        )

    except Exception as error:
        print(f"Quality check failed: {error}", file=sys.stderr)
        return 1

    print(
        "Quality report: "
        f"symbol={report.symbol}, interval={report.interval}, "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )

    print(f"expected_candles={report.expected_candles}")
    print(f"actual_candles={report.actual_candles}")
    print(f"missing_candles={report.missing_candles}")

    if report.missing_open_times:
        print("missing_open_times (UTC):")

        for open_time in report.missing_open_times:
            print(f"  {_format_utc_timestamp(open_time)}")

    if report.missing_candles > len(report.missing_open_times):
        print(
            "missing_open_times truncated: "
            f"showing first {len(report.missing_open_times)} "
            f"of {report.missing_candles}"
        )

    return 1 if report.missing_candles else 0


def _run_analyze(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Analyze stored historical market data."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )

        repository = SQLiteRepository(database_path)

        quality_report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
        )

        if quality_report.missing_candles > 0:
            raise ValueError(
                "Cannot analyze incomplete historical data: "
                f"{quality_report.missing_candles} candles are missing"
            )

        if (
            quality_report.actual_candles
            != quality_report.expected_candles
        ):
            raise ValueError(
                "Cannot analyze historical data: "
                "actual candle count differs from expected count"
            )

        candles = repository.get_candles_by_range(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
        )

        report = MarketAnalyzer().analyze(
            symbol=args.symbol,
            interval=args.interval,
            candles=candles,
        )

        output = format_market_report(report)

    except Exception as error:
        print(f"Analysis failed: {error}", file=sys.stderr)
        return 1

    print(
        "Analysis report: "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )
    print()
    print(output)

    return 0


def _create_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="cryptolab",
        description="CryptoLab cryptocurrency market data tools",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    collect_parser = subparsers.add_parser(
        "collect",
        help="collect historical Binance Spot candles",
        description="Collect historical Binance Spot candles",
    )
    _add_common_arguments(collect_parser)

    quality_parser = subparsers.add_parser(
        "quality",
        help="check completeness of stored candles",
        description="Check completeness of stored historical candles",
    )
    _add_common_arguments(quality_parser)

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="analyze stored historical market data",
        description=(
            "Calculate market statistics and technical indicators "
            "from stored historical candles"
        ),
    )
    _add_common_arguments(analyze_parser)

    return parser


def _add_common_arguments(
    command_parser: argparse.ArgumentParser,
) -> None:
    """Add shared arguments to a CLI subcommand."""
    command_parser.add_argument(
        "--symbol",
        required=True,
    )

    command_parser.add_argument(
        "--interval",
        required=True,
    )

    command_parser.add_argument(
        "--start",
        required=True,
        type=_parse_utc_date,
        metavar="YYYY-MM-DD",
    )

    command_parser.add_argument(
        "--end",
        required=True,
        type=_parse_utc_date,
        metavar="YYYY-MM-DD",
    )

    command_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        metavar="PATH",
    )


def _parse_utc_date(value: str) -> datetime:
    """Parse a YYYY-MM-DD date as UTC midnight."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")

    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from error

    if parsed.strftime("%Y-%m-%d") != value:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        )

    return parsed.replace(tzinfo=UTC)


def _format_utc_timestamp(
    timestamp_milliseconds: int,
) -> str:
    """Format a Unix millisecond timestamp as ISO 8601 UTC."""
    timestamp = datetime.fromtimestamp(
        timestamp_milliseconds / 1000,
        tz=UTC,
    )

    return timestamp.isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
