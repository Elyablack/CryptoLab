"""Command-line interface for CryptoLab."""

import argparse
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cryptolab.analytics.analyzer import MarketAnalyzer
from cryptolab.analytics.formatter import format_market_report
from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    FixedFractionPositionSizer,
)
from cryptolab.backtesting.formatter import format_backtest_report
from cryptolab.features.builder import FeatureBuilder
from cryptolab.market_data.bybit_client import BybitSpotClient
from cryptolab.market_data.client import BinanceSpotClient
from cryptolab.market_data.collector import (
    CollectionFailure,
    CollectionResult,
    HistoricalDataCollector,
)
from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    SUPPORTED_EXCHANGES,
)
from cryptolab.market_data.parser import parse_bybit_klines
from cryptolab.research import StrategyResearchRunner
from cryptolab.research.formatter import format_research_report
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.strategies import (
    BreakoutStrategy,
    MeanReversionStrategy,
    Signal,
    StrategyRunner,
    TrendFollowingStrategy,
)
from cryptolab.strategies.base import Strategy


DEFAULT_DATABASE_PATH = Path("data/market_data.db")
LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run the CryptoLab command-line interface."""
    parser = _create_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

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

    if args.command == "signals":
        return _run_signals(args, start_time, end_time, database_path)

    if args.command == "backtest":
        return _run_backtest(args, start_time, end_time, database_path)

    if args.command == "research":
        return _run_research(args, start_time, end_time, database_path)

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

        if args.exchange == DEFAULT_EXCHANGE:
            client = BinanceSpotClient()
            collector = HistoricalDataCollector(client, repository)
        else:
            bybit_client = BybitSpotClient()
            collector = HistoricalDataCollector(
                bybit_client,
                repository,
                exchange=args.exchange,
                market_type=SPOT_MARKET_TYPE,
                parser=parse_bybit_klines,
                pagination_direction="backward",
            )

        result = collector.collect(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
        )

    except CollectionFailure as error:
        _print_collection_summary(error.result)
        print(f"Collection failed: {error}", file=sys.stderr)
        LOGGER.debug("Collection failure details", exc_info=True)
        return 1
    except Exception as error:
        print(f"Collection failed: {error}", file=sys.stderr)
        LOGGER.debug("Collection failure details", exc_info=True)
        return 1

    _print_collection_summary(result)
    if not result.succeeded:
        print(
            "Collection incomplete: "
            f"{result.failure_reason or 'coverage validation failed'}",
            file=sys.stderr,
        )
        return 1

    print(
        "Collection completed: "
        f"exchange={args.exchange}, market_type={SPOT_MARKET_TYPE}, "
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
        repository.init_schema()

        report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            **_market_identity_kwargs(args.exchange),
        )

    except Exception as error:
        print(f"Quality check failed: {error}", file=sys.stderr)
        LOGGER.debug("Quality-check failure details", exc_info=True)
        return 1

    print(
        "Quality report: "
        f"exchange={report.exchange}, market_type={report.market_type}, "
        f"symbol={report.symbol}, interval={report.interval}, "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )

    print(f"expected_candles={report.expected_candles}")
    print(f"actual_candles={report.actual_candles}")
    print(f"missing_candles={report.missing_candles}")
    print(f"unexpected_candles={report.unexpected_candles}")
    print(f"duplicate_candles={report.duplicate_candles}")
    print(f"invalid_candles={report.invalid_candles}")

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

    if report.unexpected_open_times:
        print("unexpected_open_times (UTC):")
        for open_time in report.unexpected_open_times:
            print(f"  {_format_utc_timestamp(open_time)}")
    if report.duplicate_open_times:
        print("duplicate_open_times (UTC):")
        for open_time in report.duplicate_open_times:
            print(f"  {_format_utc_timestamp(open_time)}")
    if report.invalid_reasons:
        print("invalid_candle_details:")
        for reason in report.invalid_reasons:
            print(f"  {reason}")

    return 0 if report.is_valid else 1


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
        repository.init_schema()

        quality_report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            **_market_identity_kwargs(args.exchange),
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
            **_market_identity_kwargs(args.exchange),
        )

        report = MarketAnalyzer().analyze(
            symbol=args.symbol,
            interval=args.interval,
            candles=candles,
            **_market_identity_kwargs(args.exchange),
        )

        output = format_market_report(report)

    except Exception as error:
        print(f"Analysis failed: {error}", file=sys.stderr)
        LOGGER.debug("Analysis failure details", exc_info=True)
        return 1

    print(
        "Analysis report: "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )
    print()
    print(output)

    return 0


def _run_signals(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Generate research signals from stored historical candles."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )

        repository = SQLiteRepository(database_path)
        repository.init_schema()
        quality_report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            **_market_identity_kwargs(args.exchange),
        )
        if quality_report.missing_candles:
            raise ValueError(
                "Cannot generate signals from incomplete historical data: "
                f"{quality_report.missing_candles} candles are missing"
            )
        if quality_report.actual_candles != quality_report.expected_candles:
            raise ValueError(
                "Cannot generate signals: actual candle count differs "
                "from expected count"
            )

        candles = repository.get_candles_by_range(
            args.symbol,
            args.interval,
            start_time,
            end_time,
            **_market_identity_kwargs(args.exchange),
        )
        rows = FeatureBuilder().build(
            candles,
            interval=args.interval,
            as_of_time=end_time,
        )
        runner = StrategyRunner(_create_strategies(args.strategy))
        decisions = runner.run(
            args.symbol,
            args.interval,
            rows,
            as_of_time=end_time,
        )
        actionable = [
            decision
            for decision in decisions
            if decision.signal is not Signal.HOLD
        ]
    except Exception as error:
        print(f"Signal generation failed: {error}", file=sys.stderr)
        LOGGER.debug("Signal-generation failure details", exc_info=True)
        return 1

    print(
        "Historical signals: "
        f"exchange={args.exchange}, market_type={SPOT_MARKET_TYPE}, "
        f"symbol={args.symbol}, interval={args.interval}, "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"strategy={args.strategy}, database={database_path}"
    )
    if not actionable:
        print("No BUY/SELL signals found.")
        return 0

    for decision in actionable:
        print(
            "available_at="
            f"{_format_utc_timestamp(decision.signal_available_time)} "
            f"{decision.strategy_name} {decision.signal.value} "
            f"price={decision.reference_price} — {decision.explanation}"
        )

    return 0


def _run_backtest(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Backtest each selected strategy with an independent portfolio."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )

        repository = SQLiteRepository(database_path)
        repository.init_schema()
        quality_report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            **_market_identity_kwargs(args.exchange),
        )
        if quality_report.missing_candles:
            raise ValueError(
                "Cannot backtest incomplete historical data: "
                f"{quality_report.missing_candles} candles are missing"
            )
        if quality_report.actual_candles != quality_report.expected_candles:
            raise ValueError(
                "Cannot backtest: actual candle count differs from expected count"
            )

        candles = repository.get_candles_by_range(
            args.symbol,
            args.interval,
            start_time,
            end_time,
            **_market_identity_kwargs(args.exchange),
        )
        rows = FeatureBuilder().build(
            candles,
            interval=args.interval,
            as_of_time=end_time,
        )
        position_sizer = (
            None
            if args.position_size_fraction is None
            else FixedFractionPositionSizer(args.position_size_fraction)
        )
        engine = BacktestEngine(
            BacktestConfig(
                fee_rate=args.fee_rate,
                trailing_stop_rate=args.trailing_stop_rate,
            ),
            position_sizer=position_sizer,
        )
        reports = [
            engine.run(
                strategy,
                args.symbol,
                args.interval,
                rows,
                exchange=args.exchange,
                market_type=SPOT_MARKET_TYPE,
                as_of_time=end_time,
            )
            for strategy in _create_strategies(args.strategy)
        ]
    except Exception as error:
        print(f"Backtest failed: {error}", file=sys.stderr)
        LOGGER.debug("Backtest failure details", exc_info=True)
        return 1

    print(
        "Independent backtests: "
        f"exchange={args.exchange}, market_type={SPOT_MARKET_TYPE}, "
        f"symbol={args.symbol}, interval={args.interval}, "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )
    for report in reports:
        print()
        print(format_backtest_report(report, show_trades=args.show_trades))

    return 0


def _run_research(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Run unchanged baselines, benchmarks, and signal diagnostics."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )

        repository = SQLiteRepository(database_path)
        repository.init_schema()
        quality_report = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            **_market_identity_kwargs(args.exchange),
        )
        if quality_report.missing_candles:
            raise ValueError(
                "Cannot research incomplete historical data: "
                f"{quality_report.missing_candles} candles are missing"
            )
        if quality_report.actual_candles != quality_report.expected_candles:
            raise ValueError(
                "Cannot research: actual candle count differs from expected count"
            )

        candles = repository.get_candles_by_range(
            args.symbol,
            args.interval,
            start_time,
            end_time,
            **_market_identity_kwargs(args.exchange),
        )
        report = StrategyResearchRunner(BacktestConfig(
            fee_rate=args.fee_rate,
        )).run(
            args.symbol,
            args.interval,
            candles,
            exchange=args.exchange,
            market_type=SPOT_MARKET_TYPE,
            as_of_time=end_time,
        )
    except Exception as error:
        print(f"Research failed: {error}", file=sys.stderr)
        LOGGER.debug("Research failure details", exc_info=True)
        return 1

    print(format_research_report(report))
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
        help="collect historical public Spot candles",
        description="Collect historical Binance or Bybit Spot candles",
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

    signals_parser = subparsers.add_parser(
        "signals",
        help="inspect historical research signals",
        description=(
            "Evaluate research strategies on stored historical candles; "
            "no orders are created"
        ),
    )
    _add_common_arguments(signals_parser)
    signals_parser.add_argument(
        "--strategy",
        choices=("all", "trend", "mean-reversion", "breakout"),
        default="all",
    )

    backtest_parser = subparsers.add_parser(
        "backtest",
        help="backtest research strategies with virtual Spot portfolios",
        description=(
            "Run deterministic long-only research backtests; "
            "no exchange orders are created"
        ),
    )
    _add_common_arguments(backtest_parser)
    backtest_parser.add_argument(
        "--strategy",
        choices=("all", "trend", "mean-reversion", "breakout"),
        default="all",
    )
    backtest_parser.add_argument(
        "--fee-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.001"),
        metavar="DECIMAL",
        help="fee fraction per fill (default: 0.001 = 0.1%%)",
    )
    backtest_parser.add_argument(
        "--show-trades",
        action="store_true",
        help="show completed trades, open position, and skipped signals",
    )
    backtest_parser.add_argument(
        "--trailing-stop-rate",
        type=_parse_positive_decimal_rate,
        default=None,
        metavar="DECIMAL",
        help="optional trailing-stop fraction, for example 0.05 = 5%%",
    )
    backtest_parser.add_argument(
        "--position-size-fraction",
        type=_parse_position_size_fraction,
        default=None,
        metavar="DECIMAL",
        help=(
            "fraction of available quote cash allocated to each BUY; "
            "default preserves all-in allocation"
        ),
    )

    research_parser = subparsers.add_parser(
        "research",
        help="compare baseline strategies with simple benchmarks",
        description=(
            "Run reproducible baseline strategy and signal diagnostics; "
            "no strategy parameters or exchange orders are changed"
        ),
    )
    _add_common_arguments(research_parser)
    research_parser.add_argument(
        "--fee-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.001"),
        metavar="DECIMAL",
        help="fee fraction per fill (default: 0.001 = 0.1%%)",
    )

    return parser


def _add_common_arguments(
    command_parser: argparse.ArgumentParser,
) -> None:
    """Add shared arguments to a CLI subcommand."""
    command_parser.add_argument(
        "--exchange",
        choices=SUPPORTED_EXCHANGES,
        default=DEFAULT_EXCHANGE,
    )

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
    command_parser.add_argument(
        "--verbose",
        action="store_true",
        help="show debug diagnostics on stderr",
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


def _parse_decimal_rate(value: str) -> Decimal:
    """Parse a finite decimal fraction in the range [0, 1)."""
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(
            f"invalid decimal rate: {value!r}"
        ) from error
    if not result.is_finite() or not Decimal("0") <= result < 1:
        raise argparse.ArgumentTypeError(
            "rate must be between 0 (inclusive) and 1"
        )
    return result


def _parse_positive_decimal_rate(value: str) -> Decimal:
    """Parse a finite decimal fraction in the range (0, 1)."""
    result = _parse_decimal_rate(value)
    if result == 0:
        raise argparse.ArgumentTypeError(
            "rate must be between 0 and 1"
        )
    return result


def _parse_position_size_fraction(value: str) -> Decimal:
    """Parse a finite allocation fraction in the range (0, 1]."""
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(
            f"invalid position size fraction: {value!r}"
        ) from error
    if not result.is_finite() or not Decimal("0") < result <= 1:
        raise argparse.ArgumentTypeError(
            "position size fraction must be between 0 and 1 inclusive"
        )
    return result


def _create_strategies(strategy_name: str) -> list[Strategy]:
    strategies = {
        "trend": TrendFollowingStrategy,
        "mean-reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
    }
    if strategy_name == "all":
        return [factory() for factory in strategies.values()]

    return [strategies[strategy_name]()]


def _market_identity_kwargs(exchange: str) -> dict[str, str]:
    """Keep legacy Binance calls positional while scoping other exchanges."""
    if exchange == DEFAULT_EXCHANGE:
        return {}
    return {
        "exchange": exchange,
        "market_type": SPOT_MARKET_TYPE,
    }


def _configure_logging(verbose: bool) -> None:
    """Configure dependency-free operational logging for one CLI run."""
    level = logging.DEBUG if verbose else logging.WARNING
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    root_logger.setLevel(level)


def _print_collection_summary(result: CollectionResult) -> None:
    print(
        "Collection summary: "
        f"exchange={result.exchange}, market_type={result.market_type}, "
        f"symbol={result.symbol}, interval={result.interval}, "
        f"expected={result.expected}, received={result.received}, "
        f"inserted={result.inserted}, existing={result.existing}, "
        f"conflicting={result.conflicting}, missing={result.missing}, "
        f"invalid={result.invalid}, unexpected={result.unexpected}, "
        f"excluded_unfinished={result.excluded_unfinished}, "
        f"empty_pages={result.empty_pages}"
    )
    if result.conflict_open_times:
        print("conflict_open_times (UTC):")
        for open_time in result.conflict_open_times:
            print(f"  {_format_utc_timestamp(open_time)}")
    if result.missing_open_times:
        print("missing_open_times (UTC):")
        for open_time in result.missing_open_times:
            print(f"  {_format_utc_timestamp(open_time)}")


if __name__ == "__main__":
    raise SystemExit(main())
