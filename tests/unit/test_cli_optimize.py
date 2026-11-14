import json
from decimal import Decimal
from pathlib import Path

import pytest

import cryptolab.cli as cli_module
from cryptolab.cli import main
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository


HOUR = 3_600_000
START = 1_704_067_200_000


def make_candles(count: int) -> list[Candle]:
    result = []
    for index in range(count):
        open_time = START + index * HOUR
        price = Decimal("100") + Decimal(index % 5) / 10
        result.append(Candle(
            open_time=open_time,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("10"),
            close_time=open_time + HOUR - 1,
            quote_volume=price * 10,
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return result


def save(database: Path, candles: list[Candle]) -> None:
    repository = SQLiteRepository(database)
    repository.init_schema()
    repository.save_candles(
        "BTCUSDT",
        "1h",
        candles,
        exchange="bybit",
        market_type="spot",
    )


def arguments(database: Path, grid: Path) -> list[str]:
    return [
        "optimize",
        "--exchange",
        "bybit",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1h",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-06",
        "--database",
        str(database),
        "--strategy",
        "trend",
        "--grid-json",
        str(grid),
        "--min-trades",
        "0",
        "--max-combinations",
        "1",
    ]


def test_optimize_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["optimize", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--max-combinations" in output
    assert "--walk-forward-folds" in output
    assert "--output-json" in output


def test_optimize_uses_local_data_and_writes_json_and_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self) -> None:
            raise AssertionError("optimization must not use HTTP")

    monkeypatch.setattr(cli_module, "BinanceSpotClient", UnexpectedClient)
    monkeypatch.setattr(cli_module, "BybitSpotClient", UnexpectedClient)
    database = tmp_path / "market.db"
    grid = tmp_path / "grid.json"
    json_output = tmp_path / "reports" / "optimization.json"
    csv_output = tmp_path / "reports" / "optimization.csv"
    save(database, make_candles(120))
    grid.write_text(
        json.dumps({"trend_following": {"buy_rsi_min": ["50"]}}),
        encoding="utf-8",
    )

    exit_code = main([
        *arguments(database, grid),
        "--output-json",
        str(json_output),
        "--output-csv",
        str(csv_output),
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "CryptoLab Strategy Optimization Research" in output
    assert "Selected configuration (chosen without holdout)" in output
    assert json.loads(json_output.read_text())["dataset"]["candle_count"] == 120
    assert "trend_following,1,true" in csv_output.read_text()


def test_optimize_rejects_invalid_split_as_argument_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main([
            "optimize",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-06",
            "--database",
            str(tmp_path / "missing.db"),
            "--train-fraction",
            "0.8",
            "--validation-fraction",
            "0.2",
        ])

    assert error.value.code == 2


def test_optimize_rejects_incomplete_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "market.db"
    grid = tmp_path / "grid.json"
    candles = make_candles(120)
    save(database, candles[:60] + candles[61:])
    grid.write_text(
        json.dumps({"trend_following": {"buy_rsi_min": ["50"]}}),
        encoding="utf-8",
    )

    assert main(arguments(database, grid)) == 1
    assert "missing=1" in capsys.readouterr().err


def test_optimize_rejects_grid_over_global_limit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "market.db"
    grid = tmp_path / "grid.json"
    save(database, make_candles(120))
    grid.write_text(
        json.dumps({"trend_following": {"buy_rsi_min": ["45", "50"]}}),
        encoding="utf-8",
    )

    assert main(arguments(database, grid)) == 1
    assert "limit is 1" in capsys.readouterr().err
