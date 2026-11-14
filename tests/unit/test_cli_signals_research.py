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
        price = Decimal("100") + Decimal(index % 7) / 10
        result.append(Candle(
            open_time=open_time,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("10") + index,
            close_time=open_time + HOUR - 1,
            quote_volume=price * (Decimal("10") + index),
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


def arguments(database: Path) -> list[str]:
    return [
        "signals-research",
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
        "--bootstrap-samples",
        "10",
        "--minimum-independent-events",
        "2",
    ]


def test_signals_research_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["signals-research", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--random-seed" in output
    assert "--bootstrap-samples" in output
    assert "--output-json" in output


def test_signals_research_uses_only_local_data_and_exports_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self) -> None:
            raise AssertionError("signal research must not use HTTP")

    monkeypatch.setattr(cli_module, "BinanceSpotClient", UnexpectedClient)
    monkeypatch.setattr(cli_module, "BybitSpotClient", UnexpectedClient)
    database = tmp_path / "market.db"
    json_path = tmp_path / "reports" / "study.json"
    csv_path = tmp_path / "reports" / "study.csv"
    save(database, make_candles(120))

    exit_code = main([
        *arguments(database),
        "--output-json",
        str(json_path),
        "--output-csv",
        str(csv_path),
    ])

    assert exit_code == 0
    assert "CryptoLab Signal Research & Event Study" in capsys.readouterr().out
    payload = json.loads(json_path.read_text())
    assert payload["entry_model"]["entry"] == "next_candle_open"
    assert payload["dataset"]["candle_count"] == 120
    assert "horizon_summary" in csv_path.read_text()


def test_signals_research_rejects_incomplete_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "market.db"
    candles = make_candles(120)
    save(database, candles[:50] + candles[51:])

    assert main(arguments(database)) == 1
    assert "missing=1" in capsys.readouterr().err


def test_signals_research_rejects_invalid_split_as_argument_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main([
            *arguments(tmp_path / "missing.db"),
            "--train-fraction",
            "0.9",
            "--validation-fraction",
            "0.1",
        ])

    assert error.value.code == 2
