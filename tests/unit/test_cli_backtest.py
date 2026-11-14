from decimal import Decimal
from dataclasses import replace
from pathlib import Path

import pytest

import cryptolab.cli as cli_module
from cryptolab.cli import main
from cryptolab.market_data.models import Candle
from cryptolab.storage.sqlite_repository import SQLiteRepository
from cryptolab.strategies.base import make_decision
from cryptolab.strategies.models import Signal


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


def save_bybit_candles(
    database_path: Path,
    candles: list[Candle],
    *,
    symbol: str = "BTCUSDT",
) -> None:
    repository = SQLiteRepository(database_path)
    repository.init_schema()
    repository.save_candles(
        symbol,
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
    assert "--show-trades" in output
    assert "--trailing-stop-rate" in output
    assert "--position-size-fraction" in output
    assert "--position-sell-fraction" in output
    assert "--portfolio-symbol" in output
    assert "--show-accounting" in output
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
    assert "Trailing stop:" not in output


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


def test_backtest_show_trades_enables_optional_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend(["--strategy", "breakout", "--show-trades"])

    exit_code = main(arguments)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Completed trade details" in output
    assert "No completed trades." in output


def test_backtest_accepts_optional_trailing_stop_rate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--strategy",
        "trend",
        "--trailing-stop-rate",
        "0.10",
    ])

    exit_code = main(arguments)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Trailing stop:    10.0000%" in output
    assert "Trailing exits:   0" in output


def test_backtest_accepts_position_size_fraction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--strategy",
        "trend",
        "--position-size-fraction",
        "0.25",
    ])

    exit_code = main(arguments)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert (
        "Position sizing:  fixed_fraction(buy_fraction=0.25)"
        in output
    )


def test_backtest_accepts_scale_in_and_scale_out_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--strategy",
        "trend",
        "--position-size-fraction",
        "0.25",
        "--position-sell-fraction",
        "0.5",
    ])

    exit_code = main(arguments)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert (
        "Position sizing:  "
        "fixed_fraction_scale(buy_fraction=0.25,sell_fraction=0.5)"
        in output
    )


def test_backtest_show_accounting_is_opt_in(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend(["--strategy", "trend", "--show-accounting"])

    assert main(arguments) == 0

    output = capsys.readouterr().out
    assert "Accounting details" in output
    assert "Open FIFO lots" in output
    assert "Lot realizations" in output
    assert "Position cycles" in output


def test_backtest_runs_shared_multi_symbol_portfolio_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self) -> None:
            raise AssertionError("portfolio backtest must not use HTTP")

    class AlwaysBuyStrategy:
        name = "always_buy_test"
        minimum_history = 1

        def evaluate(self, symbol, interval, history):
            return make_decision(
                strategy_name=self.name,
                symbol=symbol,
                interval=interval,
                row=history[-1],
                signal=Signal.BUY,
                explanation="deterministic CLI portfolio test",
            )

    monkeypatch.setattr(cli_module, "BinanceSpotClient", UnexpectedClient)
    monkeypatch.setattr(cli_module, "BybitSpotClient", UnexpectedClient)
    monkeypatch.setattr(
        cli_module,
        "_create_strategies",
        lambda strategy_name: [AlwaysBuyStrategy()],
    )
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    save_bybit_candles(database_path, candles)
    save_bybit_candles(database_path, candles, symbol="ETHUSDT")
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--portfolio-symbol",
        "ETHUSDT",
        "--strategy",
        "trend",
        "--position-size-fraction",
        "0.5",
        "--show-accounting",
    ])

    exit_code = main(arguments)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.count("CryptoLab Shared Portfolio Backtest") == 1
    assert "symbols=BTCUSDT,ETHUSDT" in output
    assert "Symbols:          BTCUSDT, ETHUSDT" in output
    assert "Quote balance:" in output
    assert "Event ordering:" in output
    assert "Accounting details" in output
    assert "BTCUSDT lot:" in output
    assert "ETHUSDT lot:" in output
    assert "BTCUSDT cycle:" in output
    assert "ETHUSDT cycle:" in output
    assert "CryptoLab Backtest\n" not in output


def test_portfolio_backtest_validates_every_symbol(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    save_bybit_candles(database_path, make_hourly_candles(72))
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--portfolio-symbol",
        "ETHUSDT",
        "--strategy",
        "trend",
        "--position-size-fraction",
        "0.5",
    ])

    exit_code = main(arguments)

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "invalid historical data for ETHUSDT" in error
    assert "72 candles are missing" in error


def test_portfolio_backtest_rejects_invalid_secondary_candle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "market_data.db"
    candles = make_hourly_candles(72)
    save_bybit_candles(database_path, candles)
    invalid = list(candles)
    invalid[10] = replace(invalid[10], high=Decimal("99"))
    save_bybit_candles(database_path, invalid, symbol="ETHUSDT")
    arguments = backtest_arguments(database_path)
    arguments.extend([
        "--portfolio-symbol",
        "ETHUSDT",
        "--strategy",
        "trend",
        "--position-size-fraction",
        "0.5",
    ])

    exit_code = main(arguments)

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "invalid historical data for ETHUSDT" in error
    assert "1 candles are invalid" in error


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


@pytest.mark.parametrize(
    "trailing_stop_rate",
    ["invalid", "0", "-0.1", "1", "NaN", "Infinity"],
)
def test_backtest_rejects_invalid_trailing_stop_rate(
    trailing_stop_rate: str,
) -> None:
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
            "--trailing-stop-rate",
            trailing_stop_rate,
        ])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "fraction",
    ["invalid", "0", "-0.1", "1.1", "NaN", "Infinity"],
)
def test_backtest_rejects_invalid_position_size_fraction(
    fraction: str,
) -> None:
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
            "--position-size-fraction",
            fraction,
        ])

    assert error.value.code == 2


def test_backtest_rejects_sell_fraction_without_buy_fraction() -> None:
    with pytest.raises(SystemExit) as error:
        main([
            "backtest",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-04",
            "--position-sell-fraction",
            "0.5",
        ])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "extra_arguments",
    [
        [
            "--portfolio-symbol",
            "ETHUSDT",
            "--strategy",
            "trend",
        ],
        [
            "--portfolio-symbol",
            "ETHUSDT",
            "--position-size-fraction",
            "0.5",
        ],
        [
            "--portfolio-symbol",
            "BTCUSDT",
            "--strategy",
            "trend",
            "--position-size-fraction",
            "0.5",
        ],
    ],
)
def test_backtest_rejects_ambiguous_portfolio_arguments(
    extra_arguments: list[str],
) -> None:
    arguments = [
        "backtest",
        "--symbol",
        "BTCUSDT",
        "--interval",
        "1h",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-04",
        *extra_arguments,
    ]

    with pytest.raises(SystemExit) as error:
        main(arguments)

    assert error.value.code == 2
