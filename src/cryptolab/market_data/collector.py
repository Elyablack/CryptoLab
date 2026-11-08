"""Historical market data collection."""

import time
from collections.abc import Callable
from typing import Literal, Protocol

from cryptolab.market_data.markets import (
    DEFAULT_EXCHANGE,
    SPOT_MARKET_TYPE,
    validate_market_identity,
)
from cryptolab.market_data.models import Candle
from cryptolab.market_data.parser import parse_klines
from cryptolab.market_data.validator import validate_candles
from cryptolab.storage.sqlite_repository import SQLiteRepository


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
    ) -> None:
        """Collect candles for the half-open interval [start_time, end_time)."""
        if end_time <= start_time:
            raise ValueError("end_time must be greater than start_time")

        collection_time = int(time.time() * 1000)
        if self._pagination_direction == "forward":
            self._collect_forward(
                symbol,
                interval,
                start_time,
                end_time,
                collection_time,
            )
        else:
            self._collect_backward(
                symbol,
                interval,
                start_time,
                end_time,
                collection_time,
            )

    def _collect_forward(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        collection_time: int,
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
                break

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
            )
            current_start_time = next_start_time

    def _collect_backward(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        collection_time: int,
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
                break

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
            )
            current_end_time = next_end_time

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
        candles = self._parser(raw_klines, symbol, interval)
        validate_candles(candles)
        return candles

    def _save_closed(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
        start_time: int,
        end_time: int,
        collection_time: int,
    ) -> None:
        closed_candles = [
            candle
            for candle in candles
            if start_time <= candle.open_time < end_time
            and candle.close_time < collection_time
        ]
        if closed_candles:
            if (
                self._exchange == DEFAULT_EXCHANGE
                and self._market_type == SPOT_MARKET_TYPE
            ):
                self._repository.save_candles(
                    symbol,
                    interval,
                    closed_candles,
                )
            else:
                self._repository.save_candles(
                    symbol,
                    interval,
                    closed_candles,
                    exchange=self._exchange,
                    market_type=self._market_type,
                )
