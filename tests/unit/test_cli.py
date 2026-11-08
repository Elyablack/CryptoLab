import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import cryptolab.cli as cli_module
from cryptolab.cli import main
from cryptolab.market_data.collector import CollectionResult
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


class FakeClient:
    created = 0

    def __init__(self) -> None:
        FakeClient.created += 1


class FakeCollector:
    last_instance: "FakeCollector | None" = None

    def __init__(
        self,
        client: object,
        repository: object,
        **configuration: object,
    ) -> None:
        self.client = client
        self.repository = repository
        self.configuration = configuration
        self.collect_call: dict[str, object] | None = None
        FakeCollector.last_instance = self

    def collect(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> CollectionResult:
        self.collect_call = {
            "symbol": symbol,
            "interval": interval,
            "start_time": start_time,
            "end_time": end_time,
        }
        expected = (end_time - start_time) // 3_600_000
        exchange = str(self.configuration.get("exchange", "binance"))
        market_type = str(self.configuration.get("market_type", "spot"))
        return CollectionResult(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            expected=expected,
            received=expected,
            inserted=expected,
            existing=0,
            conflicting=0,
            missing=0,
            invalid=0,
            unexpected=0,
            excluded_unfinished=0,
            empty_pages=0,
            conflict_open_times=(),
            missing_open_times=(),
        )


@pytest.fixture(autouse=True)
def fake_collection_components(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeCollector.last_instance = None
    FakeClient.created = 0
    monkeypatch.setattr(cli_module, "BinanceSpotClient", FakeClient)
    monkeypatch.setattr(cli_module, "HistoricalDataCollector", FakeCollector)


def test_main_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "collect" in output
    assert "quality" in output


def test_collect_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["collect", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--exchange" in output
    assert "--symbol" in output
    assert "--database" in output
    assert "--verbose" in output


def test_quality_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["quality", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--symbol" in output
    assert "--database" in output


def test_collect_converts_utc_dates_and_initializes_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "nested" / "market_data.db"

    exit_code = main(
        [
            "collect",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--database",
            str(database_path),
        ]
    )

    assert exit_code == 0
    assert FakeCollector.last_instance is not None
    assert FakeCollector.last_instance.collect_call == {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "start_time": 1_704_067_200_000,
        "end_time": 1_704_153_600_000,
    }
    assert database_path.exists()

    with sqlite3.connect(database_path) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "candles"),
        ).fetchone()

    assert table == ("candles",)
    output = capsys.readouterr().out
    assert "Collection completed" in output
    assert "exchange=binance" in output
    assert "range=[2024-01-01, 2024-01-02)" in output


def test_collect_configures_bybit_spot_backward_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBybitClient:
        pass

    monkeypatch.setattr(cli_module, "BybitSpotClient", FakeBybitClient)

    exit_code = main([
        "collect",
        "--exchange",
        "bybit",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1h",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-02",
        "--database",
        str(tmp_path / "market_data.db"),
    ])

    assert exit_code == 0
    assert FakeCollector.last_instance is not None
    assert isinstance(FakeCollector.last_instance.client, FakeBybitClient)
    assert FakeCollector.last_instance.configuration == {
        "exchange": "bybit",
        "market_type": "spot",
        "parser": cli_module.parse_bybit_klines,
        "pagination_direction": "backward",
    }


def test_collect_uses_default_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "collect",
            "--symbol",
            "ETHUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "data" / "market_data.db").exists()


def test_collect_returns_nonzero_for_incomplete_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class IncompleteCollector(FakeCollector):
        def collect(self, *args, **kwargs) -> CollectionResult:
            result = super().collect(*args, **kwargs)
            return replace(
                result,
                missing=1,
                missing_open_times=(result.start_time,),
                failure_reason="Persisted candle coverage is incomplete",
            )

    monkeypatch.setattr(
        cli_module,
        "HistoricalDataCollector",
        IncompleteCollector,
    )

    exit_code = main([
        "collect",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1h",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-02",
        "--database",
        str(tmp_path / "market_data.db"),
    ])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "missing=1" in captured.out
    assert "Collection incomplete" in captured.err


def test_quality_returns_nonzero_for_invalid_stored_candle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "invalid.db"
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles("BTCUSDT", "1d", [Candle(
        open_time=0,
        open=Decimal("100"),
        high=Decimal("99"),
        low=Decimal("90"),
        close=Decimal("95"),
        volume=Decimal("1"),
        close_time=86_399_999,
        quote_volume=Decimal("100"),
        number_of_trades=1,
        taker_buy_base_volume=Decimal("0.5"),
        taker_buy_quote_volume=Decimal("50"),
    )])

    exit_code = main([
        "quality",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1d",
        "--start",
        "1970-01-01",
        "--end",
        "1970-01-02",
        "--database",
        str(database_path),
    ])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "missing_candles=0" in output
    assert "invalid_candles=1" in output


def test_collect_rejects_invalid_date_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "collect",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "1h",
                "--start",
                "2024/01/01",
                "--end",
                "2024-01-02",
            ]
        )

    assert error.value.code == 2
    assert "expected YYYY-MM-DD" in capsys.readouterr().err


def test_collect_rejects_non_increasing_date_range(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "collect",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "1h",
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-02",
            ]
        )

    assert error.value.code == 2
    assert "--end must be later than --start" in capsys.readouterr().err


def test_collect_rejects_unknown_exchange() -> None:
    with pytest.raises(SystemExit) as error:
        main([
            "collect",
            "--exchange",
            "kraken",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
        ])

    assert error.value.code == 2


def test_collect_requires_arguments() -> None:
    with pytest.raises(SystemExit) as error:
        main(["collect"])

    assert error.value.code == 2


def test_collect_returns_nonzero_code_on_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingCollector(FakeCollector):
        def collect(
            self,
            symbol: str,
            interval: str,
            start_time: int,
            end_time: int,
        ) -> None:
            raise RuntimeError("API unavailable")

    monkeypatch.setattr(cli_module, "HistoricalDataCollector", FailingCollector)

    exit_code = main(
        [
            "collect",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--database",
            str(tmp_path / "market_data.db"),
        ]
    )

    assert exit_code == 1
    assert "Collection failed: API unavailable" in capsys.readouterr().err


def test_quality_returns_zero_for_complete_range_without_http(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles(
        "BTCUSDT",
        "1d",
        [_daily_candle(1_704_067_200_000)],
    )

    exit_code = main(
        [
            "quality",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1d",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--database",
            str(database_path),
        ]
    )

    assert exit_code == 0
    assert FakeClient.created == 0
    output = capsys.readouterr().out
    assert "expected_candles=1" in output
    assert "actual_candles=1" in output
    assert "missing_candles=0" in output


def test_quality_uses_selected_exchange(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles(
        "BTCUSDT",
        "1d",
        [_daily_candle(1_704_067_200_000)],
        exchange="bybit",
        market_type="spot",
    )

    exit_code = main([
        "quality",
        "--exchange",
        "bybit",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1d",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-02",
        "--database",
        str(database_path),
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "exchange=bybit" in output
    assert "actual_candles=1" in output


def test_quality_returns_one_and_prints_missing_times_in_utc(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    SQLiteRepository(database_path).init_schema()

    exit_code = main(
        [
            "quality",
            "--symbol",
            "ETHUSDT",
            "--interval",
            "1d",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-03",
            "--database",
            str(database_path),
        ]
    )

    assert exit_code == 1
    assert FakeClient.created == 0
    output = capsys.readouterr().out
    assert "expected_candles=2" in output
    assert "actual_candles=0" in output
    assert "missing_candles=2" in output
    assert "2024-01-01T00:00:00Z" in output
    assert "2024-01-02T00:00:00Z" in output


def test_quality_reports_truncated_missing_times(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    SQLiteRepository(database_path).init_schema()

    exit_code = main(
        [
            "quality",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1m",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--database",
            str(database_path),
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "missing_candles=1440" in output
    assert "missing_open_times truncated: showing first 1000 of 1440" in output


def test_quality_requires_arguments() -> None:
    with pytest.raises(SystemExit) as error:
        main(["quality"])

    assert error.value.code == 2


def test_quality_returns_one_on_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingChecker:
        def __init__(self, repository: object) -> None:
            pass

        def check(
            self,
            symbol: str,
            interval: str,
            start_time: int,
            end_time: int,
        ) -> None:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(cli_module, "DataQualityChecker", FailingChecker)

    exit_code = main(
        [
            "quality",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1d",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--database",
            str(tmp_path / "market_data.db"),
        ]
    )

    assert exit_code == 1
    assert "Quality check failed: database unavailable" in capsys.readouterr().err


def _daily_candle(open_time: int) -> Candle:
    return Candle(
        open_time=open_time,
        open=Decimal("100"),
        high=Decimal("120"),
        low=Decimal("90"),
        close=Decimal("110"),
        volume=Decimal("10"),
        close_time=open_time + 86_400_000 - 1,
        quote_volume=Decimal("1000"),
        number_of_trades=25,
        taker_buy_base_volume=Decimal("6"),
        taker_buy_quote_volume=Decimal("600"),
    )
