from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import cryptolab.market_data.collector as collector_module
from cryptolab.market_data.collector import HistoricalDataCollector
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


def make_raw_kline(open_time: int, close_time: int | None = None) -> list[Any]:
    return [
        open_time,
        "100",
        "120",
        "90",
        "110",
        "10",
        close_time if close_time is not None else open_time + 999,
        "1000",
        25,
        "6",
        "600",
        "0",
    ]


def make_candle(open_time: int, close_time: int | None = None) -> Candle:
    return Candle(
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("90"),
        close=Decimal("110"),
        volume=Decimal("10"),
        close_time=close_time if close_time is not None else open_time + 999,
        quote_volume=Decimal("1000"),
        number_of_trades=25,
        taker_buy_base_volume=Decimal("6"),
        taker_buy_quote_volume=Decimal("600"),
    )


class FakeClient:
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
        self.calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        return next(self._responses, [])


class FakeRepository:
    def __init__(self) -> None:
        self.saved_batches: list[tuple[str, str, list[Candle]]] = []

    def save_candles(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
    ) -> None:
        self.saved_batches.append((symbol, interval, candles))


def make_collector(
    client: FakeClient,
    repository: FakeRepository | SQLiteRepository,
) -> HistoricalDataCollector:
    return HistoricalDataCollector(  # type: ignore[arg-type]
        client=client,
        repository=repository,
    )


def test_collect_parses_validates_and_saves_closed_candles() -> None:
    client = FakeClient([[make_raw_kline(1_000)], []])
    repository = FakeRepository()
    collector = make_collector(client, repository)

    collector.collect("BTCUSDT", "1h", start_time=1_000, end_time=3_000)

    assert repository.saved_batches == [
        ("BTCUSDT", "1h", [make_candle(1_000)])
    ]
    assert client.calls[0] == {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 1000,
        "start_time": 1_000,
        "end_time": 2_999,
    }


def test_collect_loads_multiple_pages_using_last_open_time() -> None:
    client = FakeClient(
        [
            [make_raw_kline(0), make_raw_kline(1_000)],
            [make_raw_kline(2_000), make_raw_kline(3_000)],
            [],
        ]
    )
    repository = FakeRepository()
    collector = make_collector(client, repository)

    collector.collect("BTCUSDT", "1h", start_time=0, end_time=4_000)

    assert [call["start_time"] for call in client.calls] == [0, 1_001, 3_001]
    assert [
        candle.open_time
        for _, _, candles in repository.saved_batches
        for candle in candles
    ] == [0, 1_000, 2_000, 3_000]


def test_collect_handles_empty_response() -> None:
    client = FakeClient([[]])
    repository = FakeRepository()
    collector = make_collector(client, repository)

    collector.collect("ETHUSDT", "1h", start_time=0, end_time=1_000)

    assert repository.saved_batches == []
    assert len(client.calls) == 1


def test_collect_rejects_candle_before_requested_start_time() -> None:
    client = FakeClient([
        [make_raw_kline(1_000)],
        [make_raw_kline(1_000)],
    ])

    repository = FakeRepository()
    collector = make_collector(client, repository)

    with pytest.raises(
        ValueError,
        match="Binance returned a candle before the requested start_time",
    ):
        collector.collect(
            "BTCUSDT",
            "1h",
            start_time=1_000,
            end_time=3_000,
        )

    assert len(client.calls) == 2


def test_collect_does_not_save_unclosed_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector_module.time, "time", lambda: 10.0)
    client = FakeClient(
        [
            [
                make_raw_kline(9_000, close_time=9_999),
                make_raw_kline(10_000, close_time=10_999),
            ],
            [],
        ]
    )
    repository = FakeRepository()
    collector = make_collector(client, repository)

    collector.collect("BTCUSDT", "1h", start_time=9_000, end_time=12_000)

    assert repository.saved_batches == [
        ("BTCUSDT", "1h", [make_candle(9_000, close_time=9_999)])
    ]


def test_collect_validates_before_saving() -> None:
    invalid_kline = make_raw_kline(1_000)
    invalid_kline[2] = "99"
    client = FakeClient([[invalid_kline]])
    repository = FakeRepository()
    collector = make_collector(client, repository)

    with pytest.raises(ValueError, match="high price cannot be less than open price"):
        collector.collect("BTCUSDT", "1h", start_time=1_000, end_time=3_000)

    assert repository.saved_batches == []


def test_collect_rejects_invalid_time_range() -> None:
    collector = make_collector(FakeClient([]), FakeRepository())

    with pytest.raises(ValueError, match="end_time must be greater than start_time"):
        collector.collect("BTCUSDT", "1h", start_time=1_000, end_time=1_000)


def test_repeated_collection_does_not_duplicate_candles(tmp_path: Path) -> None:
    client = FakeClient(
        [
            [make_raw_kline(1_000)],
            [],
            [make_raw_kline(1_000)],
            [],
        ]
    )
    repository = SQLiteRepository(tmp_path / "candles.sqlite3")
    repository.init_schema()
    collector = make_collector(client, repository)

    collector.collect("BTCUSDT", "1h", start_time=1_000, end_time=3_000)
    collector.collect("BTCUSDT", "1h", start_time=1_000, end_time=3_000)

    assert repository.get_candles("BTCUSDT", "1h") == [make_candle(1_000)]
