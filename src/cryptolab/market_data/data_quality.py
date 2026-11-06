"""Completeness checks for stored historical candles."""

from dataclasses import dataclass

from cryptolab.storage.sqlite_repository import SQLiteRepository


@dataclass(frozen=True)
class DataQualityReport:
    """Result of checking candle completeness for a time range.

    ``missing_open_times`` contains at most the first 1000 missing timestamps.
    ``missing_candles`` always contains the exact total number of missing
    candles, even when the timestamp tuple is truncated.
    """

    symbol: str
    interval: str
    start_time: int
    end_time: int
    expected_candles: int
    actual_candles: int
    missing_candles: int
    missing_open_times: tuple[int, ...]


class DataQualityChecker:
    """Check whether all expected candles exist in SQLite."""

    MAX_MISSING_OPEN_TIMES = 1000

    INTERVAL_MILLISECONDS = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
        "1d": 24 * 60 * 60_000,
    }

    def __init__(self, repository: SQLiteRepository) -> None:
        self._repository = repository

    def check(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> DataQualityReport:
        """Check candle completeness in the range [start_time, end_time)."""
        try:
            interval_milliseconds = self.INTERVAL_MILLISECONDS[interval]
        except KeyError as error:
            raise ValueError(f"Unsupported interval: {interval}") from error

        if end_time <= start_time:
            raise ValueError("end_time must be greater than start_time")

        if start_time % interval_milliseconds != 0:
            raise ValueError("start_time must be aligned to the interval")

        if end_time % interval_milliseconds != 0:
            raise ValueError("end_time must be aligned to the interval")

        candles = self._repository.get_candles_by_range(
            symbol,
            interval,
            start_time,
            end_time,
        )
        actual_open_times = {candle.open_time for candle in candles}
        expected_open_times = range(
            start_time,
            end_time,
            interval_milliseconds,
        )
        missing_candles = 0
        missing_open_times: list[int] = []
        for open_time in expected_open_times:
            if open_time in actual_open_times:
                continue

            missing_candles += 1
            if len(missing_open_times) < self.MAX_MISSING_OPEN_TIMES:
                missing_open_times.append(open_time)

        expected_candles = (end_time - start_time) // interval_milliseconds

        return DataQualityReport(
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            expected_candles=expected_candles,
            actual_candles=len(candles),
            missing_candles=missing_candles,
            missing_open_times=tuple(missing_open_times),
        )

