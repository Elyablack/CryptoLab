from decimal import Decimal
from pathlib import Path

import pytest

import cryptolab.cli as cli_module
from cryptolab.cli import main
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


HOUR_MILLISECONDS = 3_600_000
JANUARY_1_2024 = 1_704_067_200_000


def make_hourly_candles(count: int) -> list[Candle]:
    candles = []
    for index in range(count):
        open_time = JANUARY_1_2024 + index * HOUR_MILLISECONDS
        candles.append(Candle(
            open_time=open_time,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            close_time=open_time + HOUR_MILLISECONDS - 1,
            quote_volume=Decimal("1000"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return candles


def research_arguments(database_path: Path) -> list[str]:
    return [
        "research",
        "--exchange",
        "bybit",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1h",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-04",
        "--database",
        str(database_path),
    ]


def save_candles(database_path: Path, candles: list[Candle]) -> None:
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles(
        "BTCUSDT",
        "1h",
        candles,
        exchange="bybit",
        market_type="spot",
    )


def test_research_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["research", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--exchange" in output
    assert "--fee-rate" in output
    assert "no strategy parameters or exchange orders are changed" in " ".join(
        output.split()
    )


def test_research_cli_uses_local_bybit_data_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self) -> None:
            raise AssertionError("research must not create an HTTP client")

    monkeypatch.setattr(cli_module, "BinanceSpotClient", UnexpectedClient)
    monkeypatch.setattr(cli_module, "BybitSpotClient", UnexpectedClient)
    database_path = tmp_path / "market_data.db"
    save_candles(database_path, make_hourly_candles(72))

    exit_code = main(research_arguments(database_path))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Dataset: bybit/spot BTCUSDT 1h" in output
    assert "Candles: 72" in output
    assert "trend_following:" in output
    assert "buy_and_hold:" in output
    assert "cash_only:" in output


def test_research_cli_rejects_incomplete_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    save_candles(database_path, candles[:20] + candles[21:])

    exit_code = main(research_arguments(database_path))

    assert exit_code == 1
    assert "1 candles are missing" in capsys.readouterr().err


def test_research_cli_rejects_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing.db"

    exit_code = main(research_arguments(database_path))

    assert exit_code == 1
    assert not database_path.exists()
    assert "Database does not exist" in capsys.readouterr().err

