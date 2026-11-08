"""Completeness checks for stored historical candles."""

import time
from collections import Counter
from dataclasses import dataclass

from cryptolab.market_data.intervals import (
    INTERVAL_MILLISECONDS as SUPPORTED_INTERVAL_MILLISECONDS,
    get_interval_milliseconds,
)
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.market_data.validator import (
    validate_candle,
    validate_candle_timing,
)


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
    exchange: str = DEFAULT_EXCHANGE
    market_type: str = SPOT_MARKET_TYPE
    unexpected_candles: int = 0
    unexpected_open_times: tuple[int, ...] = ()
    duplicate_candles: int = 0
    duplicate_open_times: tuple[int, ...] = ()
    invalid_candles: int = 0
    invalid_reasons: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Whether the stored range exactly matches a valid candle grid."""
        return (
            self.missing_candles == 0
            and self.unexpected_candles == 0
            and self.duplicate_candles == 0
            and self.invalid_candles == 0
            and self.actual_candles == self.expected_candles
        )


class DataQualityChecker:
    """Check whether all expected candles exist in SQLite."""

    MAX_MISSING_OPEN_TIMES = 1000
    MAX_ISSUE_DETAILS = 1000

    INTERVAL_MILLISECONDS = SUPPORTED_INTERVAL_MILLISECONDS

    def __init__(self, repository: SQLiteRepository) -> None:
        self._repository = repository

    def check(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
        as_of_time: int | None = None,
    ) -> DataQualityReport:
        """Validate exact candle coverage in ``[start_time, end_time)``."""
        validate_market_identity(exchange, market_type)
        interval_milliseconds = get_interval_milliseconds(interval)

        if end_time <= start_time:
            raise ValueError("end_time must be greater than start_time")

        if start_time % interval_milliseconds != 0:
            raise ValueError("start_time must be aligned to the interval")

        if end_time % interval_milliseconds != 0:
            raise ValueError("end_time must be aligned to the interval")

        if exchange == DEFAULT_EXCHANGE and market_type == SPOT_MARKET_TYPE:
            candles = self._repository.get_candles_by_range(
                symbol,
                interval,
                start_time,
                end_time,
            )
        else:
            candles = self._repository.get_candles_by_range(
                symbol,
                interval,
                start_time,
                end_time,
                exchange=exchange,
                market_type=market_type,
            )
        evaluation_time = (
            int(time.time() * 1000)
            if as_of_time is None
            else as_of_time
        )
        counts = Counter(candle.open_time for candle in candles)
        actual_open_times = set(counts)
        expected_range = range(
            start_time,
            end_time,
            interval_milliseconds,
        )
        expected_open_times = set(expected_range)
        missing_candles = 0
        missing_open_times: list[int] = []
        for open_time in expected_range:
            if open_time in actual_open_times:
                continue

            missing_candles += 1
            if len(missing_open_times) < self.MAX_MISSING_OPEN_TIMES:
                missing_open_times.append(open_time)

        unexpected_open_times = sorted(
            actual_open_times - expected_open_times
        )
        duplicate_open_times = sorted(
            open_time for open_time, count in counts.items() if count > 1
        )
        duplicate_candles = sum(
            count - 1 for count in counts.values() if count > 1
        )

        invalid_reasons: list[str] = []
        invalid_candles = 0
        for index, candle in enumerate(candles):
            try:
                validate_candle(candle)
                validate_candle_timing(
                    candle,
                    interval,
                    as_of_time=evaluation_time,
                )
            except ValueError as error:
                invalid_candles += 1
                if len(invalid_reasons) < self.MAX_ISSUE_DETAILS:
                    invalid_reasons.append(
                        f"index {index}, open_time={candle.open_time}: {error}"
                    )

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
            exchange=exchange,
            market_type=market_type,
            unexpected_candles=len(unexpected_open_times),
            unexpected_open_times=tuple(
                unexpected_open_times[:self.MAX_ISSUE_DETAILS]
            ),
            duplicate_candles=duplicate_candles,
            duplicate_open_times=tuple(
                duplicate_open_times[:self.MAX_ISSUE_DETAILS]
            ),
            invalid_candles=invalid_candles,
            invalid_reasons=tuple(invalid_reasons),
        )

