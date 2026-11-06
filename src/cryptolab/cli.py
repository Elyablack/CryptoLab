"""Command-line interface for CryptoLab."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptolab.market_data.client import BinanceSpotClient
from cryptolab.market_data.collector import HistoricalDataCollector
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


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cryptolab",
        description="CryptoLab cryptocurrency market data tools",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect",
        help="collect historical Binance Spot candles",
        description="Collect historical Binance Spot candles",
    )
    collect_parser.add_argument("--symbol", required=True)
    collect_parser.add_argument("--interval", required=True)
    collect_parser.add_argument(
        "--start",
        required=True,
        type=_parse_utc_date,
        metavar="YYYY-MM-DD",
    )
    collect_parser.add_argument(
        "--end",
        required=True,
        type=_parse_utc_date,
        metavar="YYYY-MM-DD",
    )
    collect_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        metavar="PATH",
    )

    return parser


def _parse_utc_date(value: str) -> datetime:
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


if __name__ == "__main__":
    raise SystemExit(main())

