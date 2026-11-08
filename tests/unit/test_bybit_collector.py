from cryptolab.market_data.collector import HistoricalDataCollector
from cryptolab.market_data.models import Candle
from cryptolab.market_data.parser import parse_bybit_klines
from cryptolab.storage.sqlite_repository import SaveCandlesResult


HOUR_MILLISECONDS = 3_600_000


def response(open_times: list[int]) -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "spot",
            "symbol": "BTCUSDT",
            "list": [
                [
                    str(open_time),
                    "100",
                    "120",
                    "90",
                    "110",
                    "10",
                    "1000",
                ]
                for open_time in open_times
            ],
        },
    }


class FakeBybitClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> object:
        self.calls.append({
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "start_time": start_time,
            "end_time": end_time,
        })
        return next(self._responses)


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, list[Candle], str, str]] = []

    def save_candles(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
        *,
        exchange: str,
        market_type: str,
    ) -> SaveCandlesResult:
        self.saved.append(
            (symbol, interval, candles, exchange, market_type)
        )
        return SaveCandlesResult(len(candles), 0, 0, ())

    def get_candles_by_range(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        exchange: str,
        market_type: str,
    ) -> list[Candle]:
        return sorted(
            (
                candle
                for stored_symbol, stored_interval, candles, stored_exchange,
                stored_market_type in self.saved
                for candle in candles
                if stored_symbol == symbol
                and stored_interval == interval
                and stored_exchange == exchange
                and stored_market_type == market_type
                and start_time <= candle.open_time < end_time
            ),
            key=lambda candle: candle.open_time,
        )


def test_bybit_collection_pages_backward_and_stores_source_identity() -> None:
    client = FakeBybitClient([
        response([3 * HOUR_MILLISECONDS, 2 * HOUR_MILLISECONDS]),
        response([HOUR_MILLISECONDS, 0]),
    ])
    repository = FakeRepository()
    collector = HistoricalDataCollector(  # type: ignore[arg-type]
        client,
        repository,
        exchange="bybit",
        market_type="spot",
        parser=parse_bybit_klines,
        pagination_direction="backward",
    )

    collector.collect(
        "BTCUSDT",
        "1h",
        start_time=0,
        end_time=4 * HOUR_MILLISECONDS,
    )

    assert [call["end_time"] for call in client.calls] == [
        4 * HOUR_MILLISECONDS - 1,
        2 * HOUR_MILLISECONDS - 1,
    ]
    assert all(batch[3:] == ("bybit", "spot") for batch in repository.saved)
    saved_open_times = sorted(
        candle.open_time
        for _, _, candles, _, _ in repository.saved
        for candle in candles
    )
    assert saved_open_times == [
        0,
        HOUR_MILLISECONDS,
        2 * HOUR_MILLISECONDS,
        3 * HOUR_MILLISECONDS,
    ]
    assert all(
        candle.number_of_trades is None
        for _, _, candles, _, _ in repository.saved
        for candle in candles
    )
