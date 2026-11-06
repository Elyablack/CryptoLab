import sqlite3
from pathlib import Path

import pytest

import cryptolab.cli as cli_module
from cryptolab.cli import main


class FakeClient:
    pass


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
    monkeypatch.setattr(cli_module, "BinanceSpotClient", FakeClient)
    monkeypatch.setattr(cli_module, "HistoricalDataCollector", FakeCollector)


def test_main_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])

    assert error.value.code == 0
    assert "collect" in capsys.readouterr().out


def test_collect_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["collect", "--help"])

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
