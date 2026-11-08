"""SQLite storage for historical market candles."""

import sqlite3
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


class SQLiteRepository:
    """Store and retrieve exchange-isolated candles in local SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = database_path

    def init_schema(self) -> None:
        """Create the current schema or migrate legacy Binance Spot rows."""
        with sqlite3.connect(self._database_path) as connection:
            table_exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'candles'
                """
            ).fetchone()
            if table_exists is None:
                connection.execute(CREATE_CANDLES_TABLE_SQL)
                return

            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(candles)"
                ).fetchall()
            }
            identity_columns = {"exchange", "market_type"}
            if identity_columns <= columns:
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
        """Rebuild the legacy table inside one SQLite transaction."""
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE candles RENAME TO candles_legacy_binance_spot"
        )
        connection.execute(CREATE_CANDLES_TABLE_SQL)
        connection.execute(
            """
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
            FROM candles_legacy_binance_spot
            """
        )
        connection.execute("DROP TABLE candles_legacy_binance_spot")

    def save_candles(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> None:
        """Atomically save candles without overwriting existing rows."""
        validate_market_identity(exchange, market_type)
        rows = [
            (
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
            for candle in candles
        ]

        with sqlite3.connect(self._database_path) as connection:
            connection.executemany(
                """
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    exchange,
                    market_type,
                    symbol,
                    interval,
                    open_time
                ) DO NOTHING
                """,
                rows,
            )

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
