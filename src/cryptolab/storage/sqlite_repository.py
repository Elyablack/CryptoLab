"""SQLite storage for historical market candles."""

import sqlite3
from decimal import Decimal
from pathlib import Path

from cryptolab.market_data.models import Candle


class SQLiteRepository:
    """Store and retrieve candles in a local SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = database_path

    def init_schema(self) -> None:
        """Create the candles table if it does not exist."""
        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
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
                    number_of_trades INTEGER NOT NULL,
                    taker_buy_base_volume TEXT NOT NULL,
                    taker_buy_quote_volume TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time)
                )
                """
            )

    def save_candles(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
    ) -> None:
        """Atomically save candles without overwriting existing rows."""
        rows = [
            (
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
                str(candle.taker_buy_base_volume),
                str(candle.taker_buy_quote_volume),
            )
            for candle in candles
        ]

        with sqlite3.connect(self._database_path) as connection:
            connection.executemany(
                """
                INSERT INTO candles (
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, interval, open_time) DO NOTHING
                """,
                rows,
            )

    def get_candles(self, symbol: str, interval: str) -> list[Candle]:
        """Return candles ordered by their opening time."""
        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                """
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
                WHERE symbol = ? AND interval = ?
                ORDER BY open_time ASC
                """,
                (symbol, interval),
            ).fetchall()

        return [
            Candle(
                open_time=row[0],
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
                close_time=row[6],
                quote_volume=Decimal(row[7]),
                number_of_trades=row[8],
                taker_buy_base_volume=Decimal(row[9]),
                taker_buy_quote_volume=Decimal(row[10]),
            )
            for row in rows
        ]

    def get_latest_open_time(self, symbol: str, interval: str) -> int | None:
        """Return the latest stored opening time, if any."""
        with sqlite3.connect(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT MAX(open_time)
                FROM candles
                WHERE symbol = ? AND interval = ?
                """,
                (symbol, interval),
            ).fetchone()

        return row[0] if row is not None else None

