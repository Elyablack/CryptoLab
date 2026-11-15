"""Dependency-free structured logging and Prometheus paper metrics."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType

from cryptolab.execution.models import Fill, Order, OrderStatus
from cryptolab.paper.notifications import NotificationOutbox


SAFE_LOG_FIELDS = (
    "event_type",
    "session_id",
    "mode",
    "symbol",
    "candle_timestamp",
    "decision_id",
    "order_id",
    "fill_id",
    "correlation_id",
    "error_type",
    "reason",
)
SENSITIVE_FRAGMENTS = ("token", "secret", "password", "api_key", "chat_id")


class JsonLogFormatter(logging.Formatter):
    """Emit one bounded JSON object without arbitrary record attributes."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, UTC
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in SAFE_LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _safe_value(field, value)
        if record.exc_info is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def configure_json_logging(level: int) -> None:
    """Replace root handlers with stdout/stderr-safe structured logging."""
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level)


def log_event(
    logger: logging.Logger,
    level: int,
    event_type: str,
    message: str,
    **fields: object,
) -> None:
    extra = {
        "event_type": event_type,
        **{
            key: _safe_value(key, value)
            for key, value in fields.items()
            if key in SAFE_LOG_FIELDS and value is not None
        },
    }
    logger.log(level, message, extra=extra)


METRIC_DEFINITIONS = MappingProxyType({
    "cryptolab_paper_up": ("gauge", "Paper process operational state."),
    "cryptolab_paper_last_candle_timestamp_seconds": (
        "gauge",
        "Open timestamp of the latest durable closed candle set.",
    ),
    "cryptolab_paper_last_decision_timestamp_seconds": (
        "gauge",
        "Availability timestamp of the latest evaluated decision.",
    ),
    "cryptolab_paper_data_lag_seconds": (
        "gauge",
        "Age of the latest expected candle availability.",
    ),
    "cryptolab_paper_orders_total": (
        "counter",
        "Durable simulated orders in the current paper session.",
    ),
    "cryptolab_paper_fills_total": (
        "counter",
        "Durable simulated fills in the current paper session.",
    ),
    "cryptolab_paper_risk_rejections_total": (
        "counter",
        "Durable risk-policy rejection count.",
    ),
    "cryptolab_paper_recovery_total": (
        "counter",
        "Session recoveries performed by this process.",
    ),
    "cryptolab_paper_reconciliation_failures_total": (
        "counter",
        "Reconciliation failures observed by this process.",
    ),
    "cryptolab_paper_cash_usdt": (
        "gauge",
        "Available shared or single-session USDT cash.",
    ),
    "cryptolab_paper_equity_usdt": (
        "gauge",
        "Latest marked paper portfolio equity in USDT.",
    ),
    "cryptolab_paper_drawdown_ratio": (
        "gauge",
        "Positive peak-to-current equity drawdown ratio.",
    ),
    "cryptolab_paper_pending_orders": (
        "gauge",
        "Current non-terminal simulated order count.",
    ),
    "cryptolab_paper_paused": (
        "gauge",
        "One when new BUY intents are manually paused.",
    ),
})


class PaperMetrics:
    """Thread-safe in-memory Prometheus text exposition registry."""

    def __init__(self) -> None:
        self._values: dict[str, Decimal] = {
            name: Decimal("0") for name in METRIC_DEFINITIONS
        }
        self._lock = threading.Lock()

    def set(self, name: str, value: Decimal | int) -> None:
        decimal_value = _metric_decimal(name, value)
        with self._lock:
            self._values[name] = decimal_value

    def set_counter_at_least(self, name: str, value: int) -> None:
        decimal_value = _metric_decimal(name, value)
        with self._lock:
            self._values[name] = max(self._values[name], decimal_value)

    def increment(self, name: str, amount: int = 1) -> None:
        decimal_amount = _metric_decimal(name, amount)
        with self._lock:
            self._values[name] += decimal_amount

    def value(self, name: str) -> Decimal:
        with self._lock:
            return self._values[name]

    def render(self) -> bytes:
        with self._lock:
            values = dict(self._values)
        lines: list[str] = []
        for name, (metric_type, help_text) in METRIC_DEFINITIONS.items():
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
            lines.append(f"{name} {values[name]}")
        return ("\n".join(lines) + "\n").encode("ascii")


class MetricsServer:
    """Small read-only HTTP server; failures never alter trading state."""

    def __init__(
        self,
        metrics: PaperMetrics,
        *,
        host: str = "127.0.0.1",
        port: int = 9108,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("metrics host must be non-empty")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise ValueError("metrics port must be between 1 and 65535")
        self.metrics = metrics
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._server is not None:
            return True
        metrics = self.metrics

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/metrics":
                    body = metrics.render()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "text/plain; version=0.0.4"
                    )
                elif self.path == "/healthz":
                    body = b"ok\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                else:
                    body = b"not found\n"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        try:
            server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as error:
            log_event(
                logging.getLogger(__name__),
                logging.ERROR,
                "metrics_start_failed",
                "Metrics endpoint could not start",
                error_type=type(error).__name__,
            )
            return False
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="cryptolab-metrics",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return True

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


class PaperTelemetry:
    """Operational side effects kept outside financial transactions."""

    def __init__(
        self,
        *,
        session_id: str,
        mode: str,
        metrics: PaperMetrics | None = None,
        outbox: NotificationOutbox | None = None,
        now_ms=None,
    ) -> None:
        if mode not in {"single", "multi"}:
            raise ValueError("paper telemetry mode must be single or multi")
        self.session_id = session_id
        self.mode = mode
        self.metrics = metrics or PaperMetrics()
        self.outbox = outbox
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._seen_order_ids: set[str] = set()
        self._seen_fill_ids: set[str] = set()
        self._last_risk_rejections = 0
        self._reconciliation_failed = False

    def started(
        self,
        *,
        recovered: bool,
        orders: Sequence[Order],
        fills: Sequence[Fill],
        checkpoint_sequence: int,
    ) -> None:
        self._seen_order_ids = {item.order_id for item in orders}
        self._seen_fill_ids = {item.fill_id for item in fills}
        self.metrics.set("cryptolab_paper_up", 1)
        self.metrics.set_counter_at_least(
            "cryptolab_paper_orders_total", len(orders)
        )
        self.metrics.set_counter_at_least(
            "cryptolab_paper_fills_total", len(fills)
        )
        if recovered:
            self.metrics.increment("cryptolab_paper_recovery_total")
            self.event(
                "recovery",
                "Paper session recovered from durable state",
                dedup_key=f"recovery:{checkpoint_sequence}",
                notify=True,
            )
        self.event(
            "startup",
            "Paper process started",
            dedup_key=f"startup:{checkpoint_sequence}",
            notify=True,
        )

    def execution(
        self,
        orders: Sequence[Order],
        fills: Sequence[Fill],
        *,
        correlation_id: str,
    ) -> None:
        orders_by_id = {item.order_id: item for item in orders}
        for order in orders:
            if order.order_id in self._seen_order_ids:
                continue
            self._seen_order_ids.add(order.order_id)
            self.event(
                "order_created",
                "Simulated paper order created",
                order_id=order.order_id,
                symbol=order.instrument.symbol,
                correlation_id=correlation_id,
            )
        for fill in fills:
            if fill.fill_id in self._seen_fill_ids:
                continue
            self._seen_fill_ids.add(fill.fill_id)
            order = orders_by_id.get(fill.order_id)
            event_type = "partial_fill" if (
                order is not None
                and (
                    order.status is OrderStatus.PARTIALLY_FILLED
                    or fill.quantity < order.requested_quantity
                )
            ) else "fill_executed"
            self.event(
                event_type,
                f"{fill.side.value} {fill.instrument.symbol} simulated fill",
                dedup_key=fill.fill_id,
                notify=True,
                symbol=fill.instrument.symbol,
                order_id=fill.order_id,
                fill_id=fill.fill_id,
                correlation_id=correlation_id,
            )
        self.metrics.set_counter_at_least(
            "cryptolab_paper_orders_total", len(orders)
        )
        self.metrics.set_counter_at_least(
            "cryptolab_paper_fills_total", len(fills)
        )
        self.metrics.set(
            "cryptolab_paper_pending_orders",
            sum(not item.status.is_terminal for item in orders),
        )

    def portfolio(
        self,
        *,
        cash: Decimal,
        equity: Decimal,
        paused: bool,
        last_candle_open_time: int | None,
        decision_available_time: int | None,
        interval_ms: int,
        risk_rejections: int,
        reconciled: bool,
        correlation_id: str,
        peak_equity: Decimal | None = None,
    ) -> None:
        now = self._now_ms()
        self.metrics.set("cryptolab_paper_cash_usdt", cash)
        self.metrics.set("cryptolab_paper_equity_usdt", equity)
        self.metrics.set("cryptolab_paper_paused", int(paused))
        if last_candle_open_time is not None:
            self.metrics.set(
                "cryptolab_paper_last_candle_timestamp_seconds",
                Decimal(last_candle_open_time) / 1000,
            )
            available = last_candle_open_time + interval_ms
            lag_ms = max(0, now - available)
            self.metrics.set(
                "cryptolab_paper_data_lag_seconds",
                Decimal(lag_ms) / 1000,
            )
            if lag_ms > interval_ms * 2:
                self.event(
                    "no_new_candle",
                    "No new closed candle for more than two intervals",
                    dedup_key=f"no-candle:{last_candle_open_time}",
                    notify=True,
                    candle_timestamp=last_candle_open_time,
                    correlation_id=correlation_id,
                )
        if decision_available_time is not None:
            self.metrics.set(
                "cryptolab_paper_last_decision_timestamp_seconds",
                Decimal(decision_available_time) / 1000,
            )
        if risk_rejections > self._last_risk_rejections:
            difference = risk_rejections - self._last_risk_rejections
            self.metrics.set_counter_at_least(
                "cryptolab_paper_risk_rejections_total", risk_rejections
            )
            self.event(
                "risk_rejection",
                f"Paper risk policy rejected {difference} decision(s)",
                dedup_key=f"risk:{risk_rejections}",
                notify=True,
                correlation_id=correlation_id,
            )
        self._last_risk_rejections = risk_rejections
        if equity > 0:
            # Peak equity is deliberately maintained here as a process gauge;
            # the durable risk manager remains the financial authority.
            peak = (
                peak_equity
                if peak_equity is not None
                else getattr(self, "_peak_equity", equity)
            )
            peak = max(peak, equity)
            self._peak_equity = peak
            self.metrics.set(
                "cryptolab_paper_drawdown_ratio",
                max(ZERO, (peak - equity) / peak),
            )
        if not reconciled and not self._reconciliation_failed:
            self.metrics.increment(
                "cryptolab_paper_reconciliation_failures_total"
            )
            self.event(
                "reconciliation_failure",
                "Paper reconciliation failed; execution is fail-closed",
                dedup_key=f"reconciliation:{last_candle_open_time}",
                notify=True,
                correlation_id=correlation_id,
            )
        self._reconciliation_failed = not reconciled

    def stale(
        self,
        *,
        candle_open_time: int | None,
        correlation_id: str,
    ) -> None:
        self.event(
            "stale_market_data",
            "Newest synchronized market data is stale; BUY is suppressed",
            dedup_key=f"stale:{candle_open_time}",
            notify=True,
            candle_timestamp=candle_open_time,
            correlation_id=correlation_id,
        )

    def event(
        self,
        event_type: str,
        message: str,
        *,
        dedup_key: str | None = None,
        notify: bool = False,
        level: int = logging.INFO,
        **fields: object,
    ) -> None:
        logger = logging.getLogger("cryptolab.paper.operations")
        log_event(
            logger,
            level,
            event_type,
            message,
            session_id=self.session_id,
            mode=self.mode,
            **fields,
        )
        if notify and self.outbox is not None and dedup_key is not None:
            try:
                self.outbox.enqueue(
                    session_id=self.session_id,
                    event_type=event_type,
                    dedup_key=dedup_key,
                    message=(
                        f"CryptoLab [{self.mode}] {self.session_id}: {message}"
                    ),
                )
            except Exception as error:
                log_event(
                    logger,
                    logging.WARNING,
                    "notification_enqueue_failed",
                    "Notification enqueue failed; paper state is unaffected",
                    session_id=self.session_id,
                    mode=self.mode,
                    error_type=type(error).__name__,
                )

    def flush(self) -> None:
        if self.outbox is None:
            return
        try:
            sent, failed = self.outbox.flush()
        except Exception as error:
            self.event(
                "notification_flush_failed",
                "Notification flush failed; paper state is unaffected",
                level=logging.WARNING,
                error_type=type(error).__name__,
            )
            return
        if sent or failed:
            self.event(
                "notification_delivery",
                f"Telegram outbox: sent={sent}, failed={failed}",
                level=logging.INFO if failed == 0 else logging.WARNING,
            )

    def stopped(self, checkpoint_sequence: int) -> None:
        self.metrics.set("cryptolab_paper_up", 0)
        self.event(
            "shutdown",
            "Paper process stopped",
            dedup_key=f"shutdown:{checkpoint_sequence}",
            notify=True,
        )
        self.flush()


ZERO = Decimal("0")


def _metric_decimal(name: str, value: Decimal | int) -> Decimal:
    if name not in METRIC_DEFINITIONS:
        raise ValueError(f"Unknown paper metric: {name}")
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError("Metric value must be Decimal or integer")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Metric value must be finite")
    return result


def _safe_value(name: str, value: object) -> object:
    lowered = name.lower()
    if any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)
