import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cryptolab.cli import main
from cryptolab.execution.adapter import SimulatedExecutionAdapter
from cryptolab.execution.models import (
    Instrument,
    IntentSource,
    OrderIntent,
    OrderSide,
)
from cryptolab.paper.notifications import (
    NotificationOutbox,
    TelegramNotifier,
)
from cryptolab.paper.observability import (
    JsonLogFormatter,
    MetricsServer,
    PaperMetrics,
    PaperTelemetry,
    log_event,
)
from cryptolab.paper.operations import (
    backup_sqlite,
    paper_health,
    restore_sqlite,
    verify_sqlite_database,
)
from cryptolab.replay.serialization import (
    canonical_json,
    serialize_execution_snapshot,
)
from cryptolab.storage.paper_state_repository import PaperStateRepository


class Clock:
    def __init__(self, now: int = 1_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def repository(tmp_path: Path) -> PaperStateRepository:
    result = PaperStateRepository(tmp_path / "paper.db")
    result.init_schema()
    return result


def create_empty_portfolio_session(repo: PaperStateRepository) -> None:
    repo.create_portfolio_session(
        session_id="ops",
        configuration_json=canonical_json({"kind": "test"}),
        checkpoint_json=canonical_json({"kind": "test"}),
        execution_json=serialize_execution_snapshot(
            SimulatedExecutionAdapter().snapshot()
        ),
        risk_state_json=canonical_json({}),
        diagnostics_json=canonical_json({"diagnostics": []}),
    )


def test_json_logging_is_structured_and_omits_secret_attributes() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        "cryptolab.test",
        logging.INFO,
        __file__,
        1,
        "paper started",
        (),
        None,
    )
    record.session_id = "session-1"
    record.event_type = "startup"
    record.correlation_id = "cycle-1"
    record.api_key = "must-not-appear"

    payload = json.loads(formatter.format(record))

    assert payload["event_type"] == "startup"
    assert payload["session_id"] == "session-1"
    assert payload["correlation_id"] == "cycle-1"
    assert "must-not-appear" not in json.dumps(payload)
    assert "api_key" not in payload


def test_log_event_adds_only_supported_context(caplog) -> None:
    logger = logging.getLogger("cryptolab.stage5-test")
    with caplog.at_level(logging.INFO):
        log_event(
            logger,
            logging.INFO,
            "fill_executed",
            "fill",
            session_id="paper",
            fill_id="fill-1",
            unsupported="ignored",
        )

    record = caplog.records[-1]
    assert record.event_type == "fill_executed"
    assert record.fill_id == "fill-1"
    assert not hasattr(record, "unsupported")


def test_prometheus_metrics_render_required_names_and_restore_counts() -> None:
    metrics = PaperMetrics()
    metrics.set("cryptolab_paper_cash_usdt", Decimal("812.25"))
    metrics.set_counter_at_least("cryptolab_paper_orders_total", 7)
    metrics.set_counter_at_least("cryptolab_paper_orders_total", 3)
    rendered = metrics.render().decode("ascii")

    assert "# TYPE cryptolab_paper_orders_total counter" in rendered
    assert "cryptolab_paper_orders_total 7" in rendered
    assert "cryptolab_paper_cash_usdt 812.25" in rendered
    assert "cryptolab_paper_reconciliation_failures_total" in rendered
    assert "cryptolab_paper_last_candle_timestamp_seconds" in rendered


def test_metrics_server_bind_failure_is_non_fatal(monkeypatch) -> None:
    def fail(*args, **kwargs):
        del args, kwargs
        raise OSError("address unavailable")

    monkeypatch.setattr(
        "cryptolab.paper.observability.ThreadingHTTPServer", fail
    )

    assert MetricsServer(PaperMetrics()).start() is False


def test_telegram_success_uses_mock_transport_without_leaking_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramNotifier(
        "secret-token",
        "12345",
        transport=httpx.MockTransport(handler),
    )
    notifier.send("BUY BTCUSDT")
    notifier.close()

    assert len(requests) == 1
    assert json.loads(requests[0].content)["chat_id"] == "12345"


def test_outbox_deduplicates_and_recovers_after_delivery_failure(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    clock = Clock()
    calls = 0

    def failing(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    first = NotificationOutbox(
        repo,
        TelegramNotifier(
            "secret-token",
            "123",
            transport=httpx.MockTransport(failing),
        ),
        now_ms=clock,
        minimum_interval_ms=0,
    )
    assert first.enqueue(
        session_id="ops",
        event_type="fill",
        dedup_key="fill-1",
        message="SELL BTCUSDT",
    ) is True
    assert first.enqueue(
        session_id="ops",
        event_type="fill",
        dedup_key="fill-1",
        message="SELL BTCUSDT",
    ) is False
    assert first.flush() == (0, 1)
    first.close()
    assert calls == 1
    assert repo.notification_counts() == (1, 0)

    clock.now = 3_000

    def success(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"ok": True, "result": {}},
        )

    recovered = NotificationOutbox(
        repo,
        TelegramNotifier(
            "secret-token",
            "123",
            transport=httpx.MockTransport(success),
        ),
        now_ms=clock,
        minimum_interval_ms=0,
    )
    assert recovered.flush() == (1, 0)
    assert repo.notification_counts() == (0, 1)
    recovered.close()


def test_telemetry_primes_durable_counts_and_gracefully_stops() -> None:
    metrics = PaperMetrics()
    telemetry = PaperTelemetry(
        session_id="ops",
        mode="multi",
        metrics=metrics,
        now_ms=Clock(7_200_000),
    )
    telemetry.started(
        recovered=True,
        orders=(),
        fills=(),
        checkpoint_sequence=9,
    )
    telemetry.portfolio(
        cash=Decimal("1000"),
        equity=Decimal("950"),
        paused=True,
        last_candle_open_time=3_600_000,
        decision_available_time=7_200_000,
        interval_ms=3_600_000,
        risk_rejections=2,
        reconciled=False,
        correlation_id="cycle-1",
    )

    assert metrics.value("cryptolab_paper_up") == 1
    assert metrics.value("cryptolab_paper_recovery_total") == 1
    assert metrics.value("cryptolab_paper_risk_rejections_total") == 2
    assert metrics.value(
        "cryptolab_paper_reconciliation_failures_total"
    ) == 1
    assert metrics.value("cryptolab_paper_paused") == 1

    telemetry.stopped(9)
    assert metrics.value("cryptolab_paper_up") == 0


def test_active_sqlite_backup_verify_and_restore_to_new_path(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_empty_portfolio_session(repo)
    active = sqlite3.connect(tmp_path / "paper.db")
    backup = tmp_path / "backups" / "paper.db"

    report = backup_sqlite(tmp_path / "paper.db", backup)

    assert report.database_type == "paper_state"
    assert report.integrity_ok is True
    assert verify_sqlite_database(backup).schema_version == 4
    restored = tmp_path / "restored" / "paper.db"
    assert restore_sqlite(backup, restored).integrity_ok is True
    restored_repo = PaperStateRepository(restored)
    assert restored_repo.load_portfolio_session("ops") == (
        repo.load_portfolio_session("ops")
    )
    assert paper_health(
        restored, session_id="ops", mode="multi"
    ).healthy is True
    with pytest.raises(ValueError, match="already exists"):
        restore_sqlite(backup, restored)
    active.close()


def test_health_fails_for_unreconciled_portfolio(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_empty_portfolio_session(repo)
    with sqlite3.connect(tmp_path / "paper.db") as connection:
        connection.execute(
            "UPDATE paper_portfolio_checkpoints SET reconciled = 0 "
            "WHERE session_id = 'ops'"
        )

    report = paper_health(
        tmp_path / "paper.db", session_id="ops", mode="multi"
    )

    assert report.healthy is False
    assert report.reconciled is False


def test_health_requires_recent_progress_when_lag_limit_is_enabled(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create_empty_portfolio_session(repo)
    with sqlite3.connect(tmp_path / "paper.db") as connection:
        connection.execute(
            "UPDATE paper_portfolio_checkpoints "
            "SET candle_sequence = 1, last_open_time = ? "
            "WHERE session_id = 'ops'",
            (9 * 3_600_000,),
        )

    fresh = paper_health(
        tmp_path / "paper.db",
        session_id="ops",
        mode="multi",
        maximum_progress_lag_seconds=0,
        now_ms=10 * 3_600_000,
    )
    stale = paper_health(
        tmp_path / "paper.db",
        session_id="ops",
        mode="multi",
        maximum_progress_lag_seconds=3_600,
        now_ms=12 * 3_600_000,
    )

    assert fresh.healthy is True
    assert fresh.progress_current is True
    assert fresh.progress_lag_seconds == 0
    assert stale.healthy is False
    assert stale.progress_current is False
    assert stale.progress_lag_seconds == 7_200


def test_backup_verification_rejects_incomplete_schema(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_empty_portfolio_session(repo)
    with sqlite3.connect(tmp_path / "paper.db") as connection:
        connection.execute("DROP TABLE paper_fills")

    with pytest.raises(ValueError, match="missing tables: paper_fills"):
        verify_sqlite_database(tmp_path / "paper.db")


def test_market_backup_verification_rejects_wrong_primary_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE candles (
                exchange TEXT NOT NULL, market_type TEXT NOT NULL,
                symbol TEXT NOT NULL, interval TEXT NOT NULL,
                open_time INTEGER NOT NULL, open TEXT NOT NULL,
                high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
                volume TEXT NOT NULL, close_time INTEGER NOT NULL,
                quote_volume TEXT NOT NULL, number_of_trades INTEGER,
                taker_buy_base_volume TEXT, taker_buy_quote_volume TEXT,
                PRIMARY KEY (symbol, interval, open_time)
            )"""
        )
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(ValueError, match="table structure differs: candles"):
        verify_sqlite_database(database)


def test_backup_cli_round_trip(tmp_path: Path, capsys) -> None:
    repo = repository(tmp_path)
    create_empty_portfolio_session(repo)
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"

    assert main([
        "paper",
        "backup",
        "--source-database",
        str(tmp_path / "paper.db"),
        "--output",
        str(backup),
    ]) == 0
    assert main([
        "paper", "verify-backup", "--backup", str(backup)
    ]) == 0
    assert main([
        "paper",
        "restore",
        "--backup",
        str(backup),
        "--output",
        str(restored),
    ]) == 0
    assert main([
        "paper",
        "health",
        "--mode",
        "multi",
        "--session-id",
        "ops",
        "--state-database",
        str(restored),
    ]) == 1
    assert "Backup verified" in capsys.readouterr().out


def test_outbox_fill_event_reuses_existing_execution_models() -> None:
    instrument = Instrument("bybit", "spot", "BTCUSDT", "BTC", "USDT")
    intent = OrderIntent(
        intent_id="intent-1",
        source_event_id="decision-1",
        source=IntentSource.STRATEGY,
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        created_at=1,
        earliest_execution_at=1,
        signal_available_time=1,
        source_candle_open_time=0,
        explanation="test",
    )
    adapter = SimulatedExecutionAdapter()
    order = adapter.submit(
        intent,
        order_id="order-1",
        created_at=1,
        reservation_amount=Decimal("1"),
        available_balance=Decimal("1000"),
    )

    metrics = PaperMetrics()
    telemetry = PaperTelemetry(
        session_id="ops", mode="multi", metrics=metrics
    )
    telemetry.started(
        recovered=False,
        orders=(),
        fills=(),
        checkpoint_sequence=0,
    )
    telemetry.execution((order,), (), correlation_id="cycle")

    assert metrics.value("cryptolab_paper_orders_total") == 1
    assert metrics.value("cryptolab_paper_pending_orders") == 1


def test_outbox_storage_failure_cannot_break_paper_telemetry(caplog) -> None:
    class BrokenOutbox:
        def enqueue(self, **kwargs) -> bool:
            del kwargs
            raise sqlite3.OperationalError("outbox unavailable")

        def flush(self) -> tuple[int, int]:
            raise sqlite3.OperationalError("outbox unavailable")

    telemetry = PaperTelemetry(
        session_id="ops",
        mode="multi",
        outbox=BrokenOutbox(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING):
        telemetry.event(
            "fill_executed",
            "simulated fill remains durable",
            dedup_key="fill-1",
            notify=True,
        )
        telemetry.flush()

    event_types = {
        getattr(record, "event_type", None) for record in caplog.records
    }
    assert "notification_enqueue_failed" in event_types
    assert "notification_flush_failed" in event_types
