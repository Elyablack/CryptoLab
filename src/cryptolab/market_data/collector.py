"""Historical market data collection."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.market_data.intervals import get_interval_milliseconds
from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.market_data.models import Candle
from cryptolab.market_data.parser import parse_klines
from cryptolab.market_data.validator import (
    validate_candle_timing,
    validate_candles,
)
from cryptolab.storage.sqlite_repository import (
    SQLiteRepository,
    SaveCandlesResult,
)


class KlineClient(Protocol):
    """Minimal public kline client used by the collector."""

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> object: ...


KlineParser = Callable[[object, str, str], list[Candle]]
PaginationDirection = Literal["forward", "backward"]


@dataclass(frozen=True)
class CollectionResult:
    """Auditable result of one requested collection range.

    ``received`` counts parsed candles returned by all HTTP pages, including
    unfinished candles later excluded. ``inserted`` counts new persisted rows;
    ``existing`` counts identical rows already present; ``conflicting`` counts
    keys whose stored and incoming values differ (stored values are retained).
    ``invalid`` counts rejected response pages. ``missing`` and ``unexpected``
    describe the final persisted interval grid. A result succeeds only when no
    failure, conflict, invalid page, missing candle, or unexpected candle
    remains.
    """

    exchange: str
    market_type: str
    symbol: str
    interval: str
    start_time: int
    end_time: int
    expected: int
    received: int
    inserted: int
    existing: int
    conflicting: int
    missing: int
    invalid: int
    unexpected: int
    excluded_unfinished: int
    empty_pages: int
    conflict_open_times: tuple[int, ...]
    missing_open_times: tuple[int, ...]
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.failure_reason is None
            and self.conflicting == 0
            and self.missing == 0
            and self.invalid == 0
            and self.unexpected == 0
        )


class CollectionFailure(ValueError):
    """Collection stopped after invalid exchange data or pagination."""

    def __init__(self, message: str, result: CollectionResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass
class _CollectionProgress:
    received: int = 0
    inserted: int = 0
    existing: int = 0
    conflicting: int = 0
    invalid: int = 0
    excluded_unfinished: int = 0
    empty_pages: int = 0
    conflict_open_times: list[int] = field(default_factory=list)
    failure_reason: str | None = None


class _InvalidPageError(ValueError):
    pass


def _parse_binance_response(
    raw: object,
    symbol: str,
    interval: str,
) -> list[Candle]:
    del symbol, interval
    return parse_klines(raw)


class HistoricalDataCollector:
    """Download, validate, and store public historical Spot candles."""

    PAGE_LIMIT = 1000

    def __init__(
        self,
        client: KlineClient,
        repository: SQLiteRepository,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
        parser: KlineParser | None = None,
        pagination_direction: PaginationDirection = "forward",
    ) -> None:
        validate_market_identity(exchange, market_type)
        if pagination_direction not in ("forward", "backward"):
            raise ValueError("Unsupported pagination direction")
        if parser is None and exchange != DEFAULT_EXCHANGE:
            raise ValueError("A response parser is required for this exchange")

        self._client = client
        self._repository = repository
        self._exchange = exchange
        self._market_type = market_type
        self._parser = parser or _parse_binance_response
        self._pagination_direction = pagination_direction

    def collect(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> CollectionResult:
        """Collect candles for the half-open interval [start_time, end_time)."""
        if end_time <= start_time:
            raise ValueError("end_time must be greater than start_time")
        interval_milliseconds = get_interval_milliseconds(interval)
        if start_time % interval_milliseconds != 0:
            raise ValueError("start_time must be aligned to the interval")
        if end_time % interval_milliseconds != 0:
            raise ValueError("end_time must be aligned to the interval")

        collection_time = int(time.time() * 1000)
        progress = _CollectionProgress()
        try:
            if self._pagination_direction == "forward":
                self._collect_forward(
                    symbol,
                    interval,
                    start_time,
                    end_time,
                    collection_time,
                    interval_milliseconds,
                    progress,
                )
            else:
                self._collect_backward(
                    symbol,
                    interval,
                    start_time,
                    end_time,
                    collection_time,
                    progress,
                )
        except _InvalidPageError as error:
            progress.invalid += 1
            progress.failure_reason = str(error)
            result = self._build_result(
                symbol,
                interval,
                start_time,
                end_time,
                collection_time,
                progress,
            )
            raise CollectionFailure(str(error), result) from error
        except ValueError as error:
            progress.failure_reason = str(error)
            result = self._build_result(
                symbol,
                interval,
                start_time,
                end_time,
                collection_time,
                progress,
            )
            raise CollectionFailure(str(error), result) from error

        result = self._build_result(
            symbol,
            interval,
            start_time,
            end_time,
            collection_time,
            progress,
        )
        return result

    def _collect_forward(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        collection_time: int,
        interval_milliseconds: int,
        progress: _CollectionProgress,
    ) -> None:
        current_start_time = start_time
        while current_start_time < end_time:
            candles = self._fetch_page(
                symbol,
                interval,
                start_time=current_start_time,
                end_time=end_time - 1,
            )
            if not candles:
                progress.empty_pages += 1
                progress.failure_reason = self._empty_page_reason(progress)
                break

            progress.received += len(candles)

            if candles[0].open_time < current_start_time:
                raise ValueError(
                    f"{self._exchange.title()} returned a candle before "
                    "the requested start_time"
                )
            if candles[-1].open_time >= end_time:
                raise ValueError(
                    f"{self._exchange.title()} returned a candle outside "
                    "the requested end_time"
                )

            next_start_time = candles[-1].open_time + 1
            if next_start_time <= current_start_time:
                raise ValueError(
                    "Kline pagination did not advance: "
                    f"requested start_time={current_start_time}, "
                    f"last open_time={candles[-1].open_time}"
                )

            self._save_closed(
                symbol,
                interval,
                candles,
                start_time,
                end_time,
                collection_time,
                progress,
            )
            if candles[-1].open_time >= end_time - interval_milliseconds:
                current_start_time = end_time
            else:
                current_start_time = (
                    candles[-1].open_time + interval_milliseconds
                )

    def _collect_backward(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        collection_time: int,
        progress: _CollectionProgress,
    ) -> None:
        current_end_time = end_time - 1
        while current_end_time >= start_time:
            candles = self._fetch_page(
                symbol,
                interval,
                start_time=start_time,
                end_time=current_end_time,
            )
            if not candles:
                progress.empty_pages += 1
                progress.failure_reason = self._empty_page_reason(progress)
                break

            progress.received += len(candles)

            if candles[0].open_time < start_time:
                raise ValueError(
                    f"{self._exchange.title()} returned a candle before "
                    "the requested start_time"
                )
            if candles[-1].open_time > current_end_time:
                raise ValueError(
                    f"{self._exchange.title()} returned a candle outside "
                    "the requested end_time"
                )

            next_end_time = candles[0].open_time - 1
            if next_end_time >= current_end_time:
                raise ValueError(
                    "Kline pagination did not advance: "
                    f"requested end_time={current_end_time}, "
                    f"first open_time={candles[0].open_time}"
                )

            self._save_closed(
                symbol,
                interval,
                candles,
                start_time,
                end_time,
                collection_time,
                progress,
            )
            if candles[0].open_time <= start_time:
                current_end_time = start_time - 1
            else:
                current_end_time = candles[0].open_time - 1

    def _fetch_page(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int,
        end_time: int,
    ) -> list[Candle]:
        raw_klines = self._client.get_klines(
            symbol=symbol,
            interval=interval,
            limit=self.PAGE_LIMIT,
            start_time=start_time,
            end_time=end_time,
        )
        try:
            candles = self._parser(raw_klines, symbol, interval)
            validate_candles(candles)
            for index, candle in enumerate(candles):
                try:
                    validate_candle_timing(
                        candle,
                        interval,
                        require_closed=False,
                    )
                except ValueError as error:
                    raise ValueError(
                        f"candle at index {index}: {error}"
                    ) from error
        except ValueError as error:
            raise _InvalidPageError(
                f"Invalid {self._exchange} kline page: {error}"
            ) from error
        return candles

    def _save_closed(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
        start_time: int,
        end_time: int,
        collection_time: int,
        progress: _CollectionProgress,
    ) -> None:
        closed_candles = [
            candle
            for candle in candles
            if start_time <= candle.open_time < end_time
            and candle.close_time < collection_time
        ]
        progress.excluded_unfinished += len(candles) - len(closed_candles)
        if closed_candles:
            if (
                self._exchange == DEFAULT_EXCHANGE
                and self._market_type == SPOT_MARKET_TYPE
            ):
                save_result = self._repository.save_candles(
                    symbol,
                    interval,
                    closed_candles,
                )
            else:
                save_result = self._repository.save_candles(
                    symbol,
                    interval,
                    closed_candles,
                    exchange=self._exchange,
                    market_type=self._market_type,
                )
            self._record_save_result(progress, save_result)

    @staticmethod
    def _record_save_result(
        progress: _CollectionProgress,
        result: SaveCandlesResult,
    ) -> None:
        progress.inserted += result.inserted
        progress.existing += result.existing
        progress.conflicting += result.conflicting
        remaining = 1000 - len(progress.conflict_open_times)
        if remaining > 0:
            progress.conflict_open_times.extend(
                conflict.open_time for conflict in result.conflicts[:remaining]
            )

    @staticmethod
    def _empty_page_reason(progress: _CollectionProgress) -> str:
        if progress.received == 0:
            return (
                "Exchange returned no candles for the requested range; "
                "the range may predate the market listing or contain no data"
            )
        return (
            "Exchange returned an empty page before requested coverage "
            "was reached"
        )

    def _build_result(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        collection_time: int,
        progress: _CollectionProgress,
    ) -> CollectionResult:
        quality = DataQualityChecker(self._repository).check(
            symbol,
            interval,
            start_time,
            end_time,
            exchange=self._exchange,
            market_type=self._market_type,
            as_of_time=collection_time,
        )
        failure_reason = progress.failure_reason
        if failure_reason is None and progress.conflicting:
            failure_reason = (
                "Incoming historical candles conflict with retained SQLite "
                "records"
            )
        if failure_reason is None and not quality.is_valid:
            failure_reason = "Persisted candle coverage is incomplete or invalid"

        return CollectionResult(
            exchange=self._exchange,
            market_type=self._market_type,
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            expected=quality.expected_candles,
            received=progress.received,
            inserted=progress.inserted,
            existing=progress.existing,
            conflicting=progress.conflicting,
            missing=quality.missing_candles,
            invalid=progress.invalid + quality.invalid_candles,
            unexpected=quality.unexpected_candles,
            excluded_unfinished=progress.excluded_unfinished,
            empty_pages=progress.empty_pages,
            conflict_open_times=tuple(progress.conflict_open_times),
            missing_open_times=quality.missing_open_times,
            failure_reason=failure_reason,
        )
