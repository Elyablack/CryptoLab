"""Command-line interface for CryptoLab."""

import argparse
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event

from cryptolab.analytics.analyzer import MarketAnalyzer
from cryptolab.analytics.formatter import format_market_report
from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    FixedFractionPositionSizer,
    FixedFractionScaleSizer,
    PortfolioBacktestInput,
    PositionSizer,
)
from cryptolab.backtesting.formatter import (
    format_backtest_report,
    format_portfolio_backtest_report,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.features.models import FeatureRow
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
from cryptolab.paper import (
    ContinuousPaperTrader,
    MetricsServer,
    MultiSymbolPaperBotConfig,
    MultiSymbolPaperTrader,
    NotificationOutbox,
    PaperBotConfig,
    PaperExecutionConfig,
    PaperMetrics,
    PaperRiskConfig,
    PaperTelemetry,
    TelegramNotifier,
    backup_sqlite,
    configure_json_logging,
    paper_health,
    restore_sqlite,
    set_paper_trading_pause,
    set_portfolio_paper_trading_pause,
    verify_sqlite_database,
)
from cryptolab.research import StrategyResearchRunner
from cryptolab.research.event_study import SignalEventStudyRunner
from cryptolab.research.event_study_formatter import (
    format_signal_event_study,
    signal_event_study_to_csv,
    signal_event_study_to_json,
)
from cryptolab.research.formatter import format_research_report
from cryptolab.research.optimization import StrategyOptimizationRunner
from cryptolab.research.optimization_formatter import (
    format_optimization_report,
    optimization_report_to_csv,
    optimization_report_to_json,
)
from cryptolab.research.parameter_grid import (
    default_parameter_grids,
    grid_from_mapping,
)
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.storage.paper_state_repository import PaperStateRepository
from cryptolab.strategies import (
    BreakoutStrategy,
    MeanReversionStrategy,
    Signal,
    StrategyRunner,
    TrendFollowingStrategy,
    TrendPullbackStrategy,
)
from cryptolab.strategies.base import Strategy


DEFAULT_DATABASE_PATH = Path("data/market_data.db")
LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run the CryptoLab command-line interface."""
    parser = _create_parser()
    args = parser.parse_args(argv)
    _configure_logging(
        args.verbose,
        json_logs=bool(getattr(args, "json_logs", False)),
    )

    if args.command == "paper":
        return _run_paper(args)

    if args.end <= args.start:
        parser.error("--end must be later than --start")
    if args.command == "backtest":
        _validate_backtest_arguments(parser, args)
    if args.command == "optimize":
        _validate_optimize_arguments(parser, args)
    if args.command == "signals-research":
        _validate_optimize_arguments(parser, args)

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

    if args.command == "optimize":
        return _run_optimize(args, start_time, end_time, database_path)

    if args.command == "signals-research":
        return _run_signals_research(
            args,
            start_time,
            end_time,
            database_path,
        )

    parser.error(f"unsupported command: {args.command}")
    return 2


def _run_paper(args: argparse.Namespace) -> int:
    """Run or inspect public-data-only single or portfolio paper trading."""
    telemetry: PaperTelemetry | None = None
    metrics_server: MetricsServer | None = None
    outbox: NotificationOutbox | None = None
    try:
        if args.paper_command == "health":
            report = paper_health(
                args.state_database,
                session_id=args.session_id,
                mode=args.mode,
            )
            print(
                "Paper health: "
                f"healthy={str(report.healthy).lower()}, "
                f"session={report.session_id}, mode={report.mode}, "
                f"schema={report.schema_version}, "
                f"checkpoint={report.checkpoint_sequence}, "
                f"last_candle={report.last_candle_open_time}, "
                f"reconciled={str(report.reconciled).lower()}"
            )
            print(report.message)
            return 0 if report.healthy else 1
        if args.paper_command == "backup":
            report = backup_sqlite(args.source_database, args.output)
            _print_database_verification("Backup created", report)
            return 0
        if args.paper_command == "verify-backup":
            report = verify_sqlite_database(args.backup)
            _print_database_verification("Backup verified", report)
            return 0
        if args.paper_command == "restore":
            report = restore_sqlite(args.backup, args.output)
            _print_database_verification("Backup restored", report)
            print(
                "Restore used a new path. Run paper health and paper "
                "integrity against it before changing deployment paths."
            )
            return 0
        if args.paper_command in ("pause", "resume"):
            paused = args.paper_command == "pause"
            pause = (
                set_portfolio_paper_trading_pause
                if args.mode == "multi"
                else set_paper_trading_pause
            )
            revision = pause(
                args.state_database,
                args.session_id,
                paused,
            )
            print(
                f"Paper trading {'paused' if paused else 'resumed'}: "
                f"session={args.session_id}, revision={revision}"
            )
            _notify_manual_control(args, paused, revision)
            return 0

        if args.paper_command in ("run", "run-once"):
            telemetry, metrics_server, outbox = _create_paper_telemetry(args)

        if args.mode == "multi":
            return _run_multi_symbol_paper(args, telemetry=telemetry)

        strategies = _create_strategies(args.strategy)
        if len(strategies) != 1:
            raise ValueError("Paper trading requires exactly one strategy")
        position_sizer: PositionSizer | None = None
        if args.position_size_fraction is not None:
            position_sizer = FixedFractionPositionSizer(
                args.position_size_fraction
            )
        trader = ContinuousPaperTrader(
            PaperBotConfig(
                session_id=args.session_id,
                market_database=args.market_database,
                state_database=args.state_database,
                symbol=args.symbol,
                interval=args.interval,
                warmup_candles=args.warmup_candles,
                poll_seconds=args.poll_seconds,
            ),
            strategies[0],
            backtest_config=BacktestConfig(
                initial_cash=Decimal("1000"),
                fee_rate=args.fee_rate,
                slippage_rate=args.slippage_rate,
            ),
            risk_config=PaperRiskConfig(
                maximum_order_notional=args.maximum_order_notional,
                maximum_position_notional=args.maximum_position_notional,
                maximum_total_exposure=args.maximum_total_exposure,
                minimum_available_cash=args.minimum_available_cash,
                maximum_daily_loss=args.maximum_daily_loss,
                maximum_portfolio_drawdown=(
                    args.maximum_portfolio_drawdown
                ),
                data_freshness_limit_ms=args.data_freshness_seconds * 1000,
            ),
            execution_config=PaperExecutionConfig(
                quantity_step=args.quantity_step,
                minimum_quantity=args.minimum_quantity,
                minimum_notional=args.minimum_notional,
                simulated_latency_ms=args.simulated_latency_ms,
                partial_fill_fraction=args.partial_fill_fraction,
                cancel_unfilled_remainder=(
                    not args.keep_partial_order_open
                ),
            ),
            position_sizer=position_sizer,
            telemetry=telemetry,
        )
        if args.paper_command == "run-once":
            result = trader.run_once()
            _print_paper_cycle(result)
            trader.close()
            return 0
        if args.paper_command in ("status", "integrity"):
            status = (
                trader.check_integrity()
                if args.paper_command == "integrity"
                else trader.status()
            )
            _print_paper_status(status)
            if args.paper_command == "integrity":
                print("Paper session integrity: OK")
            trader.close()
            return 0
        if args.paper_command == "run":
            stop = Event()

            def request_stop(signum, frame) -> None:
                del signum, frame
                stop.set()

            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            print(
                "Paper bot running (public data, simulated orders only): "
                f"session={args.session_id}, symbol={args.symbol}, "
                f"interval={args.interval}"
            )
            trader.run_forever(stop)
            print("Paper bot stopped cleanly.")
            return 0
        raise ValueError(f"Unsupported paper command: {args.paper_command}")
    except Exception as error:
        print(f"Paper command failed: {error}", file=sys.stderr)
        LOGGER.debug("Paper command failure details", exc_info=True)
        return 1
    finally:
        if metrics_server is not None:
            metrics_server.stop()
        if outbox is not None:
            outbox.close()


def _run_multi_symbol_paper(
    args: argparse.Namespace,
    *,
    telemetry: PaperTelemetry | None = None,
) -> int:
    """Dispatch the fixed BTC/ETH Rotation shared-USDT paper service."""
    if args.strategy != "rotation":
        raise ValueError(
            "Multi-symbol paper mode requires --strategy rotation"
        )
    position_sizer: PositionSizer | None = None
    if args.position_size_fraction is not None:
        position_sizer = FixedFractionPositionSizer(
            args.position_size_fraction
        )
    trader = MultiSymbolPaperTrader(
        MultiSymbolPaperBotConfig(
            session_id=args.session_id,
            market_database=args.market_database,
            state_database=args.state_database,
            interval=args.interval,
            warmup_candles=args.portfolio_warmup_candles,
            poll_seconds=args.poll_seconds,
        ),
        backtest_config=BacktestConfig(
            initial_cash=Decimal("1000"),
            fee_rate=args.fee_rate,
            slippage_rate=args.slippage_rate,
        ),
        risk_config=PaperRiskConfig(
            maximum_order_notional=args.maximum_order_notional,
            maximum_position_notional=args.maximum_position_notional,
            maximum_total_exposure=args.maximum_total_exposure,
            minimum_available_cash=args.minimum_available_cash,
            maximum_daily_loss=args.maximum_daily_loss,
            maximum_portfolio_drawdown=(
                args.maximum_portfolio_drawdown
            ),
            data_freshness_limit_ms=args.data_freshness_seconds * 1000,
        ),
        execution_config=PaperExecutionConfig(
            quantity_step=args.quantity_step,
            minimum_quantity=args.minimum_quantity,
            minimum_notional=args.minimum_notional,
            simulated_latency_ms=args.simulated_latency_ms,
            partial_fill_fraction=args.partial_fill_fraction,
            cancel_unfilled_remainder=not args.keep_partial_order_open,
        ),
        position_sizer=position_sizer,
        telemetry=telemetry,
    )
    if args.paper_command == "run-once":
        result = trader.run_once()
        _print_portfolio_paper_cycle(result)
        return 0 if result.reconciled else 1
    if args.paper_command in ("status", "integrity"):
        status = (
            trader.check_integrity()
            if args.paper_command == "integrity"
            else trader.status()
        )
        _print_portfolio_paper_status(status)
        if args.paper_command == "integrity":
            print("Portfolio paper reconciliation: OK")
        return 0 if status.reconciled else 1
    if args.paper_command == "run":
        stop = Event()

        def request_stop(signum, frame) -> None:
            del signum, frame
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        print(
            "Portfolio paper bot running (public data, simulated orders "
            "only): "
            f"session={args.session_id}, symbols=BTCUSDT,ETHUSDT, "
            f"interval={args.interval}"
        )
        trader.run_forever(stop)
        print("Portfolio paper bot stopped cleanly.")
        return 0
    raise ValueError(f"Unsupported paper command: {args.paper_command}")


def _print_paper_cycle(result) -> None:
    print(
        "Paper cycle: "
        f"session={result.session_id}, processed={result.processed_candles}, "
        f"backfilled={result.backfilled_candles}, "
        f"live={result.live_candles}, fills={result.fills_created}, "
        f"paused={str(result.trading_paused).lower()}"
    )
    print(result.message)


def _print_paper_status(status) -> None:
    print(
        "Paper session: "
        f"session={status.session_id}, "
        f"checkpoint_sequence={status.checkpoint_sequence}, "
        f"checkpoint_open_time={status.checkpoint_open_time}, "
        f"paused={str(status.trading_paused).lower()}"
    )
    print(
        f"quote_balance={status.quote_balance} "
        f"base_balance={status.base_balance} "
        f"realized_pnl={status.realized_pnl} "
        f"fees_paid={status.fees_paid}"
    )
    print(
        f"orders={status.order_count} fills={status.fill_count} "
        f"reservations={status.reservation_count}"
    )


def _print_portfolio_paper_cycle(result) -> None:
    print(
        "Portfolio paper cycle: "
        f"session={result.session_id}, "
        f"sets={result.processed_candle_sets}, "
        f"backfilled={result.backfilled_candle_sets}, "
        f"live={result.live_candle_sets}, fills={result.fills_created}, "
        f"target={result.rotation_target}, "
        f"pending={result.pending_target}, "
        f"paused={str(result.trading_paused).lower()}, "
        f"reconciled={str(result.reconciled).lower()}"
    )
    print(result.message)


def _print_portfolio_paper_status(status) -> None:
    print(
        "Portfolio paper session: "
        f"session={status.session_id}, "
        f"checkpoint_sequence={status.checkpoint_sequence}, "
        f"checkpoint_revision={status.checkpoint_revision}, "
        f"checkpoint_open_time={status.checkpoint_open_time}, "
        f"target={status.rotation_target}, "
        f"pending={status.pending_target}, "
        f"phase={status.pending_phase}, "
        f"paused={str(status.trading_paused).lower()}, "
        f"reconciled={str(status.reconciled).lower()}"
    )
    print(
        f"USDT={status.quote_balance} BTC={status.btc_balance} "
        f"ETH={status.eth_balance} realized_pnl={status.realized_pnl} "
        f"unrealized_pnl={status.unrealized_pnl} "
        f"fees_paid={status.fees_paid}"
    )
    print(
        f"orders={status.order_count} fills={status.fill_count} "
        f"reservations={status.reservation_count}"
    )
    for diagnostic in status.reconciliation_diagnostics:
        print(f"reconciliation: {diagnostic}")


def _create_paper_telemetry(
    args: argparse.Namespace,
) -> tuple[PaperTelemetry, MetricsServer | None, NotificationOutbox]:
    repository = PaperStateRepository(args.state_database)
    repository.init_schema()
    notifier = TelegramNotifier.from_environment()
    if args.telegram and notifier is None:
        raise ValueError(
            "--telegram requires CRYPTOLAB_TELEGRAM_BOT_TOKEN and "
            "CRYPTOLAB_TELEGRAM_CHAT_ID"
        )
    outbox = NotificationOutbox(repository, notifier)
    metrics = PaperMetrics()
    telemetry = PaperTelemetry(
        session_id=args.session_id,
        mode=args.mode,
        metrics=metrics,
        outbox=outbox,
    )
    server: MetricsServer | None = None
    if args.metrics_port is not None:
        candidate = MetricsServer(
            metrics,
            host=args.metrics_host,
            port=args.metrics_port,
        )
        if candidate.start():
            server = candidate
    return telemetry, server, outbox


def _notify_manual_control(
    args: argparse.Namespace,
    paused: bool,
    revision: int,
) -> None:
    outbox: NotificationOutbox | None = None
    try:
        repository = PaperStateRepository(args.state_database)
        repository.init_schema()
        notifier = TelegramNotifier.from_environment()
        outbox = NotificationOutbox(repository, notifier)
        telemetry = PaperTelemetry(
            session_id=args.session_id,
            mode=args.mode,
            outbox=outbox,
        )
        telemetry.event(
            "manual_pause" if paused else "manual_resume",
            (
                "Paper BUY intents paused"
                if paused
                else "Paper BUY intents resumed"
            ),
            dedup_key=f"manual-control:{revision}",
            notify=True,
        )
        telemetry.flush()
    except Exception as error:
        LOGGER.warning(
            "Manual control notification failed (%s); control is durable",
            type(error).__name__,
        )
    finally:
        if outbox is not None:
            outbox.close()


def _print_database_verification(prefix: str, report) -> None:
    print(
        f"{prefix}: path={report.path}, type={report.database_type}, "
        f"schema={report.schema_version}, "
        f"integrity_ok={str(report.integrity_ok).lower()}"
    )


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
    """Run an independent or explicitly shared-cash portfolio backtest."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )

        repository = SQLiteRepository(database_path)
        repository.init_schema()
        symbols = (args.symbol, *args.portfolio_symbol)
        rows_by_symbol = {
            symbol: _load_backtest_rows(
                repository,
                symbol=symbol,
                interval=args.interval,
                start_time=start_time,
                end_time=end_time,
                exchange=args.exchange,
            )
            for symbol in symbols
        }
        position_sizer = _create_backtest_position_sizer(args)
        engine = BacktestEngine(
            BacktestConfig(
                fee_rate=args.fee_rate,
                trailing_stop_rate=args.trailing_stop_rate,
                stop_loss_rate=args.stop_loss_rate,
            ),
            position_sizer=position_sizer,
        )
        strategies = _create_strategies(args.strategy)
        if args.portfolio_symbol:
            strategy = strategies[0]
            portfolio_report = engine.run_portfolio(
                tuple(
                    PortfolioBacktestInput(
                        strategy=strategy,
                        symbol=symbol,
                        interval=args.interval,
                        rows=rows_by_symbol[symbol],
                        exchange=args.exchange,
                        market_type=SPOT_MARKET_TYPE,
                    )
                    for symbol in symbols
                ),
                as_of_time=end_time,
            )
            reports = []
        else:
            portfolio_report = None
            reports = [
                engine.run(
                    strategy,
                    args.symbol,
                    args.interval,
                    rows_by_symbol[args.symbol],
                    exchange=args.exchange,
                    market_type=SPOT_MARKET_TYPE,
                    as_of_time=end_time,
                )
                for strategy in strategies
            ]
    except Exception as error:
        print(f"Backtest failed: {error}", file=sys.stderr)
        LOGGER.debug("Backtest failure details", exc_info=True)
        return 1

    if portfolio_report is not None:
        print(
            "Shared portfolio backtest: "
            f"exchange={args.exchange}, market_type={SPOT_MARKET_TYPE}, "
            f"symbols={','.join(symbols)}, interval={args.interval}, "
            f"range=[{args.start.date()}, {args.end.date()}), "
            f"database={database_path}"
        )
        print()
        print(format_portfolio_backtest_report(
            portfolio_report,
            strategy_name=strategies[0].name,
            symbols=symbols,
            exchange=args.exchange,
            market_type=SPOT_MARKET_TYPE,
            show_trades=args.show_trades,
            show_accounting=args.show_accounting,
        ))
        return 0

    print(
        "Independent backtests: "
        f"exchange={args.exchange}, market_type={SPOT_MARKET_TYPE}, "
        f"symbol={args.symbol}, interval={args.interval}, "
        f"range=[{args.start.date()}, {args.end.date()}), "
        f"database={database_path}"
    )
    for report in reports:
        print()
        print(format_backtest_report(
            report,
            show_trades=args.show_trades,
            show_accounting=args.show_accounting,
        ))

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


def _run_optimize(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Run bounded chronological research without network access."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )
        repository = SQLiteRepository(database_path)
        repository.init_schema()
        quality = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            as_of_time=end_time,
            **_market_identity_kwargs(args.exchange),
        )
        if not quality.is_valid:
            raise ValueError(
                "Cannot optimize invalid historical data: "
                f"missing={quality.missing_candles}, "
                f"unexpected={quality.unexpected_candles}, "
                f"duplicates={quality.duplicate_candles}, "
                f"invalid={quality.invalid_candles}"
            )
        candles = repository.get_candles_by_range(
            args.symbol,
            args.interval,
            start_time,
            end_time,
            **_market_identity_kwargs(args.exchange),
        )
        report = StrategyOptimizationRunner(BacktestConfig(
            fee_rate=args.fee_rate,
        )).run(
            args.symbol,
            args.interval,
            candles,
            _load_optimization_grids(args),
            exchange=args.exchange,
            market_type=SPOT_MARKET_TYPE,
            as_of_time=end_time,
            train_fraction=args.train_fraction,
            validation_fraction=args.validation_fraction,
            minimum_completed_trades=args.min_trades,
            max_combinations=args.max_combinations,
            walk_forward_folds=args.walk_forward_folds,
        )
        if args.output_json is not None:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(
                optimization_report_to_json(report),
                encoding="utf-8",
            )
        if args.output_csv is not None:
            args.output_csv.parent.mkdir(parents=True, exist_ok=True)
            args.output_csv.write_text(
                optimization_report_to_csv(report),
                encoding="utf-8",
            )
    except Exception as error:
        print(f"Optimization failed: {error}", file=sys.stderr)
        LOGGER.debug("Optimization failure details", exc_info=True)
        return 1

    print(format_optimization_report(report))
    if args.output_json is not None:
        print(f"JSON report: {args.output_json}")
    if args.output_csv is not None:
        print(f"CSV grid results: {args.output_csv}")
    return 0


def _run_signals_research(
    args: argparse.Namespace,
    start_time: int,
    end_time: int,
    database_path: Path,
) -> int:
    """Study forward price behavior after locally generated signals."""
    try:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Database does not exist: {database_path}"
            )
        repository = SQLiteRepository(database_path)
        repository.init_schema()
        quality = DataQualityChecker(repository).check(
            symbol=args.symbol,
            interval=args.interval,
            start_time=start_time,
            end_time=end_time,
            as_of_time=end_time,
            **_market_identity_kwargs(args.exchange),
        )
        if not quality.is_valid:
            raise ValueError(
                "Cannot study signals on invalid historical data: "
                f"missing={quality.missing_candles}, "
                f"unexpected={quality.unexpected_candles}, "
                f"duplicates={quality.duplicate_candles}, "
                f"invalid={quality.invalid_candles}"
            )
        candles = repository.get_candles_by_range(
            args.symbol,
            args.interval,
            start_time,
            end_time,
            **_market_identity_kwargs(args.exchange),
        )
        report = SignalEventStudyRunner().run(
            args.symbol,
            args.interval,
            candles,
            strategies=_create_strategies(args.strategy),
            exchange=args.exchange,
            market_type=SPOT_MARKET_TYPE,
            as_of_time=end_time,
            train_fraction=args.train_fraction,
            validation_fraction=args.validation_fraction,
            fee_rate=args.fee_rate,
            slippage_rate=args.slippage_rate,
            random_seed=args.random_seed,
            bootstrap_samples=args.bootstrap_samples,
            minimum_independent_events=args.minimum_independent_events,
        )
        if args.output_json is not None:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(
                signal_event_study_to_json(report),
                encoding="utf-8",
            )
        if args.output_csv is not None:
            args.output_csv.parent.mkdir(parents=True, exist_ok=True)
            args.output_csv.write_text(
                signal_event_study_to_csv(report),
                encoding="utf-8",
            )
    except Exception as error:
        print(f"Signal research failed: {error}", file=sys.stderr)
        LOGGER.debug("Signal-research failure details", exc_info=True)
        return 1

    print(format_signal_event_study(report))
    if args.output_json is not None:
        print(f"JSON report: {args.output_json}")
    if args.output_csv is not None:
        print(f"CSV report: {args.output_csv}")
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
        choices=(
            "all",
            "trend",
            "trend-pullback",
            "mean-reversion",
            "breakout",
        ),
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
        choices=(
            "all",
            "trend",
            "trend-pullback",
            "mean-reversion",
            "breakout",
        ),
        default="all",
    )
    backtest_parser.add_argument(
        "--position-sell-fraction",
        type=_parse_position_size_fraction,
        default=None,
        metavar="DECIMAL",
        help=(
            "fraction of the open base position sold on each SELL; requires "
            "--position-size-fraction and enables scale-in/scale-out"
        ),
    )
    backtest_parser.add_argument(
        "--portfolio-symbol",
        action="append",
        default=[],
        metavar="SYMBOL",
        help=(
            "additional symbol sharing the same USDT portfolio; repeat for "
            "multiple symbols"
        ),
    )
    backtest_parser.add_argument(
        "--show-accounting",
        action="store_true",
        help="show FIFO lots, lot realizations, and position cycles",
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
        "--stop-loss-rate",
        type=_parse_positive_decimal_rate,
        default=None,
        metavar="DECIMAL",
        help=(
            "optional fixed protective stop relative to actual entry price; "
            "cannot be combined with --trailing-stop-rate"
        ),
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

    optimize_parser = subparsers.add_parser(
        "optimize",
        help="run bounded chronological strategy-parameter research",
        description=(
            "Evaluate deterministic parameter grids using train, validation, "
            "and untouched test periods; no exchange orders are created"
        ),
    )
    _add_common_arguments(optimize_parser)
    optimize_parser.add_argument(
        "--strategy",
        choices=("all", "trend", "mean-reversion", "breakout"),
        default="all",
    )
    optimize_parser.add_argument(
        "--fee-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.001"),
        metavar="DECIMAL",
    )
    optimize_parser.add_argument(
        "--train-fraction",
        type=_parse_positive_decimal_rate,
        default=Decimal("0.6"),
        metavar="DECIMAL",
    )
    optimize_parser.add_argument(
        "--validation-fraction",
        type=_parse_positive_decimal_rate,
        default=Decimal("0.2"),
        metavar="DECIMAL",
    )
    optimize_parser.add_argument(
        "--min-trades",
        type=_parse_nonnegative_integer,
        default=3,
        metavar="COUNT",
    )
    optimize_parser.add_argument(
        "--max-combinations",
        type=_parse_positive_integer,
        default=100,
        metavar="COUNT",
    )
    optimize_parser.add_argument(
        "--walk-forward-folds",
        type=_parse_nonnegative_integer,
        default=0,
        metavar="COUNT",
        help="optional post-selection expanding-window diagnostics",
    )
    optimize_parser.add_argument(
        "--grid-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="optional JSON mapping of strategy names to parameter arrays",
    )
    optimize_parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        metavar="PATH",
    )
    optimize_parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        metavar="PATH",
    )

    signal_research_parser = subparsers.add_parser(
        "signals-research",
        help="study forward returns after historical research signals",
        description=(
            "Compare signal-following returns with deterministic matched "
            "controls; no portfolio or exchange orders are created"
        ),
    )
    _add_common_arguments(signal_research_parser)
    signal_research_parser.add_argument(
        "--strategy",
        choices=(
            "all",
            "trend",
            "trend-pullback",
            "mean-reversion",
            "breakout",
        ),
        default="all",
    )
    signal_research_parser.add_argument(
        "--fee-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.001"),
        metavar="DECIMAL",
    )
    signal_research_parser.add_argument(
        "--slippage-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.0005"),
        metavar="DECIMAL",
    )
    signal_research_parser.add_argument(
        "--train-fraction",
        type=_parse_positive_decimal_rate,
        default=Decimal("0.6"),
        metavar="DECIMAL",
    )
    signal_research_parser.add_argument(
        "--validation-fraction",
        type=_parse_positive_decimal_rate,
        default=Decimal("0.2"),
        metavar="DECIMAL",
    )
    signal_research_parser.add_argument(
        "--random-seed",
        type=int,
        default=20261114,
        metavar="INTEGER",
    )
    signal_research_parser.add_argument(
        "--bootstrap-samples",
        type=_parse_positive_integer,
        default=1000,
        metavar="COUNT",
    )
    signal_research_parser.add_argument(
        "--minimum-independent-events",
        type=_parse_positive_integer,
        default=20,
        metavar="COUNT",
    )
    signal_research_parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        metavar="PATH",
    )
    signal_research_parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        metavar="PATH",
    )

    paper_parser = subparsers.add_parser(
        "paper",
        help="run the persistent public-data paper-trading process",
        description=(
            "Operate deterministic Bybit Spot paper trading; no real "
            "exchange orders or private APIs are used"
        ),
    )
    paper_subparsers = paper_parser.add_subparsers(
        dest="paper_command",
        required=True,
    )
    for command, help_text in (
        ("run", "run continuously until SIGINT or SIGTERM"),
        ("run-once", "process all currently missing closed candles once"),
        ("status", "recover and show the current paper-session state"),
        ("integrity", "recover and verify all persistent session state"),
    ):
        command_parser = paper_subparsers.add_parser(
            command,
            help=help_text,
        )
        _add_paper_runtime_arguments(command_parser)
    for command, help_text in (
        ("pause", "block new BUY orders while allowing risk-reducing SELL"),
        ("resume", "allow new BUY orders after a manual pause"),
    ):
        command_parser = paper_subparsers.add_parser(
            command,
            help=help_text,
        )
        command_parser.add_argument("--session-id", required=True)
        command_parser.add_argument(
            "--mode",
            choices=("single", "multi"),
            default="single",
            help="single strategy session or shared BTC/ETH Rotation portfolio",
        )
        command_parser.add_argument(
            "--state-database",
            type=Path,
            default=Path("data/paper_state.db"),
            metavar="PATH",
        )
        command_parser.add_argument(
            "--verbose",
            action="store_true",
            help="show debug diagnostics on stderr",
        )
        command_parser.add_argument(
            "--json-logs",
            action="store_true",
            help="emit structured JSON operational logs",
        )
        command_parser.add_argument(
            "--telegram",
            action="store_true",
            help="enqueue Telegram notification using environment credentials",
        )

    health_parser = paper_subparsers.add_parser(
        "health",
        help="perform a network-free paper checkpoint health check",
    )
    health_parser.add_argument("--session-id", required=True)
    health_parser.add_argument(
        "--mode", choices=("single", "multi"), default="single"
    )
    health_parser.add_argument(
        "--state-database",
        type=Path,
        default=Path("data/paper_state.db"),
        metavar="PATH",
    )
    _add_operational_logging_arguments(health_parser)

    backup_parser = paper_subparsers.add_parser(
        "backup",
        help="create a consistent online SQLite backup",
    )
    backup_parser.add_argument(
        "--source-database", type=Path, required=True, metavar="PATH"
    )
    backup_parser.add_argument(
        "--output", type=Path, required=True, metavar="PATH"
    )
    _add_operational_logging_arguments(backup_parser)

    verify_parser = paper_subparsers.add_parser(
        "verify-backup",
        help="verify SQLite integrity and CryptoLab schema compatibility",
    )
    verify_parser.add_argument(
        "--backup", type=Path, required=True, metavar="PATH"
    )
    _add_operational_logging_arguments(verify_parser)

    restore_parser = paper_subparsers.add_parser(
        "restore",
        help="restore a verified backup to a new path",
    )
    restore_parser.add_argument(
        "--backup", type=Path, required=True, metavar="PATH"
    )
    restore_parser.add_argument(
        "--output", type=Path, required=True, metavar="NEW_PATH"
    )
    _add_operational_logging_arguments(restore_parser)

    return parser


def _add_paper_runtime_arguments(
    command_parser: argparse.ArgumentParser,
) -> None:
    command_parser.add_argument("--session-id", required=True)
    command_parser.add_argument(
        "--mode",
        choices=("single", "multi"),
        default="single",
        help="single strategy session or shared BTC/ETH Rotation portfolio",
    )
    command_parser.add_argument(
        "--strategy",
        choices=(
            "trend",
            "trend-pullback",
            "mean-reversion",
            "breakout",
            "rotation",
        ),
        default="trend",
    )
    command_parser.add_argument("--symbol", default="BTCUSDT")
    command_parser.add_argument("--interval", default="1h")
    command_parser.add_argument(
        "--market-database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        metavar="PATH",
    )
    command_parser.add_argument(
        "--state-database",
        type=Path,
        default=Path("data/paper_state.db"),
        metavar="PATH",
    )
    command_parser.add_argument(
        "--warmup-candles",
        type=_parse_positive_integer,
        default=100,
    )
    command_parser.add_argument(
        "--portfolio-warmup-candles",
        type=_parse_positive_integer,
        default=721,
        help="multi-symbol history; must exceed the fixed 720-hour lookback",
    )
    command_parser.add_argument(
        "--poll-seconds",
        type=_parse_positive_integer,
        default=60,
    )
    command_parser.add_argument(
        "--fee-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.001"),
    )
    command_parser.add_argument(
        "--slippage-rate",
        type=_parse_decimal_rate,
        default=Decimal("0.0005"),
    )
    command_parser.add_argument(
        "--position-size-fraction",
        type=_parse_position_size_fraction,
        default=None,
    )
    command_parser.add_argument(
        "--maximum-order-notional",
        type=_parse_positive_decimal,
        default=Decimal("1000"),
    )
    command_parser.add_argument(
        "--maximum-position-notional",
        type=_parse_positive_decimal,
        default=Decimal("1000"),
    )
    command_parser.add_argument(
        "--maximum-total-exposure",
        type=_parse_position_size_fraction,
        default=Decimal("1"),
    )
    command_parser.add_argument(
        "--minimum-available-cash",
        type=_parse_non_negative_decimal,
        default=Decimal("0"),
    )
    command_parser.add_argument(
        "--maximum-daily-loss",
        type=_parse_positive_decimal_rate,
        default=Decimal("0.05"),
    )
    command_parser.add_argument(
        "--maximum-portfolio-drawdown",
        type=_parse_positive_decimal_rate,
        default=Decimal("0.20"),
    )
    command_parser.add_argument(
        "--data-freshness-seconds",
        type=_parse_positive_integer,
        default=300,
    )
    command_parser.add_argument(
        "--quantity-step",
        type=_parse_positive_decimal,
        default=Decimal("0.00000001"),
    )
    command_parser.add_argument(
        "--minimum-quantity",
        type=_parse_positive_decimal,
        default=Decimal("0.00001"),
    )
    command_parser.add_argument(
        "--minimum-notional",
        type=_parse_positive_decimal,
        default=Decimal("5"),
    )
    command_parser.add_argument(
        "--simulated-latency-ms",
        type=_parse_nonnegative_integer,
        default=0,
    )
    command_parser.add_argument(
        "--partial-fill-fraction",
        type=_parse_position_size_fraction,
        default=Decimal("1"),
        help="deterministic fraction of requested quantity filled immediately",
    )
    command_parser.add_argument(
        "--keep-partial-order-open",
        action="store_true",
        help=(
            "retain an unfilled remainder and its reservation for recovery "
            "testing; no later fill is fabricated automatically"
        ),
    )
    command_parser.add_argument(
        "--verbose",
        action="store_true",
        help="show debug diagnostics on stderr",
    )
    command_parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit one structured JSON object per operational event",
    )
    command_parser.add_argument(
        "--metrics-host",
        default="127.0.0.1",
        help="metrics bind host (default: loopback)",
    )
    command_parser.add_argument(
        "--metrics-port",
        type=_parse_positive_integer,
        default=None,
        metavar="PORT",
        help="enable the Prometheus endpoint on this port",
    )
    command_parser.add_argument(
        "--telegram",
        action="store_true",
        help="deliver durable notifications using environment credentials",
    )


def _add_operational_logging_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show debug diagnostics on stderr",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit structured JSON operational logs",
    )


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


def _parse_positive_decimal(value: str) -> Decimal:
    """Parse any finite Decimal greater than zero."""
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(
            f"invalid decimal value: {value!r}"
        ) from error
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return result


def _parse_non_negative_decimal(value: str) -> Decimal:
    """Parse any finite Decimal greater than or equal to zero."""
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(
            f"invalid decimal value: {value!r}"
        ) from error
    if not result.is_finite() or result < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
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


def _parse_nonnegative_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if result < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return result


def _parse_positive_integer(value: str) -> int:
    result = _parse_nonnegative_integer(value)
    if result == 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def _validate_backtest_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Reject combinations whose allocation semantics would be ambiguous."""
    if (
        args.trailing_stop_rate is not None
        and args.stop_loss_rate is not None
    ):
        parser.error(
            "--trailing-stop-rate and --stop-loss-rate cannot be combined"
        )
    if (
        args.stop_loss_rate is not None
        and args.position_sell_fraction is not None
    ):
        parser.error(
            "--stop-loss-rate does not support scale-in/scale-out sizing"
        )
    if (
        args.position_sell_fraction is not None
        and args.position_size_fraction is None
    ):
        parser.error(
            "--position-sell-fraction requires --position-size-fraction"
        )

    if not args.portfolio_symbol:
        return
    symbols = [args.symbol, *args.portfolio_symbol]
    normalized = [symbol.upper() for symbol in symbols]
    if len(normalized) != len(set(normalized)):
        parser.error("portfolio symbols must be unique")
    if args.strategy == "all":
        parser.error(
            "portfolio mode requires one explicit --strategy; "
            "strategy aggregation is not supported"
        )
    if args.position_size_fraction is None:
        parser.error(
            "portfolio mode requires --position-size-fraction for explicit "
            "shared-capital allocation"
        )


def _validate_optimize_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.train_fraction + args.validation_fraction >= 1:
        parser.error(
            "--train-fraction and --validation-fraction must sum to less than 1"
        )


def _load_optimization_grids(args: argparse.Namespace):
    requested_names = {
        "trend": "trend_following",
        "mean-reversion": "mean_reversion",
        "breakout": "breakout",
    }
    selected = (
        set(requested_names.values())
        if args.strategy == "all"
        else {requested_names[args.strategy]}
    )
    if args.grid_json is None:
        return tuple(
            grid
            for grid in default_parameter_grids()
            if grid.strategy_name in selected
        )

    try:
        payload = json.loads(args.grid_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read parameter grid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Parameter grid JSON root must be an object")
    missing = selected - set(payload)
    if missing:
        raise ValueError(
            "Parameter grid JSON is missing: " + ", ".join(sorted(missing))
        )
    grids = []
    for name in sorted(selected):
        axes = payload[name]
        if not isinstance(axes, dict):
            raise ValueError(f"Grid for {name} must be a JSON object")
        grids.append(grid_from_mapping(name, axes))
    return tuple(grids)


def _create_backtest_position_sizer(
    args: argparse.Namespace,
) -> PositionSizer | None:
    """Map opt-in CLI allocation flags to existing sizing policies."""
    if args.position_sell_fraction is not None:
        return FixedFractionScaleSizer(
            buy_fraction=args.position_size_fraction,
            sell_fraction=args.position_sell_fraction,
        )
    if args.position_size_fraction is not None:
        return FixedFractionPositionSizer(args.position_size_fraction)
    return None


def _load_backtest_rows(
    repository: SQLiteRepository,
    *,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    exchange: str,
) -> tuple[FeatureRow, ...]:
    """Validate one instrument fully before constructing its features."""
    quality_report = DataQualityChecker(repository).check(
        symbol=symbol,
        interval=interval,
        start_time=start_time,
        end_time=end_time,
        as_of_time=end_time,
        **_market_identity_kwargs(exchange),
    )
    if not quality_report.is_valid:
        issues: list[str] = []
        if quality_report.missing_candles:
            issues.append(
                f"{quality_report.missing_candles} candles are missing"
            )
        if quality_report.unexpected_candles:
            issues.append(
                f"{quality_report.unexpected_candles} candles are unexpected"
            )
        if quality_report.duplicate_candles:
            issues.append(
                f"{quality_report.duplicate_candles} candles are duplicated"
            )
        if quality_report.invalid_candles:
            issues.append(
                f"{quality_report.invalid_candles} candles are invalid"
            )
        if quality_report.actual_candles != quality_report.expected_candles:
            issues.append(
                "actual candle count differs from expected count "
                f"({quality_report.actual_candles} != "
                f"{quality_report.expected_candles})"
            )
        raise ValueError(
            f"Cannot backtest invalid historical data for {symbol}: "
            + "; ".join(issues)
        )

    candles = repository.get_candles_by_range(
        symbol,
        interval,
        start_time,
        end_time,
        **_market_identity_kwargs(exchange),
    )
    return tuple(FeatureBuilder().build(
        candles,
        interval=interval,
        as_of_time=end_time,
    ))


def _create_strategies(strategy_name: str) -> list[Strategy]:
    strategies = {
        "trend": TrendFollowingStrategy,
        "trend-pullback": TrendPullbackStrategy,
        "mean-reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
    }
    if strategy_name == "all":
        return [
            TrendFollowingStrategy(),
            MeanReversionStrategy(),
            BreakoutStrategy(),
        ]

    return [strategies[strategy_name]()]


def _market_identity_kwargs(exchange: str) -> dict[str, str]:
    """Keep legacy Binance calls positional while scoping other exchanges."""
    if exchange == DEFAULT_EXCHANGE:
        return {}
    return {
        "exchange": exchange,
        "market_type": SPOT_MARKET_TYPE,
    }


def _configure_logging(verbose: bool, *, json_logs: bool = False) -> None:
    """Configure dependency-free operational logging for one CLI run."""
    if json_logs:
        configure_json_logging(
            logging.DEBUG if verbose else logging.INFO
        )
        return
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
