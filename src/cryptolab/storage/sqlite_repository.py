"""SQLite storage for historical market candles."""

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.market_data.models import Candle


CREATE_CANDLES_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS candles (
        exchange TEXT NOT NULL,
        market_type TEXT NOT NULL,
        symbol TEXT NOT NULL,
        interval TEXT NOT NULL,
        open_time INTEGER NOT NULL,
        open TEXT NOT NULL,
        high TEXT NOT NULL,
        low TEXT NOT NULL,
        close TEXT NOT NULL,
        volume TEXT NOT NULL,
        close_time INTEGER NOT NULL,
        quote_volume TEXT NOT NULL,
        number_of_trades INTEGER,
        taker_buy_base_volume TEXT,
        taker_buy_quote_volume TEXT,
        PRIMARY KEY (
            exchange,
            market_type,
            symbol,
            interval,
            open_time
        )
    )
"""

SCHEMA_VERSION = 2
LEGACY_BACKUP_TABLE = "candles_legacy_v1_backup"
REQUIRED_CANDLE_COLUMNS = {
    "exchange",
    "market_type",
    "symbol",
    "interval",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "number_of_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
}


@dataclass(frozen=True)
class CandleConflict:
    """An incoming historical candle differs from the retained DB row."""

    open_time: int
    existing: Candle
    incoming: Candle


@dataclass(frozen=True)
class SaveCandlesResult:
    """Outcome of one atomic save operation."""

    inserted: int
    existing: int
    conflicting: int
    conflicts: tuple[CandleConflict, ...]


class SQLiteRepository:
    """Store and retrieve exchange-isolated candles in local SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = database_path

    def init_schema(self) -> None:
        """Create, adopt, or safely migrate to the current schema version."""
        with sqlite3.connect(self._database_path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than "
                    f"supported version {SCHEMA_VERSION}"
                )
            table_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'candles'
                """
            ).fetchone()
            if table_exists is None:
                connection.execute(CREATE_CANDLES_TABLE_SQL)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return

            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(candles)"
                ).fetchall()
            }
            identity_columns = {"exchange", "market_type"}
            if identity_columns <= columns:
                missing_columns = REQUIRED_CANDLE_COLUMNS - columns
                if missing_columns:
                    raise RuntimeError(
                        "Unsupported candles schema; missing columns: "
                        + ", ".join(sorted(missing_columns))
                    )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return
            if identity_columns & columns:
                raise RuntimeError(
                    "Unsupported partial candles schema: exchange and "
                    "market_type must be added together"
                )

            self._migrate_legacy_binance_spot_schema(connection)

    @staticmethod
    def _migrate_legacy_binance_spot_schema(
        connection: sqlite3.Connection,
    ) -> None:
        """Copy legacy rows transactionally and retain a read-only backup."""
        backup_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (LEGACY_BACKUP_TABLE,),
        ).fetchone()
        if backup_exists is not None:
            raise RuntimeError(
                f"Cannot migrate while backup table {LEGACY_BACKUP_TABLE} exists"
            )

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"ALTER TABLE candles RENAME TO {LEGACY_BACKUP_TABLE}"
        )
        connection.execute(CREATE_CANDLES_TABLE_SQL)
        connection.execute(
            f"""
            INSERT INTO candles (
                exchange,
                market_type,
                symbol,
                interval,
                open_time,
                open,
                high,
                low,
                close,
                volume,
                close_time,
                quote_volume,
                number_of_trades,
                taker_buy_base_volume,
                taker_buy_quote_volume
            )
            SELECT
                'binance',
                'spot',
                symbol,
                interval,
                open_time,
                open,
                high,
                low,
                close,
                volume,
                close_time,
                quote_volume,
                number_of_trades,
                taker_buy_base_volume,
                taker_buy_quote_volume
            FROM {LEGACY_BACKUP_TABLE}
            """
        )
        legacy_count = connection.execute(
            f"SELECT COUNT(*) FROM {LEGACY_BACKUP_TABLE}"
        ).fetchone()[0]
        migrated_count = connection.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]
        if legacy_count != migrated_count:
            raise RuntimeError(
                "Legacy migration row count mismatch: "
                f"source={legacy_count}, migrated={migrated_count}"
            )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def save_candles(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> SaveCandlesResult:
        """Atomically insert new candles and retain conflicting history.

        An unchanged key/value pair is counted as ``existing``. If the same
        market key already contains different candle values, the existing row
        is retained and a ``CandleConflict`` is returned. Historical data is
        never overwritten implicitly.
        """
        validate_market_identity(exchange, market_type)
        inserted = 0
        existing = 0
        conflicts: list[CandleConflict] = []
        with sqlite3.connect(self._database_path) as connection:
            for candle in candles:
                row = connection.execute(
                    f"""
                    {self._candle_select_sql()}
                    WHERE exchange = ?
                        AND market_type = ?
                        AND symbol = ?
                        AND interval = ?
                        AND open_time = ?
                    """,
                    (
                        exchange,
                        market_type,
                        symbol,
                        interval,
                        candle.open_time,
                    ),
                ).fetchone()
                if row is not None:
                    retained = _row_to_candle(row)
                    if retained == candle:
                        existing += 1
                    else:
                        conflicts.append(CandleConflict(
                            open_time=candle.open_time,
                            existing=retained,
                            incoming=candle,
                        ))
                    continue

                connection.execute(
                    """
                    INSERT INTO candles (
                        exchange, market_type, symbol, interval, open_time,
                        open, high, low, close, volume, close_time,
                        quote_volume, number_of_trades,
                        taker_buy_base_volume, taker_buy_quote_volume
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _candle_to_row(
                        candle,
                        exchange=exchange,
                        market_type=market_type,
                        symbol=symbol,
                        interval=interval,
                    ),
                )
                inserted += 1

        return SaveCandlesResult(
            inserted=inserted,
            existing=existing,
            conflicting=len(conflicts),
            conflicts=tuple(conflicts),
        )

    def get_schema_version(self) -> int:
        """Return the SQLite user-version used for deterministic migrations."""
        with sqlite3.connect(self._database_path) as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def get_candles(
        self,
        symbol: str,
        interval: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> list[Candle]:
        """Return candles ordered by opening time for one market source."""
        validate_market_identity(exchange, market_type)
        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                f"""
                {self._candle_select_sql()}
                WHERE exchange = ?
                    AND market_type = ?
                    AND symbol = ?
                    AND interval = ?
                ORDER BY open_time ASC
                """,
                (exchange, market_type, symbol, interval),
            ).fetchall()

        return [_row_to_candle(row) for row in rows]

    def get_candles_by_range(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> list[Candle]:
        """Return source-specific candles in [start_time, end_time)."""
        validate_market_identity(exchange, market_type)
        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                f"""
                {self._candle_select_sql()}
                WHERE exchange = ?
                    AND market_type = ?
                    AND symbol = ?
                    AND interval = ?
                    AND open_time >= ?
                    AND open_time < ?
                ORDER BY open_time ASC
                """,
                (
                    exchange,
                    market_type,
                    symbol,
                    interval,
                    start_time,
                    end_time,
                ),
            ).fetchall()

        return [_row_to_candle(row) for row in rows]

    def get_latest_open_time(
        self,
        symbol: str,
        interval: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> int | None:
        """Return the latest source-specific opening time, if any."""
        validate_market_identity(exchange, market_type)
        with sqlite3.connect(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT MAX(open_time)
                FROM candles
                WHERE exchange = ?
                    AND market_type = ?
                    AND symbol = ?
                    AND interval = ?
                """,
                (exchange, market_type, symbol, interval),
            ).fetchone()

        return row[0] if row is not None else None

    @staticmethod
    def _candle_select_sql() -> str:
        return """
            SELECT
                open_time,
                open,
                high,
                low,
                close,
                volume,
                close_time,
                quote_volume,
                number_of_trades,
                taker_buy_base_volume,
                taker_buy_quote_volume
            FROM candles
        """


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _candle_to_row(
    candle: Candle,
    *,
    exchange: str,
    market_type: str,
    symbol: str,
    interval: str,
) -> tuple[object, ...]:
    return (
        exchange,
        market_type,
        symbol,
        interval,
        candle.open_time,
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.volume),
        candle.close_time,
        str(candle.quote_volume),
        candle.number_of_trades,
        _optional_decimal_text(candle.taker_buy_base_volume),
        _optional_decimal_text(candle.taker_buy_quote_volume),
    )


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _row_to_candle(row: tuple[object, ...]) -> Candle:
    return Candle(
        open_time=int(row[0]),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
        close_time=int(row[6]),
        quote_volume=Decimal(str(row[7])),
        number_of_trades=(None if row[8] is None else int(row[8])),
        taker_buy_base_volume=_optional_decimal(row[9]),
        taker_buy_quote_volume=_optional_decimal(row[10]),
    )
