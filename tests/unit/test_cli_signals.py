from dataclasses import replace
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
        candles.append(
            Candle(
                open_time=open_time,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("100"),
                close_time=open_time + HOUR_MILLISECONDS - 1,
                quote_volume=Decimal("10000"),
                number_of_trades=100,
                taker_buy_base_volume=Decimal("50"),
                taker_buy_quote_volume=Decimal("5000"),
            )
        )
    return candles


def save_candles(database_path: Path, candles: list[Candle]) -> None:
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles("BTCUSDT", "1h", candles)


def signal_arguments(database_path: Path, strategy: str = "all") -> list[str]:
    return [
        "signals",
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
        "--strategy",
        strategy,
    ]


def test_signals_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["signals", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--strategy" in output
    assert "no orders are created" in " ".join(output.split())


def test_signals_prints_breakout_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self) -> None:
            raise AssertionError("signals must not create an HTTP client")

    monkeypatch.setattr(cli_module, "BinanceSpotClient", UnexpectedClient)
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    candles[21] = replace(
        candles[21],
        open=Decimal("103"),
        high=Decimal("105"),
        low=Decimal("103"),
        close=Decimal("104"),
        volume=Decimal("300"),
        quote_volume=Decimal("31200"),
        taker_buy_base_volume=Decimal("150"),
        taker_buy_quote_volume=Decimal("15600"),
    )
    save_candles(database_path, candles)

    exit_code = main(signal_arguments(database_path, strategy="breakout"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "breakout BUY" in output
    assert "available_at=2024-01-01T22:00:00Z" in output
    assert "available_at=2024-01-01T21:00:00Z" not in output
    assert "price=104" in output


def test_signals_reports_no_actionable_signal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_candles(database_path, make_hourly_candles(72))

    exit_code = main(signal_arguments(database_path))

    assert exit_code == 0
    assert "No BUY/SELL signals found" in capsys.readouterr().out


def test_signals_rejects_incomplete_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    save_candles(database_path, candles[:10] + candles[11:])

    exit_code = main(signal_arguments(database_path))

    assert exit_code == 1
    assert "1 candles are missing" in capsys.readouterr().err


def test_signals_rejects_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing.db"

    exit_code = main(signal_arguments(database_path))

    assert exit_code == 1
    assert not database_path.exists()
    assert "Database does not exist" in capsys.readouterr().err


def test_signals_rejects_unknown_strategy() -> None:
    with pytest.raises(SystemExit) as error:
        main([
            "signals",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-04",
            "--strategy",
            "unknown",
        ])

    assert error.value.code == 2
