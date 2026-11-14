"""Public Bybit market observations used by the Stage 3 paper loop."""

import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from cryptolab.market_data.bybit_client import BybitSpotClient
from cryptolab.market_data.collector import (
    CollectionFailure,
    HistoricalDataCollector,
)
from cryptolab.market_data.data_quality import DataQualityChecker
from cryptolab.market_data.models import Candle
from cryptolab.market_data.parser import parse_bybit_klines
from cryptolab.market_data.validator import validate_historical_candles
from cryptolab.replay.models import PriceObservation
from cryptolab.storage.sqlite_repository import SQLiteRepository


class PaperMarketSource(Protocol):
    """Closed-candle storage plus a separately observed public price."""

    def load_closed_candles(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        received_at: int,
    ) -> list[Candle]: ...

    def observe_price(self, symbol: str) -> PriceObservation: ...


class BybitPaperMarketSource:
    """Recover exact closed-candle coverage through Bybit public REST."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        client: BybitSpotClient | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._repository = SQLiteRepository(database_path)
        self._repository.init_schema()
        self._client = client or BybitSpotClient()
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._collector = HistoricalDataCollector(
            self._client,
            self._repository,
            exchange="bybit",
            market_type="spot",
            parser=parse_bybit_klines,
            pagination_direction="backward",
        )

    def load_closed_candles(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        received_at: int,
    ) -> list[Candle]:
        checker = DataQualityChecker(self._repository)
        quality = checker.check(
            symbol,
            interval,
            start_time,
            end_time,
            exchange="bybit",
            market_type="spot",
            as_of_time=received_at,
        )
        if not quality.is_valid:
            non_missing_issues = (
                quality.invalid_candles
                + quality.unexpected_candles
                + quality.duplicate_candles
            )
            if non_missing_issues:
                raise ValueError(
                    "Stored Bybit range is invalid; repair it before paper "
                    "trading"
                )
            try:
                result = self._collector.collect(
                    symbol,
                    interval,
                    start_time,
                    end_time,
                )
            except CollectionFailure as error:
                raise RuntimeError(
                    f"Closed-candle recovery failed: {error}"
                ) from error
            if not result.succeeded:
                raise RuntimeError(
                    "Closed-candle recovery did not reach exact coverage: "
                    f"missing={result.missing}, "
                    f"conflicting={result.conflicting}, invalid={result.invalid}"
                )
            quality = checker.check(
                symbol,
                interval,
                start_time,
                end_time,
                exchange="bybit",
                market_type="spot",
                as_of_time=received_at,
            )
        if not quality.is_valid:
            raise RuntimeError("Closed-candle coverage remains incomplete")
        candles = self._repository.get_candles_by_range(
            symbol,
            interval,
            start_time,
            end_time,
            exchange="bybit",
            market_type="spot",
        )
        validate_historical_candles(
            candles,
            interval,
            as_of_time=received_at,
        )
        return candles

    def observe_price(self, symbol: str) -> PriceObservation:
        response = self._client.get_ticker(symbol)
        price = parse_bybit_last_price(response, symbol)
        return PriceObservation(
            price=price,
            observed_at=self._now_ms(),
            source="bybit_public_v5_ticker_lastPrice",
        )


def parse_bybit_last_price(raw: object, symbol: str) -> Decimal:
    """Parse one positive finite lastPrice without trusting response shape."""
    if not isinstance(raw, Mapping):
        raise ValueError("Bybit ticker response must be an object")
    ret_code = raw.get("retCode")
    if type(ret_code) is not int or ret_code != 0:
        raise ValueError(
            f"Bybit ticker API error {ret_code}: "
            f"{raw.get('retMsg', 'unknown error')}"
        )
    result = raw.get("result")
    if not isinstance(result, Mapping) or result.get("category") != "spot":
        raise ValueError("Bybit ticker result must be Spot data")
    items = result.get("list")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Bybit ticker must contain exactly one instrument")
    item = items[0]
    if not isinstance(item, Mapping) or item.get("symbol") != symbol:
        raise ValueError("Bybit ticker symbol does not match the request")
    value = item.get("lastPrice")
    if not isinstance(value, str):
        raise ValueError("Bybit ticker lastPrice must be a decimal string")
    try:
        price = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Bybit ticker lastPrice is invalid") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("Bybit ticker lastPrice must be finite and positive")
    return price
