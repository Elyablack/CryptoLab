from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from cryptolab.cli import main
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


HOUR_MILLISECONDS = 3_600_000
JANUARY_1_2024 = 1_704_067_200_000


def make_hourly_candles(
    count: int,
    *,
    start_time: int = JANUARY_1_2024,
) -> list[Candle]:
    candles = []
    for index in range(count):
        close = Decimal(100 + index)
        volume = Decimal(100 + index)
        open_time = start_time + index * HOUR_MILLISECONDS
        candles.append(
            Candle(
                open_time=open_time,
                open=close - Decimal("0.5"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=volume,
                close_time=open_time + HOUR_MILLISECONDS - 1,
                quote_volume=volume * close,
                number_of_trades=100 + index,
                taker_buy_base_volume=volume / 2,
                taker_buy_quote_volume=volume * close / 2,
            )
        )
    return candles


def save_candles(database_path: Path, candles: list[Candle]) -> None:
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles("BTCUSDT", "1h", candles)


def analyze_arguments(database_path: Path) -> list[str]:
    return [
        "analyze",
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


def test_analyze_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["analyze", "--help"])

    assert error.value.code == 0
    assert "--symbol" in capsys.readouterr().out


def test_analyze_valid_hourly_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_candles(database_path, make_hourly_candles(72))

    exit_code = main(analyze_arguments(database_path))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "CryptoLab Market Analysis" in output
    assert "Symbol:          BTCUSDT" in output
    assert "Per-period return volatility (1h)" in output
    assert "annual" not in output.lower()


def test_analyze_rejects_missing_candle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    save_candles(database_path, candles[:10] + candles[11:])

    exit_code = main(analyze_arguments(database_path))

    assert exit_code == 1
    assert "1 candles are missing" in capsys.readouterr().err


def test_analyze_rejects_malformed_candle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    candles[10] = replace(candles[10], high=Decimal("1"))
    save_candles(database_path, candles)

    exit_code = main(analyze_arguments(database_path))

    assert exit_code == 1
    assert "high price cannot be less" in capsys.readouterr().err


def test_analyze_rejects_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing.db"

    exit_code = main(analyze_arguments(database_path))

    assert exit_code == 1
    assert not database_path.exists()
    assert "Database does not exist" in capsys.readouterr().err


def test_analyze_rejects_unclosed_future_candles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "future.db"
    future_start = 4_070_908_800_000  # 2099-01-01T00:00:00Z
    save_candles(database_path, make_hourly_candles(72, start_time=future_start))
    arguments = analyze_arguments(database_path)
    arguments[arguments.index("2024-01-01")] = "2099-01-01"
    arguments[arguments.index("2024-01-04")] = "2099-01-04"

    exit_code = main(arguments)

    assert exit_code == 1
    assert "not closed" in capsys.readouterr().err
