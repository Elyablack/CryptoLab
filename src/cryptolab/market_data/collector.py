"""Historical market data collection."""

import time

from cryptolab.market_data.client import BinanceSpotClient
from cryptolab.market_data.parser import parse_klines
from cryptolab.market_data.validator import validate_candles
from cryptolab.storage.sqlite_repository import SQLiteRepository


class HistoricalDataCollector:
    """Download, validate, and store historical Binance Spot candles."""

    PAGE_LIMIT = 1000

    def __init__(
        self,
        client: BinanceSpotClient,
        repository: SQLiteRepository,
    ) -> None:
        self._client = client
        self._repository = repository

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

        current_start_time = start_time
        collection_time = int(time.time() * 1000)

        while current_start_time < end_time:
            raw_klines = self._client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=self.PAGE_LIMIT,
                start_time=current_start_time,
                end_time=end_time - 1,
            )

            if raw_klines == []:
                break

            candles = parse_klines(raw_klines)
            validate_candles(candles)

            last_open_time = candles[-1].open_time
            next_start_time = last_open_time + 1
            if next_start_time <= current_start_time:
                raise ValueError(
                    "Kline pagination did not advance: "
                    f"requested start_time={current_start_time}, "
                    f"last open_time={last_open_time}"
                )

            closed_candles = [
                candle
                for candle in candles
                if start_time <= candle.open_time < end_time
                and candle.close_time < collection_time
            ]
            if closed_candles:
                self._repository.save_candles(
                    symbol,
                    interval,
                    closed_candles,
                )

            current_start_time = next_start_time

