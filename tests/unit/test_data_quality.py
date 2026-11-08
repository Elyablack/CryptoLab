from decimal import Decimal
from pathlib import Path

import pytest

from cryptolab.market_data.data_quality import (
    DataQualityChecker,
    DataQualityReport,
)
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


def make_candle(open_time: int, interval_milliseconds: int) -> Candle:
    return Candle(
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("90"),
        close=Decimal("110"),
        volume=Decimal("10"),
        close_time=open_time + interval_milliseconds - 1,
        quote_volume=Decimal("1000"),
        number_of_trades=25,
        taker_buy_base_volume=Decimal("6"),
        taker_buy_quote_volume=Decimal("600"),
    )


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    result = SQLiteRepository(tmp_path / "candles.sqlite3")
    result.init_schema()
    return result


@pytest.mark.parametrize(
    ("interval", "interval_milliseconds"),
    [
        ("1m", 60_000),
        ("5m", 300_000),
        ("15m", 900_000),
        ("1h", 3_600_000),
        ("4h", 14_400_000),
        ("1d", 86_400_000),
    ],
)
def test_check_supports_expected_intervals(
    repository: SQLiteRepository,
    interval: str,
    interval_milliseconds: int,
) -> None:
    report = DataQualityChecker(repository).check(
        "BTCUSDT",
        interval,
        start_time=0,
        end_time=2 * interval_milliseconds,
    )

    assert report.expected_candles == 2
    assert report.missing_open_times == (0, interval_milliseconds)


def test_check_returns_complete_report(repository: SQLiteRepository) -> None:
    interval_milliseconds = 60_000
    candles = [
        make_candle(0, interval_milliseconds),
        make_candle(60_000, interval_milliseconds),
        make_candle(120_000, interval_milliseconds),
    ]
    repository.save_candles("BTCUSDT", "1m", candles)

    report = DataQualityChecker(repository).check(
        "BTCUSDT",
        "1m",
        start_time=0,
        end_time=180_000,
    )

    assert report == DataQualityReport(
        symbol="BTCUSDT",
        interval="1m",
        start_time=0,
        end_time=180_000,
        expected_candles=3,
        actual_candles=3,
        missing_candles=0,
        missing_open_times=(),
    )


def test_check_detects_missing_start_middle_and_end(
    repository: SQLiteRepository,
) -> None:
    interval_milliseconds = 60_000
    repository.save_candles(
        "BTCUSDT",
        "1m",
        [
            make_candle(60_000, interval_milliseconds),
            make_candle(180_000, interval_milliseconds),
        ],
    )

    report = DataQualityChecker(repository).check(
        "BTCUSDT",
        "1m",
        start_time=0,
        end_time=300_000,
    )

    assert report.expected_candles == 5
    assert report.actual_candles == 2
    assert report.missing_candles == 3
    assert report.missing_open_times == (0, 120_000, 240_000)


def test_check_handles_completely_empty_range(
    repository: SQLiteRepository,
) -> None:
    report = DataQualityChecker(repository).check(
        "ETHUSDT",
        "5m",
        start_time=0,
        end_time=900_000,
    )

    assert report.expected_candles == 3
    assert report.actual_candles == 0
    assert report.missing_candles == 3
    assert report.missing_open_times == (0, 300_000, 600_000)


def test_check_limits_missing_timestamp_preview_but_keeps_exact_count(
    repository: SQLiteRepository,
) -> None:
    interval_milliseconds = 60_000
    total_missing = 1_005

    report = DataQualityChecker(repository).check(
        "BTCUSDT",
        "1m",
        start_time=0,
        end_time=total_missing * interval_milliseconds,
    )

    assert report.missing_candles == total_missing
    assert isinstance(report.missing_open_times, tuple)
    assert len(report.missing_open_times) == 1_000
    assert report.missing_open_times[0] == 0
    assert report.missing_open_times[-1] == 999 * interval_milliseconds


def test_check_rejects_unsupported_interval(
    repository: SQLiteRepository,
) -> None:
    with pytest.raises(ValueError, match="Unsupported interval: 2h"):
        DataQualityChecker(repository).check("BTCUSDT", "2h", 0, 7_200_000)


@pytest.mark.parametrize("end_time", [0, -60_000])
def test_check_rejects_non_increasing_range(
    repository: SQLiteRepository,
    end_time: int,
) -> None:
    with pytest.raises(ValueError, match="end_time must be greater than start_time"):
        DataQualityChecker(repository).check("BTCUSDT", "1m", 0, end_time)


def test_check_rejects_unaligned_start_time(
    repository: SQLiteRepository,
) -> None:
    with pytest.raises(ValueError, match="start_time must be aligned"):
        DataQualityChecker(repository).check("BTCUSDT", "1m", 1, 60_000)


def test_check_rejects_unaligned_end_time(
    repository: SQLiteRepository,
) -> None:
    with pytest.raises(ValueError, match="end_time must be aligned"):
        DataQualityChecker(repository).check("BTCUSDT", "1m", 0, 60_001)


def test_check_queries_only_requested_range() -> None:
    class RangeOnlyRepository:
        def __init__(self) -> None:
            self.call: tuple[str, str, int, int] | None = None

        def get_candles_by_range(
            self,
            symbol: str,
            interval: str,
            start_time: int,
            end_time: int,
        ) -> list[Candle]:
            self.call = (symbol, interval, start_time, end_time)
            return []

    repository = RangeOnlyRepository()
    checker = DataQualityChecker(repository)  # type: ignore[arg-type]

    checker.check("BTCUSDT", "1h", 3_600_000, 7_200_000)

    assert repository.call == ("BTCUSDT", "1h", 3_600_000, 7_200_000)


def test_check_isolates_identical_markets_by_exchange(
    repository: SQLiteRepository,
) -> None:
    candle = make_candle(0, 60_000)
    repository.save_candles("BTCUSDT", "1m", [candle])

    binance_report = DataQualityChecker(repository).check(
        "BTCUSDT",
        "1m",
        0,
        60_000,
    )
    bybit_report = DataQualityChecker(repository).check(
        "BTCUSDT",
        "1m",
        0,
        60_000,
        exchange="bybit",
        market_type="spot",
    )

    assert binance_report.actual_candles == 1
    assert binance_report.exchange == "binance"
    assert bybit_report.actual_candles == 0
    assert bybit_report.missing_candles == 1
    assert bybit_report.exchange == "bybit"
