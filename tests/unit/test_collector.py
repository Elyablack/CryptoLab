from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import cryptolab.market_data.collector as collector_module
from cryptolab.market_data.collector import (
    CollectionFailure,
    HistoricalDataCollector,
)
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import (
    CandleConflict,
    SQLiteRepository,
    SaveCandlesResult,
)


HOUR = 3_600_000


def make_raw_kline(open_time: int, close_time: int | None = None) -> list[Any]:
    return [
        open_time,
        "100",
        "120",
        "90",
        "110",
        "10",
        close_time if close_time is not None else open_time + HOUR - 1,
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
        close_time=(
            close_time if close_time is not None else open_time + HOUR - 1
        ),
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
        self.candles: dict[tuple[str, str, int], Candle] = {}

    def save_candles(
        self,
        symbol: str,
        interval: str,
        candles: list[Candle],
    ) -> SaveCandlesResult:
        self.saved_batches.append((symbol, interval, candles))
        inserted = 0
        existing = 0
        conflicts = []
        for candle in candles:
            key = (symbol, interval, candle.open_time)
            retained = self.candles.get(key)
            if retained is None:
                self.candles[key] = candle
                inserted += 1
            elif retained == candle:
                existing += 1
            else:
                conflicts.append(CandleConflict(
                    candle.open_time,
                    retained,
                    candle,
                ))
        return SaveCandlesResult(
            inserted,
            existing,
            len(conflicts),
            tuple(conflicts),
        )

    def get_candles_by_range(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        **identity: str,
    ) -> list[Candle]:
        del identity
        return sorted(
            (
                candle
                for (stored_symbol, stored_interval, _), candle
                in self.candles.items()
                if stored_symbol == symbol
                and stored_interval == interval
                and start_time <= candle.open_time < end_time
            ),
            key=lambda candle: candle.open_time,
        )


def make_collector(
    client: FakeClient,
    repository: FakeRepository | SQLiteRepository,
) -> HistoricalDataCollector:
    return HistoricalDataCollector(  # type: ignore[arg-type]
        client=client,
        repository=repository,
    )


def test_collect_parses_validates_and_saves_closed_candles() -> None:
    client = FakeClient([[make_raw_kline(HOUR)]])
    repository = FakeRepository()
    collector = make_collector(client, repository)

    result = collector.collect(
        "BTCUSDT", "1h", start_time=HOUR, end_time=2 * HOUR
    )

    assert repository.saved_batches == [
        ("BTCUSDT", "1h", [make_candle(HOUR)])
    ]
    assert client.calls[0] == {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 1000,
        "start_time": HOUR,
        "end_time": 2 * HOUR - 1,
    }
    assert result.succeeded
    assert result.inserted == 1


def test_collect_loads_multiple_pages_using_last_open_time() -> None:
    client = FakeClient(
        [
            [make_raw_kline(0), make_raw_kline(HOUR)],
            [make_raw_kline(2 * HOUR), make_raw_kline(3 * HOUR)],
        ]
    )
    repository = FakeRepository()
    collector = make_collector(client, repository)

    result = collector.collect(
        "BTCUSDT", "1h", start_time=0, end_time=4 * HOUR
    )

    assert [call["start_time"] for call in client.calls] == [0, 2 * HOUR]
    assert [
        candle.open_time
        for _, _, candles in repository.saved_batches
        for candle in candles
    ] == [0, HOUR, 2 * HOUR, 3 * HOUR]
    assert result.succeeded


def test_collect_handles_empty_response() -> None:
    client = FakeClient([[]])
    repository = FakeRepository()
    collector = make_collector(client, repository)

    result = collector.collect("ETHUSDT", "1h", start_time=0, end_time=HOUR)

    assert repository.saved_batches == []
    assert len(client.calls) == 1
    assert not result.succeeded
    assert result.empty_pages == 1
    assert result.missing == 1


def test_collect_rejects_candle_before_requested_start_time() -> None:
    client = FakeClient([
        [make_raw_kline(HOUR)],
        [make_raw_kline(HOUR)],
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
            start_time=HOUR,
            end_time=3 * HOUR,
        )

    assert len(client.calls) == 2


def test_collect_does_not_save_unclosed_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        collector_module.time,
        "time",
        lambda: 10 * HOUR / 1000,
    )
    client = FakeClient(
        [
            [
                    make_raw_kline(9 * HOUR),
                    make_raw_kline(10 * HOUR),
            ],
            [],
        ]
    )
    repository = FakeRepository()
    collector = make_collector(client, repository)

    result = collector.collect(
        "BTCUSDT", "1h", start_time=9 * HOUR, end_time=12 * HOUR
    )

    assert repository.saved_batches == [
        ("BTCUSDT", "1h", [make_candle(9 * HOUR)])
    ]
    assert result.excluded_unfinished == 1
    assert not result.succeeded


def test_collect_validates_before_saving() -> None:
    invalid_kline = make_raw_kline(HOUR)
    invalid_kline[2] = "99"
    client = FakeClient([[invalid_kline]])
    repository = FakeRepository()
    collector = make_collector(client, repository)

    with pytest.raises(ValueError, match="high price cannot be less than open price"):
        collector.collect(
            "BTCUSDT", "1h", start_time=HOUR, end_time=2 * HOUR
        )

    assert repository.saved_batches == []


def test_collect_rejects_invalid_time_range() -> None:
    collector = make_collector(FakeClient([]), FakeRepository())

    with pytest.raises(ValueError, match="end_time must be greater than start_time"):
        collector.collect("BTCUSDT", "1h", start_time=HOUR, end_time=HOUR)


def test_repeated_collection_does_not_duplicate_candles(tmp_path: Path) -> None:
    client = FakeClient(
        [
            [make_raw_kline(HOUR)],
            [make_raw_kline(HOUR)],
        ]
    )
    repository = SQLiteRepository(tmp_path / "candles.sqlite3")
    repository.init_schema()
    collector = make_collector(client, repository)

    first = collector.collect(
        "BTCUSDT", "1h", start_time=HOUR, end_time=2 * HOUR
    )
    second = collector.collect(
        "BTCUSDT", "1h", start_time=HOUR, end_time=2 * HOUR
    )

    assert repository.get_candles("BTCUSDT", "1h") == [make_candle(HOUR)]
    assert first.inserted == 1
    assert second.existing == 1


def test_partial_response_cannot_report_success() -> None:
    client = FakeClient([[make_raw_kline(0)], []])
    repository = FakeRepository()

    result = make_collector(client, repository).collect(
        "BTCUSDT", "1h", 0, 2 * HOUR
    )

    assert not result.succeeded
    assert result.received == 1
    assert result.inserted == 1
    assert result.missing == 1
    assert result.empty_pages == 1


def test_incorrect_close_time_is_an_invalid_page() -> None:
    client = FakeClient([[make_raw_kline(0, close_time=HOUR)]])
    repository = FakeRepository()

    with pytest.raises(CollectionFailure) as error:
        make_collector(client, repository).collect(
            "BTCUSDT", "1h", 0, HOUR
        )

    assert error.value.result.invalid == 1
    assert error.value.result.inserted == 0
    assert "close_time does not match" in str(error.value)


def test_changed_historical_candle_is_reported_and_retained(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "candles.sqlite3")
    repository.init_schema()
    repository.save_candles("BTCUSDT", "1h", [make_candle(0)])
    changed = make_raw_kline(0)
    changed[4] = "115"

    result = make_collector(FakeClient([[changed]]), repository).collect(
        "BTCUSDT", "1h", 0, HOUR
    )

    assert not result.succeeded
    assert result.conflicting == 1
    assert result.conflict_open_times == (0,)
    assert repository.get_candles("BTCUSDT", "1h") == [make_candle(0)]
