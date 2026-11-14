"""Versioned SQLite persistence for deterministic paper replay state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptolab.execution.adapter import ExecutionAdapterSnapshot
from cryptolab.execution.lifecycle import LifecycleSnapshot
from cryptolab.replay.models import ReplaySessionCheckpoint
from cryptolab.replay.persistence_models import (
    CrashPoint,
    InjectedReplayCrash,
    ReplayStateIntegrityError,
    StoredCandleEvent,
    StoredReplaySession,
)
from cryptolab.replay.serialization import (
    canonical_json,
    deserialize_execution_snapshot,
    serialize_execution_snapshot,
)


PAPER_STATE_SCHEMA_VERSION = 1
PAPER_STATE_SERIALIZATION_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE paper_sessions (
    session_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    serialization_version INTEGER NOT NULL,
    configuration_json TEXT NOT NULL,
    initial_risk_state_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE paper_candle_events (
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, event_id),
    UNIQUE (session_id, sequence),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_replay_events (
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, event_id),
    UNIQUE (session_id, sequence),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_decisions (
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, decision_id),
    UNIQUE (session_id, sequence),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_order_intents (
    session_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, intent_id),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_orders (
    session_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, order_id),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_fills (
    session_id TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, fill_id),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_reservations (
    session_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, order_id),
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_accounting (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_execution_adapter (
    session_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);

CREATE TABLE paper_checkpoints (
    session_id TEXT PRIMARY KEY,
    checkpoint_sequence INTEGER NOT NULL,
    candle_event_id TEXT,
    candle_open_time INTEGER,
    replay_event_count INTEGER NOT NULL,
    checkpoint_json TEXT NOT NULL,
    risk_state_json TEXT NOT NULL,
    committed_at_utc TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES paper_sessions(session_id)
);
"""


class PaperStateRepository:
    """Persist one replay candle and every derived state in one transaction."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def init_schema(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0])
            if version == 0:
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                if existing:
                    raise ReplayStateIntegrityError(
                        "Paper-state database has unversioned tables"
                    )
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    f"PRAGMA user_version = {PAPER_STATE_SCHEMA_VERSION}"
                )
                return
            if version != PAPER_STATE_SCHEMA_VERSION:
                raise ReplayStateIntegrityError(
                    f"Unsupported paper-state schema version {version}; "
                    f"expected {PAPER_STATE_SCHEMA_VERSION}"
                )
            expected = {
                "paper_sessions",
                "paper_candle_events",
                "paper_replay_events",
                "paper_decisions",
                "paper_order_intents",
                "paper_orders",
                "paper_fills",
                "paper_reservations",
                "paper_accounting",
                "paper_execution_adapter",
                "paper_checkpoints",
            }
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = expected - actual
            if missing:
                raise ReplayStateIntegrityError(
                    "Paper-state schema is incomplete: "
                    + ", ".join(sorted(missing))
                )

    def create_session(
        self,
        *,
        session_id: str,
        configuration_json: str,
        initial_risk_state_json: str,
        checkpoint: ReplaySessionCheckpoint,
        risk_state_json: str,
    ) -> None:
        self._validate_session_id(session_id)
        now = _utc_now()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO paper_sessions (
                        session_id, schema_version, serialization_version,
                        configuration_json, initial_risk_state_json,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        PAPER_STATE_SCHEMA_VERSION,
                        PAPER_STATE_SERIALIZATION_VERSION,
                        configuration_json,
                        initial_risk_state_json,
                        now,
                        now,
                    ),
                )
                self._write_state(
                    connection,
                    session_id,
                    checkpoint,
                    risk_state_json,
                    checkpoint_sequence=0,
                    checkpoint_event_id=None,
                    checkpoint_open_time=None,
                    crash_at=None,
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as error:
                connection.execute("ROLLBACK")
                raise ReplayStateIntegrityError(
                    f"Replay session already exists: {session_id}"
                ) from error
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def commit_candle(
        self,
        *,
        session_id: str,
        event_id: str,
        candle_open_time: int,
        candle_payload: str,
        checkpoint: ReplaySessionCheckpoint,
        risk_state_json: str,
        crash_at: CrashPoint | None = None,
    ) -> bool:
        """Commit the candle and all derived state; return False for a no-op."""
        self._validate_session_id(session_id)
        connection = self._connect()
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._checkpoint_row(connection, session_id)
            existing = connection.execute(
                """
                SELECT open_time, payload
                FROM paper_candle_events
                WHERE session_id = ? AND event_id = ?
                """,
                (session_id, event_id),
            ).fetchone()
            if existing is not None:
                if existing != (candle_open_time, candle_payload):
                    raise ReplayStateIntegrityError(
                        "Conflicting duplicate persistent candle event"
                    )
                connection.execute("ROLLBACK")
                return False

            next_sequence = current[0] + 1
            if len(checkpoint.candle_event_ids) != next_sequence:
                raise ReplayStateIntegrityError(
                    "Checkpoint candle count is not the next sequence"
                )
            if checkpoint.candle_event_ids[-1] != event_id:
                raise ReplayStateIntegrityError(
                    "Checkpoint does not end with the committed event"
                )
            connection.execute(
                """
                INSERT INTO paper_candle_events (
                    session_id, sequence, event_id, open_time, payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    next_sequence,
                    event_id,
                    candle_open_time,
                    candle_payload,
                ),
            )
            _inject(crash_at, CrashPoint.AFTER_EVENT_JOURNAL)
            self._write_state(
                connection,
                session_id,
                checkpoint,
                risk_state_json,
                checkpoint_sequence=next_sequence,
                checkpoint_event_id=event_id,
                checkpoint_open_time=candle_open_time,
                crash_at=crash_at,
            )
            connection.execute(
                "UPDATE paper_sessions SET updated_at_utc = ? "
                "WHERE session_id = ?",
                (_utc_now(), session_id),
            )
            connection.execute("COMMIT")
            committed = True
        except Exception:
            if not committed:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        _inject(crash_at, CrashPoint.AFTER_COMMIT)
        return True

    def load_session(self, session_id: str) -> StoredReplaySession:
        self._validate_session_id(session_id)
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT configuration_json, initial_risk_state_json,
                       schema_version, serialization_version
                FROM paper_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                raise ReplayStateIntegrityError(
                    f"Unknown replay session: {session_id}"
                )
            if session[2:] != (
                PAPER_STATE_SCHEMA_VERSION,
                PAPER_STATE_SERIALIZATION_VERSION,
            ):
                raise ReplayStateIntegrityError(
                    "Stored replay schema/serialization version is incompatible"
                )
            checkpoint = self._checkpoint_row(connection, session_id)
            candle_rows = connection.execute(
                """
                SELECT sequence, event_id, open_time, payload
                FROM paper_candle_events
                WHERE session_id = ? ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
            expected_sequence = list(range(1, len(candle_rows) + 1))
            if [row[0] for row in candle_rows] != expected_sequence:
                raise ReplayStateIntegrityError(
                    "Persistent candle sequence is not contiguous"
                )
            if checkpoint[0] != len(candle_rows):
                raise ReplayStateIntegrityError(
                    "Checkpoint does not match the committed candle count"
                )
            if candle_rows:
                if checkpoint[1:3] != candle_rows[-1][1:3]:
                    raise ReplayStateIntegrityError(
                        "Checkpoint does not identify the last candle event"
                    )
            elif checkpoint[1] is not None or checkpoint[2] is not None:
                raise ReplayStateIntegrityError(
                    "Empty session has a non-empty checkpoint identity"
                )
            return StoredReplaySession(
                session_id=session_id,
                configuration_json=session[0],
                initial_risk_state_json=session[1],
                checkpoint_sequence=checkpoint[0],
                checkpoint_event_id=checkpoint[1],
                checkpoint_open_time=checkpoint[2],
                checkpoint_json=checkpoint[4],
                risk_state_json=checkpoint[5],
                candles=tuple(
                    StoredCandleEvent(*row) for row in candle_rows
                ),
            )

    def get_candle_event(
        self,
        session_id: str,
        event_id: str,
    ) -> tuple[int, str] | None:
        """Return the immutable persisted candle identity and payload."""
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT open_time, payload FROM paper_candle_events
                WHERE session_id = ? AND event_id = ?
                """,
                (session_id, event_id),
            ).fetchone()

    def verify_checkpoint(
        self,
        session_id: str,
        checkpoint: ReplaySessionCheckpoint,
        risk_state_json: str,
    ) -> None:
        """Verify every normalized table against deterministic reconstruction."""
        with self._connect() as connection:
            row = self._checkpoint_row(connection, session_id)
            if row[0] != len(checkpoint.candle_event_ids):
                raise ReplayStateIntegrityError(
                    "Recovered checkpoint sequence does not match storage"
                )
            if row[4] != canonical_json(checkpoint):
                raise ReplayStateIntegrityError(
                    "Recovered replay checkpoint differs from storage"
                )
            if row[5] != risk_state_json:
                raise ReplayStateIntegrityError(
                    "Recovered risk-policy state differs from storage"
                )
            self._assert_normalized_state(
                connection,
                session_id,
                checkpoint,
            )

    def load_execution_snapshot(
        self,
        session_id: str,
    ) -> ExecutionAdapterSnapshot:
        """Load typed order lifecycle state including active reservations."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM paper_execution_adapter
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise ReplayStateIntegrityError(
                f"Replay session has no execution snapshot: {session_id}"
            )
        try:
            return deserialize_execution_snapshot(row[0])
        except ValueError as error:
            raise ReplayStateIntegrityError(
                "Persistent execution snapshot is invalid"
            ) from error

    def _write_state(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        checkpoint: ReplaySessionCheckpoint,
        risk_state_json: str,
        *,
        checkpoint_sequence: int,
        checkpoint_event_id: str | None,
        checkpoint_open_time: int | None,
        crash_at: CrashPoint | None,
    ) -> None:
        event_rows = [
            (event.sequence, event.event_id, canonical_json(event))
            for event in checkpoint.events
        ]
        self._sync_sequential_rows(
            connection,
            "paper_replay_events",
            "event_id",
            session_id,
            event_rows,
        )

        decision_rows = [
            (
                sequence,
                _decision_id(decision),
                canonical_json(decision),
            )
            for sequence, decision in enumerate(
                checkpoint.decisions,
                start=1,
            )
        ]
        self._sync_sequential_rows(
            connection,
            "paper_decisions",
            "decision_id",
            session_id,
            decision_rows,
        )
        _inject(crash_at, CrashPoint.AFTER_DECISION)

        self._sync_immutable_rows(
            connection,
            "paper_order_intents",
            "intent_id",
            session_id,
            [
                (intent.intent_id, canonical_json(intent))
                for intent in checkpoint.execution.order_intents
            ],
        )
        _inject(crash_at, CrashPoint.AFTER_ORDER_INTENTS)

        connection.execute(
            "DELETE FROM paper_orders WHERE session_id = ?",
            (session_id,),
        )
        connection.executemany(
            """
            INSERT INTO paper_orders (session_id, order_id, status, payload)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    order.order_id,
                    order.status.value,
                    canonical_json(order),
                )
                for order in checkpoint.execution.orders
            ],
        )
        _inject(crash_at, CrashPoint.AFTER_ORDERS)

        self._sync_immutable_rows(
            connection,
            "paper_fills",
            "fill_id",
            session_id,
            [
                (fill.fill_id, canonical_json(fill))
                for fill in checkpoint.execution.execution_fills
            ],
        )
        _inject(crash_at, CrashPoint.AFTER_FILLS)

        connection.execute(
            "DELETE FROM paper_reservations WHERE session_id = ?",
            (session_id,),
        )
        connection.executemany(
            """
            INSERT INTO paper_reservations (session_id, order_id, payload)
            VALUES (?, ?, ?)
            """,
            [
                (
                    session_id,
                    reservation.order_id,
                    canonical_json(reservation),
                )
                for reservation in checkpoint.execution.reservations
            ],
        )
        _inject(crash_at, CrashPoint.AFTER_RESERVATIONS)

        adapter_snapshot = _execution_snapshot(checkpoint)
        connection.execute(
            """
            INSERT INTO paper_execution_adapter (session_id, payload)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload
            """,
            (session_id, serialize_execution_snapshot(adapter_snapshot)),
        )

        accounting_json = canonical_json(checkpoint.execution)
        connection.execute(
            """
            INSERT INTO paper_accounting (session_id, payload)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload
            """,
            (session_id, accounting_json),
        )
        _inject(crash_at, CrashPoint.AFTER_ACCOUNTING)
        _inject(crash_at, CrashPoint.BEFORE_CHECKPOINT)

        connection.execute(
            """
            INSERT INTO paper_checkpoints (
                session_id, checkpoint_sequence, candle_event_id,
                candle_open_time, replay_event_count, checkpoint_json,
                risk_state_json, committed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                checkpoint_sequence = excluded.checkpoint_sequence,
                candle_event_id = excluded.candle_event_id,
                candle_open_time = excluded.candle_open_time,
                replay_event_count = excluded.replay_event_count,
                checkpoint_json = excluded.checkpoint_json,
                risk_state_json = excluded.risk_state_json,
                committed_at_utc = excluded.committed_at_utc
            """,
            (
                session_id,
                checkpoint_sequence,
                checkpoint_event_id,
                checkpoint_open_time,
                len(checkpoint.events),
                canonical_json(checkpoint),
                risk_state_json,
                _utc_now(),
            ),
        )

    def _assert_normalized_state(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        checkpoint: ReplaySessionCheckpoint,
    ) -> None:
        expected_events = [
            (event.sequence, event.event_id, canonical_json(event))
            for event in checkpoint.events
        ]
        actual_events = connection.execute(
            """
            SELECT sequence, event_id, payload FROM paper_replay_events
            WHERE session_id = ? ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
        if actual_events != expected_events:
            raise ReplayStateIntegrityError("Replay event journal diverged")

        expected_decisions = [
            (sequence, _decision_id(item), canonical_json(item))
            for sequence, item in enumerate(checkpoint.decisions, start=1)
        ]
        actual_decisions = connection.execute(
            """
            SELECT sequence, decision_id, payload FROM paper_decisions
            WHERE session_id = ? ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
        if actual_decisions != expected_decisions:
            raise ReplayStateIntegrityError("Persistent decisions diverged")

        comparisons = (
            (
                "paper_order_intents",
                "intent_id",
                sorted(
                    (
                        item.intent_id,
                        canonical_json(item),
                    )
                    for item in checkpoint.execution.order_intents
                ),
            ),
            (
                "paper_fills",
                "fill_id",
                sorted(
                    (item.fill_id, canonical_json(item))
                    for item in checkpoint.execution.execution_fills
                ),
            ),
            (
                "paper_reservations",
                "order_id",
                sorted(
                    (item.order_id, canonical_json(item))
                    for item in checkpoint.execution.reservations
                ),
            ),
        )
        for table, key_column, expected in comparisons:
            actual = connection.execute(
                f"SELECT {key_column}, payload FROM {table} "
                f"WHERE session_id = ? ORDER BY {key_column}",
                (session_id,),
            ).fetchall()
            if actual != expected:
                raise ReplayStateIntegrityError(
                    f"Persistent {table} state diverged"
                )

        expected_orders = sorted(
            (
                item.order_id,
                item.status.value,
                canonical_json(item),
            )
            for item in checkpoint.execution.orders
        )
        actual_orders = connection.execute(
            """
            SELECT order_id, status, payload FROM paper_orders
            WHERE session_id = ? ORDER BY order_id
            """,
            (session_id,),
        ).fetchall()
        if actual_orders != expected_orders:
            raise ReplayStateIntegrityError("Persistent order state diverged")
        accounting = connection.execute(
            "SELECT payload FROM paper_accounting WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if accounting != (canonical_json(checkpoint.execution),):
            raise ReplayStateIntegrityError("Persistent accounting diverged")
        adapter = connection.execute(
            """
            SELECT payload FROM paper_execution_adapter WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        expected_adapter = serialize_execution_snapshot(
            _execution_snapshot(checkpoint)
        )
        if adapter != (expected_adapter,):
            raise ReplayStateIntegrityError(
                "Persistent execution adapter state diverged"
            )

    @staticmethod
    def _sync_sequential_rows(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        session_id: str,
        expected: list[tuple[int, str, str]],
    ) -> None:
        existing = connection.execute(
            f"SELECT sequence, {id_column}, payload FROM {table} "
            "WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        if existing != expected[:len(existing)]:
            raise ReplayStateIntegrityError(
                f"Stored {table} rows are not a deterministic prefix"
            )
        connection.executemany(
            f"INSERT INTO {table} "
            f"(session_id, sequence, {id_column}, payload) "
            "VALUES (?, ?, ?, ?)",
            [
                (session_id, sequence, row_id, payload)
                for sequence, row_id, payload in expected[len(existing):]
            ],
        )

    @staticmethod
    def _sync_immutable_rows(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        session_id: str,
        expected: list[tuple[str, str]],
    ) -> None:
        existing = dict(connection.execute(
            f"SELECT {id_column}, payload FROM {table} WHERE session_id = ?",
            (session_id,),
        ).fetchall())
        expected_map = dict(expected)
        if len(expected_map) != len(expected):
            raise ReplayStateIntegrityError(f"Duplicate {table} identifiers")
        if set(existing) - set(expected_map):
            raise ReplayStateIntegrityError(f"Stored {table} has extra rows")
        for row_id, payload in existing.items():
            if expected_map[row_id] != payload:
                raise ReplayStateIntegrityError(
                    f"Conflicting immutable {table} row: {row_id}"
                )
        connection.executemany(
            f"INSERT INTO {table} (session_id, {id_column}, payload) "
            "VALUES (?, ?, ?)",
            [
                (session_id, row_id, payload)
                for row_id, payload in expected
                if row_id not in existing
            ],
        )

    @staticmethod
    def _checkpoint_row(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> tuple[int, str | None, int | None, int, str, str]:
        row = connection.execute(
            """
            SELECT checkpoint_sequence, candle_event_id, candle_open_time,
                   replay_event_count, checkpoint_json, risk_state_json
            FROM paper_checkpoints WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise ReplayStateIntegrityError(
                f"Replay session has no checkpoint: {session_id}"
            )
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")


def _decision_id(decision) -> str:
    return ":".join((
        decision.strategy_name,
        decision.symbol,
        decision.interval,
        str(decision.candle_open_time),
    ))


def _execution_snapshot(
    checkpoint: ReplaySessionCheckpoint,
) -> ExecutionAdapterSnapshot:
    execution = checkpoint.execution
    return ExecutionAdapterSnapshot(
        lifecycle=LifecycleSnapshot(
            orders=execution.orders,
            fills=execution.execution_fills,
            reservations=execution.reservations,
        ),
        intents=execution.order_intents,
        events=execution.execution_events,
        next_sequence=len(execution.execution_events) + 1,
    )


def _inject(actual: CrashPoint | None, expected: CrashPoint) -> None:
    if actual is expected:
        raise InjectedReplayCrash(expected)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
