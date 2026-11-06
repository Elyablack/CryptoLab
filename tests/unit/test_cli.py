import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import cryptolab.cli as cli_module
from cryptolab.cli import main
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


class FakeClient:
    created = 0

    def __init__(self) -> None:
        FakeClient.created += 1


class FakeCollector:
    last_instance: "FakeCollector | None" = None

    def __init__(self, client: object, repository: object) -> None:
        self.client = client
        self.repository = repository
        self.collect_call: dict[str, object] | None = None
        FakeCollector.last_instance = self

    def collect(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> None:
        self.collect_call = {
            "symbol": symbol,
            "interval": interval,
            "start_time": start_time,
            "end_time": end_time,
        }


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
    assert "--symbol" in output
    assert "--database" in output


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
    assert "range=[2024-01-01, 2024-01-02)" in output


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
