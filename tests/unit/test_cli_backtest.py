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
            volume=Decimal("100"),
            close_time=open_time + HOUR_MILLISECONDS - 1,
            quote_volume=Decimal("10000"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return candles


def save_bybit_candles(database_path: Path, candles: list[Candle]) -> None:
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles(
        "BTCUSDT",
        "1h",
        candles,
        exchange="bybit",
        market_type="spot",
    )


def backtest_arguments(database_path: Path) -> list[str]:
    return [
        "backtest",
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


def test_backtest_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["backtest", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--exchange" in output
    assert "--strategy" in output
    assert "--fee-rate" in output
    assert "no exchange orders are created" in " ".join(output.split())


def test_backtest_all_runs_three_independent_bybit_portfolios_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self) -> None:
            raise AssertionError("backtest must not create an HTTP client")

    monkeypatch.setattr(cli_module, "BinanceSpotClient", UnexpectedClient)
    monkeypatch.setattr(cli_module, "BybitSpotClient", UnexpectedClient)
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))

    exit_code = main(backtest_arguments(database_path))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.count("CryptoLab Backtest") == 3
    assert output.count("Initial cash:     1000.00 USDT") == 3
    assert "Strategy:         trend_following" in output
    assert "Strategy:         mean_reversion" in output
    assert "Strategy:         breakout" in output
    assert output.count("Exchange:         bybit") == 3


def test_backtest_single_strategy_uses_configured_fee(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--strategy",
        "breakout",
        "--fee-rate",
        "0.002",
    ])

    exit_code = main(arguments)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.count("CryptoLab Backtest") == 1
    assert "Strategy:         breakout" in output
    assert "Fee per fill:     0.2000%" in output


def test_backtest_rejects_incomplete_selected_exchange(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    save_bybit_candles(database_path, candles[:10] + candles[11:])

    exit_code = main(backtest_arguments(database_path))

    assert exit_code == 1
    assert "1 candles are missing" in capsys.readouterr().err


def test_backtest_rejects_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing.db"

    exit_code = main(backtest_arguments(database_path))

    assert exit_code == 1
    assert not database_path.exists()
    assert "Database does not exist" in capsys.readouterr().err


@pytest.mark.parametrize("fee_rate", ["invalid", "-0.1", "1"])
def test_backtest_rejects_invalid_fee_rate(fee_rate: str) -> None:
    with pytest.raises(SystemExit) as error:
        main([
            "backtest",
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
            "--fee-rate",
            fee_rate,
        ])

    assert error.value.code == 2
