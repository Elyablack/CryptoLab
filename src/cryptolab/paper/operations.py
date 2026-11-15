"""Health, SQLite backup, verification, and non-destructive restore."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from cryptolab.storage.paper_state_repository import (
    PAPER_STATE_SCHEMA_VERSION,
)
from cryptolab.storage.sqlite_repository import SCHEMA_VERSION


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
    message: str


def verify_sqlite_database(path: str | Path) -> DatabaseVerification:
    """Open a database read-only and validate integrity and schema identity."""
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
    if "paper_sessions" in tables and "paper_checkpoints" in tables:
        database_type = "paper_state"
        expected = PAPER_STATE_SCHEMA_VERSION
    elif "candles" in tables:
        database_type = "market_data"
        expected = SCHEMA_VERSION
    else:
        raise ValueError("Unrecognized CryptoLab SQLite schema")
    if version != expected:
        raise ValueError(
            f"Unsupported {database_type} schema version {version}; "
            f"expected {expected}"
        )
    return DatabaseVerification(
        path=database,
        database_type=database_type,
        schema_version=version,
        integrity_ok=True,
    )


def paper_health(
    database_path: str | Path,
    *,
    session_id: str,
    mode: str,
) -> PaperHealthReport:
    """Perform a network-free health check against one durable checkpoint."""
    if mode not in {"single", "multi"}:
        raise ValueError("mode must be single or multi")
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
            message="paper session checkpoint is missing",
        )
    return PaperHealthReport(
        session_id=session_id,
        mode=mode,
        healthy=reconciled,
        schema_version=verification.schema_version,
        checkpoint_sequence=int(row[0]),
        last_candle_open_time=row[1],
        reconciled=reconciled,
        message=(
            "paper checkpoint is structurally healthy"
            if reconciled
            else "paper portfolio is marked unreconciled"
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
