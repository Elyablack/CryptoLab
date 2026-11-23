"""Health, SQLite backup, verification, and non-destructive restore."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.storage.paper_state_repository import (
    PAPER_STATE_SCHEMA_VERSION,
    PAPER_STATE_SERIALIZATION_VERSION,
    SCHEMA_SQL as PAPER_SCHEMA_SQL,
)
from cryptolab.storage.sqlite_repository import (
    CREATE_CANDLES_TABLE_SQL,
    SCHEMA_VERSION,
)


@dataclass(frozen=True)
class DatabaseVerification:
    path: Path
    database_type: str
    schema_version: int
    integrity_ok: bool


@dataclass(frozen=True)
class PaperHealthReport:
    session_id: str
    mode: str
    healthy: bool
    schema_version: int
    checkpoint_sequence: int
    last_candle_open_time: int | None
    reconciled: bool
    progress_current: bool
    progress_lag_seconds: int | None
    message: str


def verify_sqlite_database(path: str | Path) -> DatabaseVerification:
    """Validate integrity plus the complete CryptoLab schema contract."""
    database = Path(path)
    if not database.is_file():
        raise ValueError(f"SQLite database does not exist: {database}")
    with _read_only_connection(database) as connection:
        integrity_rows = connection.execute(
            "PRAGMA integrity_check"
        ).fetchall()
        integrity_ok = integrity_rows == [("ok",)]
        if not integrity_ok:
            raise ValueError("SQLite integrity_check failed")
        version = int(connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0])
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "paper_sessions" in tables or "paper_checkpoints" in tables:
            database_type = "paper_state"
            expected = PAPER_STATE_SCHEMA_VERSION
            schema_sql = PAPER_SCHEMA_SQL
        elif "candles" in tables:
            database_type = "market_data"
            expected = SCHEMA_VERSION
            schema_sql = CREATE_CANDLES_TABLE_SQL
        else:
            raise ValueError("Unrecognized CryptoLab SQLite schema")
        if version != expected:
            raise ValueError(
                f"Unsupported {database_type} schema version {version}; "
                f"expected {expected}"
            )
        _verify_schema_contract(connection, schema_sql, database_type)
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise ValueError(
                f"{database_type} foreign-key integrity check failed"
            )
        if database_type == "paper_state":
            _verify_paper_versions(connection)
    return DatabaseVerification(
        path=database,
        database_type=database_type,
        schema_version=version,
        integrity_ok=True,
    )


def _verify_schema_contract(
    actual: sqlite3.Connection,
    schema_sql: str,
    database_type: str,
) -> None:
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(schema_sql)
        expected_tables = {
            row[0]
            for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        actual_tables = {
            row[0]
            for row in actual.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = expected_tables - actual_tables
        if missing:
            raise ValueError(
                f"{database_type} schema is missing tables: "
                + ", ".join(sorted(missing))
            )
        for table in sorted(expected_tables):
            if _table_signature(actual, table) != _table_signature(
                expected, table
            ):
                raise ValueError(
                    f"{database_type} table structure differs: {table}"
                )
            if database_type == "paper_state":
                actual_sql = _canonical_schema_sql(
                    actual.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()[0]
                )
                expected_sql = _canonical_schema_sql(
                    expected.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()[0]
                )
                if actual_sql != expected_sql:
                    raise ValueError(
                        f"{database_type} table constraints differ: {table}"
                    )
    finally:
        expected.close()


def _table_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    columns = tuple(
        tuple(row[1:])
        for row in connection.execute(
            f"PRAGMA table_xinfo({_quote_identifier(table)})"
        )
    )
    foreign_keys = tuple(sorted(
        tuple(row[2:])
        for row in connection.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        )
    ))
    indexes = []
    for row in connection.execute(
        f"PRAGMA index_list({_quote_identifier(table)})"
    ):
        columns_in_index = tuple(
            item[2]
            for item in connection.execute(
                f"PRAGMA index_info({_quote_identifier(row[1])})"
            )
        )
        indexes.append((row[2], row[3], row[4], columns_in_index))
    return columns, foreign_keys, tuple(sorted(indexes))


def _canonical_schema_sql(value: str) -> str:
    value = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value.strip()).lower()
    return re.sub(r"\s*([(),])\s*", r"\1", value)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _verify_paper_versions(connection: sqlite3.Connection) -> None:
    for table in ("paper_sessions", "paper_portfolio_sessions"):
        invalid = connection.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} "
            "WHERE schema_version != ? OR serialization_version != ? "
            "LIMIT 1",
            (
                PAPER_STATE_SCHEMA_VERSION,
                PAPER_STATE_SERIALIZATION_VERSION,
            ),
        ).fetchone()
        if invalid is not None:
            raise ValueError(
                f"{table} contains an incompatible persisted version"
            )


def paper_health(
    database_path: str | Path,
    *,
    session_id: str,
    mode: str,
    interval: str = "1h",
    maximum_progress_lag_seconds: int | None = None,
    now_ms: int | None = None,
) -> PaperHealthReport:
    """Check structural integrity and recent durable candle progress."""
    if mode not in {"single", "multi"}:
        raise ValueError("mode must be single or multi")
    interval_ms = get_interval_milliseconds(interval)
    if maximum_progress_lag_seconds is not None and (
        type(maximum_progress_lag_seconds) is not int
        or maximum_progress_lag_seconds < 0
    ):
        raise ValueError(
            "maximum_progress_lag_seconds must be non-negative"
        )
    checked_at = int(time.time() * 1000) if now_ms is None else now_ms
    if type(checked_at) is not int or checked_at < 0:
        raise ValueError("now_ms must be a non-negative integer")
    verification = verify_sqlite_database(database_path)
    if verification.database_type != "paper_state":
        raise ValueError("Health check requires a paper-state database")
    with _read_only_connection(Path(database_path)) as connection:
        if mode == "single":
            row = connection.execute(
                """
                SELECT checkpoint_sequence, candle_open_time
                FROM paper_checkpoints WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            reconciled = row is not None
        else:
            row = connection.execute(
                """
                SELECT candle_sequence, last_open_time, reconciled
                FROM paper_portfolio_checkpoints WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            reconciled = row is not None and row[2] == 1
    if row is None:
        return PaperHealthReport(
            session_id=session_id,
            mode=mode,
            healthy=False,
            schema_version=verification.schema_version,
            checkpoint_sequence=0,
            last_candle_open_time=None,
            reconciled=False,
            progress_current=False,
            progress_lag_seconds=None,
            message="paper session checkpoint is missing",
        )
    last_open_time = row[1]
    expected_last_open = checked_at - checked_at % interval_ms - interval_ms
    progress_lag_ms = (
        None
        if last_open_time is None
        else expected_last_open - int(last_open_time)
    )
    progress_current = (
        progress_lag_ms is not None
        and maximum_progress_lag_seconds is not None
        and 0 <= progress_lag_ms <= maximum_progress_lag_seconds * 1000
    )
    progress_required = maximum_progress_lag_seconds is not None
    healthy = reconciled and (progress_current or not progress_required)
    return PaperHealthReport(
        session_id=session_id,
        mode=mode,
        healthy=healthy,
        schema_version=verification.schema_version,
        checkpoint_sequence=int(row[0]),
        last_candle_open_time=last_open_time,
        reconciled=reconciled,
        progress_current=progress_current,
        progress_lag_seconds=(
            None if progress_lag_ms is None else progress_lag_ms // 1000
        ),
        message=(
            (
                "paper checkpoint is structurally healthy; progress was not "
                "evaluated"
            )
            if healthy and not progress_required
            else "paper checkpoint is structurally healthy and progress is current"
            if healthy
            else (
                "paper portfolio is marked unreconciled"
                if not reconciled
                else "paper checkpoint progress is missing, stale, or ahead"
            )
        ),
    )


def backup_sqlite(
    source_path: str | Path,
    output_path: str | Path,
) -> DatabaseVerification:
    """Create a consistent online SQLite backup without copying live files."""
    source = Path(source_path)
    output = Path(output_path)
    verify_sqlite_database(source)
    if output.exists():
        raise ValueError(f"Backup destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(source) as source_connection:
            with sqlite3.connect(temporary) as destination_connection:
                source_connection.backup(destination_connection)
        verify_sqlite_database(temporary)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return verify_sqlite_database(output)


def restore_sqlite(
    backup_path: str | Path,
    output_path: str | Path,
) -> DatabaseVerification:
    """Restore a verified backup to a new path, never over an active DB."""
    backup = Path(backup_path)
    output = Path(output_path)
    verify_sqlite_database(backup)
    if output.exists():
        raise ValueError(f"Restore destination already exists: {output}")
    return backup_sqlite(backup, output)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    absolute = path.resolve()
    return sqlite3.connect(
        f"file:{quote(str(absolute))}?mode=ro",
        uri=True,
    )
