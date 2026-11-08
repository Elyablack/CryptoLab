import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


FIRST_CANDLE = Candle(
    open_time=1_000,
    open=Decimal("100.1000"),
    high=Decimal("120.2000"),
    low=Decimal("90.3000"),
    close=Decimal("110.4000"),
    volume=Decimal("10.5000"),
    close_time=1_999,
    quote_volume=Decimal("1000.6000"),
    number_of_trades=25,
    taker_buy_base_volume=Decimal("6.7000"),
    taker_buy_quote_volume=Decimal("600.8000"),
)

SECOND_CANDLE = replace(
    FIRST_CANDLE,
    open_time=2_000,
    close_time=2_999,
    close=Decimal("115.9000"),
)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "candles.sqlite3"


@pytest.fixture
def repository(database_path: Path) -> SQLiteRepository:
    result = SQLiteRepository(database_path)
    result.init_schema()
    return result


def test_init_schema_creates_candles_table(database_path: Path) -> None:
    repository = SQLiteRepository(database_path)

    repository.init_schema()

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "candles"),
        ).fetchone()

    assert row == ("candles",)


def test_save_and_get_candles_returns_them_ordered(
    repository: SQLiteRepository,
) -> None:
    repository.save_candles("BTCUSDT", "1h", [SECOND_CANDLE, FIRST_CANDLE])

    assert repository.get_candles("BTCUSDT", "1h") == [
        FIRST_CANDLE,
        SECOND_CANDLE,
    ]


def test_get_candles_filters_by_symbol_and_interval(
    repository: SQLiteRepository,
) -> None:
    repository.save_candles("BTCUSDT", "1h", [FIRST_CANDLE])
    repository.save_candles("ETHUSDT", "1h", [SECOND_CANDLE])
    repository.save_candles("BTCUSDT", "4h", [SECOND_CANDLE])

    assert repository.get_candles("BTCUSDT", "1h") == [FIRST_CANDLE]


def test_get_candles_by_range_uses_half_open_bounds(
    repository: SQLiteRepository,
) -> None:
    repository.save_candles("BTCUSDT", "1h", [SECOND_CANDLE, FIRST_CANDLE])

    assert repository.get_candles_by_range(
        "BTCUSDT",
        "1h",
        start_time=1_000,
        end_time=2_000,
    ) == [FIRST_CANDLE]


def test_decimal_values_are_stored_as_text_without_losing_precision(
    repository: SQLiteRepository,
    database_path: Path,
) -> None:
    repository.save_candles("BTCUSDT", "1h", [FIRST_CANDLE])

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT open, typeof(open), volume, typeof(volume) FROM candles"
        ).fetchone()

    assert row == ("100.1000", "text", "10.5000", "text")
    assert repository.get_candles("BTCUSDT", "1h") == [FIRST_CANDLE]


def test_get_latest_open_time_returns_none_for_empty_result(
    repository: SQLiteRepository,
) -> None:
    assert repository.get_latest_open_time("BTCUSDT", "1h") is None


def test_get_latest_open_time_returns_maximum_value(
    repository: SQLiteRepository,
) -> None:
    repository.save_candles("BTCUSDT", "1h", [SECOND_CANDLE, FIRST_CANDLE])

    assert repository.get_latest_open_time("BTCUSDT", "1h") == 2_000


def test_saving_same_candle_twice_does_not_create_duplicate(
    repository: SQLiteRepository,
) -> None:
    repository.save_candles("BTCUSDT", "1h", [FIRST_CANDLE])
    repository.save_candles("BTCUSDT", "1h", [FIRST_CANDLE])

    assert repository.get_candles("BTCUSDT", "1h") == [FIRST_CANDLE]


def test_conflict_does_not_overwrite_existing_candle(
    repository: SQLiteRepository,
) -> None:
    changed_candle = replace(FIRST_CANDLE, close=Decimal("119.0000"))
    repository.save_candles("BTCUSDT", "1h", [FIRST_CANDLE])

    repository.save_candles("BTCUSDT", "1h", [changed_candle])

    assert repository.get_candles("BTCUSDT", "1h") == [FIRST_CANDLE]


def test_save_candles_rolls_back_entire_batch_on_error(
    repository: SQLiteRepository,
) -> None:
    invalid_candle = replace(
        SECOND_CANDLE,
        close_time=None,  # type: ignore[arg-type]
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.save_candles(
            "BTCUSDT",
            "1h",
            [FIRST_CANDLE, invalid_candle],
        )

    assert repository.get_candles("BTCUSDT", "1h") == []


def test_init_schema_migrates_legacy_rows_to_binance_spot(
    database_path: Path,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE candles (
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
        connection.execute(
            """
            INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "BTCUSDT",
                "1h",
                FIRST_CANDLE.open_time,
                str(FIRST_CANDLE.open),
                str(FIRST_CANDLE.high),
                str(FIRST_CANDLE.low),
                str(FIRST_CANDLE.close),
                str(FIRST_CANDLE.volume),
                FIRST_CANDLE.close_time,
                str(FIRST_CANDLE.quote_volume),
                FIRST_CANDLE.number_of_trades,
                str(FIRST_CANDLE.taker_buy_base_volume),
                str(FIRST_CANDLE.taker_buy_quote_volume),
            ),
        )

    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.init_schema()

    assert repository.get_candles("BTCUSDT", "1h") == [FIRST_CANDLE]
    with sqlite3.connect(database_path) as connection:
        identity = connection.execute(
            "SELECT exchange, market_type FROM candles"
        ).fetchone()
        primary_key = [
            row[1]
            for row in connection.execute("PRAGMA table_info(candles)")
            if row[5]
        ]
        legacy_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'candles_legacy_binance_spot'
            """
        ).fetchone()

    assert identity == ("binance", "spot")
    assert primary_key == [
        "exchange",
        "market_type",
        "symbol",
        "interval",
        "open_time",
    ]
    assert legacy_table is None


def test_same_candle_key_is_isolated_by_exchange(
    repository: SQLiteRepository,
    database_path: Path,
) -> None:
    bybit_candle = replace(
        FIRST_CANDLE,
        close=Decimal("111.5000"),
        number_of_trades=None,
        taker_buy_base_volume=None,
        taker_buy_quote_volume=None,
    )
    repository.save_candles("BTCUSDT", "1h", [FIRST_CANDLE])
    repository.save_candles(
        "BTCUSDT",
        "1h",
        [bybit_candle],
        exchange="bybit",
        market_type="spot",
    )

    assert repository.get_candles("BTCUSDT", "1h") == [FIRST_CANDLE]
    assert repository.get_candles(
        "BTCUSDT",
        "1h",
        exchange="bybit",
        market_type="spot",
    ) == [bybit_candle]
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT exchange, number_of_trades, taker_buy_base_volume
            FROM candles ORDER BY exchange
            """
        ).fetchall()

    assert rows == [
        ("binance", 25, "6.7000"),
        ("bybit", None, None),
    ]
