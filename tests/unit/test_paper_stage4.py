import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from cryptolab.cli import main
from cryptolab.execution.adapter import SimulatedExecutionAdapter
from cryptolab.market_data.models import Candle
from cryptolab.paper.models import (
    MultiSymbolPaperBotConfig,
    MultiSymbolPaperCycleResult,
    PaperExecutionConfig,
    PaperRiskConfig,
)
from cryptolab.paper.portfolio_service import MultiSymbolPaperTrader
from cryptolab.replay.models import PriceObservation
from cryptolab.replay.serialization import serialize_execution_snapshot
from cryptolab.strategies.relative_momentum import (
    RelativeMomentumRotationPlanner,
)


HOUR = 3_600_000


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def candles(prices: list[str]) -> list[Candle]:
    result = []
    for index, raw in enumerate(prices):
        price = Decimal(raw)
        result.append(Candle(
            open_time=index * HOUR,
            open=price,
            high=price + Decimal("2"),
            low=price - Decimal("2"),
            close=price,
            volume=Decimal("10"),
            close_time=(index + 1) * HOUR - 1,
            quote_volume=price * Decimal("10"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return result


class FakePortfolioMarket:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.data = {
            "BTCUSDT": candles([
                "100", "110", "120", "130", "110", "100", "95", "90"
            ]),
            "ETHUSDT": candles([
                "100", "90", "80", "70", "100", "140", "110", "90"
            ]),
        }
        self.omit_last_for: str | None = None
        self.shift_for: str | None = None

    def load_closed_candles(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        *,
        received_at: int,
    ) -> list[Candle]:
        del interval, received_at
        result = [
            item
            for item in self.data[symbol]
            if start_time <= item.open_time < end_time
        ]
        if self.omit_last_for == symbol and result:
            result = result[:-1]
        if self.shift_for == symbol and result:
            first = result[0]
            result[0] = Candle(
                open_time=first.open_time + HOUR,
                open=first.open,
                high=first.high,
                low=first.low,
                close=first.close,
                volume=first.volume,
                close_time=first.close_time + HOUR,
                quote_volume=first.quote_volume,
                number_of_trades=None,
                taker_buy_base_volume=None,
                taker_buy_quote_volume=None,
            )
        return result

    def observe_price(self, symbol: str) -> PriceObservation:
        index = self.clock.now // HOUR - 1
        return PriceObservation(
            price=self.data[symbol][index].close,
            observed_at=self.clock.now,
            source="stage4-fake-public-price",
        )


def make_trader(
    tmp_path: Path,
    clock: Clock,
    market: FakePortfolioMarket,
    *,
    execution: PaperExecutionConfig | None = None,
    risk: PaperRiskConfig | None = None,
    after_sell_checkpoint=None,
) -> MultiSymbolPaperTrader:
    return MultiSymbolPaperTrader(
        MultiSymbolPaperBotConfig(
            session_id="rotation-paper",
            market_database=tmp_path / "market.db",
            state_database=tmp_path / "state.db",
            warmup_candles=4,
            poll_seconds=1,
        ),
        execution_config=execution,
        risk_config=risk or PaperRiskConfig(
            data_freshness_limit_ms=HOUR,
            maximum_daily_loss=Decimal("0.99"),
            maximum_portfolio_drawdown=Decimal("0.99"),
        ),
        market_source=market,
        now_ms=clock,
        rotation_planner=RelativeMomentumRotationPlanner(
            lookback_candles=3,
            rebalance_candles=2,
        ),
        after_sell_checkpoint=after_sell_checkpoint,
    )


def test_aligned_sets_buy_btc_and_restore_exact_shared_portfolio(
    tmp_path: Path,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(tmp_path, clock, market)

    result = trader.run_once()
    before = trader.status()
    recovered = make_trader(tmp_path, clock, market).status()

    assert result.processed_candle_sets == 4
    assert result.backfilled_candle_sets == 3
    assert result.live_candle_sets == 1
    assert result.rotation_target == "BTCUSDT"
    assert before.btc_balance > 0
    assert before.eth_balance == 0
    assert before.quote_balance >= 0
    assert recovered.quote_balance == before.quote_balance
    assert recovered.btc_balance == before.btc_balance
    assert recovered.fees_paid == before.fees_paid
    assert recovered.reconciled is True


@pytest.mark.parametrize("problem", ["delayed", "missing", "misaligned"])
def test_incomplete_or_misaligned_set_is_not_checkpointed(
    tmp_path: Path,
    problem: str,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    if problem == "delayed":
        market.omit_last_for = "ETHUSDT"
    elif problem == "missing":
        market.omit_last_for = "BTCUSDT"
    else:
        market.shift_for = "ETHUSDT"
    trader = make_trader(tmp_path, clock, market)

    with pytest.raises(ValueError, match="incomplete|boundary|misaligned"):
        trader.run_once()

    stored = trader.repository.load_portfolio_session("rotation-paper")
    assert stored.candle_sequence == 0
    assert stored.candles == ()


def test_rotation_btc_to_eth_sells_before_buying(tmp_path: Path) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(tmp_path, clock, market)
    trader.run_once()
    clock.now = 6 * HOUR

    result = trader.run_once()
    status = trader.status()
    snapshot = trader.repository.load_portfolio_session("rotation-paper")

    assert result.rotation_target == "ETHUSDT"
    assert status.btc_balance == 0
    assert status.eth_balance > 0
    assert status.quote_balance >= 0
    assert status.fill_count == 3
    execution = snapshot.execution_json
    assert execution.index('"side":"SELL"') < execution.rindex(
        '"side":"BUY"'
    )


def test_rotation_to_cash_has_no_replacement_buy(tmp_path: Path) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(tmp_path, clock, market)
    trader.run_once()
    clock.now = 6 * HOUR
    trader.run_once()
    clock.now = 8 * HOUR

    result = trader.run_once()
    status = trader.status()

    assert result.rotation_target == "USDT"
    assert status.btc_balance == 0
    assert status.eth_balance == 0
    assert status.fill_count == 4


def test_rotation_eth_to_btc_preserves_one_active_asset(tmp_path: Path) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    # Preserve the intended timestamps when replacing the final prices.
    for index, raw in ((6, "180"), (7, "200")):
        price = Decimal(raw)
        market.data["BTCUSDT"][index] = Candle(
            index * HOUR,
            price,
            price + 2,
            price - 2,
            price,
            Decimal("10"),
            (index + 1) * HOUR - 1,
            price * 10,
            None,
            None,
            None,
        )
    trader = make_trader(tmp_path, clock, market)
    trader.run_once()
    clock.now = 6 * HOUR
    trader.run_once()
    clock.now = 8 * HOUR

    result = trader.run_once()
    status = trader.status()

    assert result.rotation_target == "BTCUSDT"
    assert status.btc_balance > 0
    assert status.eth_balance == 0
    assert status.fill_count == 5


def test_partial_sell_keeps_pending_and_never_buys_second_asset(
    tmp_path: Path,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    execution = PaperExecutionConfig(partial_fill_fraction=Decimal("0.5"))
    trader = make_trader(tmp_path, clock, market, execution=execution)
    trader.run_once()
    clock.now = 6 * HOUR

    result = trader.run_once()
    status = trader.status()

    assert result.pending_target == "ETHUSDT"
    assert status.btc_balance > 0
    assert status.eth_balance == 0
    assert status.pending_phase == "sell"


def test_crash_after_sell_recovers_pending_buy_without_duplicate_sell(
    tmp_path: Path,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    initial = make_trader(tmp_path, clock, market)
    initial.run_once()
    clock.now = 6 * HOUR

    def crash() -> None:
        raise RuntimeError("simulated crash after durable SELL")

    crashing = make_trader(
        tmp_path,
        clock,
        market,
        after_sell_checkpoint=crash,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.run_once()
    after_sell = crashing.repository.load_portfolio_session("rotation-paper")
    assert '"phase":"buy"' in after_sell.checkpoint_json

    recovered = make_trader(tmp_path, clock, market)
    result = recovered.run_once()
    status = recovered.status()

    assert result.pending_target is None
    assert status.rotation_target == "ETHUSDT"
    assert status.btc_balance == 0
    assert status.eth_balance > 0
    assert status.fill_count == 3


def test_rejected_buy_remains_pending_and_cash_non_negative(
    tmp_path: Path,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(
        tmp_path,
        clock,
        market,
        risk=PaperRiskConfig(
            maximum_order_notional=Decimal("10"),
            data_freshness_limit_ms=HOUR,
        ),
    )

    result = trader.run_once()
    status = trader.status()

    assert result.pending_target == "BTCUSDT"
    assert status.fill_count == 0
    assert status.quote_balance == Decimal("1000")
    assert status.btc_balance == 0


def test_manual_pause_and_total_exposure_block_new_buy(tmp_path: Path) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    paused = make_trader(tmp_path, clock, market)
    paused.start()
    paused.repository.set_portfolio_trading_paused("rotation-paper", True)
    paused_result = paused.run_once()
    assert paused_result.pending_target == "BTCUSDT"
    assert paused.status().fill_count == 0

    other = tmp_path / "exposure"
    other.mkdir()
    limited = make_trader(
        other,
        clock,
        market,
        risk=PaperRiskConfig(
            maximum_total_exposure=Decimal("0.5"),
            data_freshness_limit_ms=HOUR,
        ),
    )
    limited.run_once()
    assert limited.status().fill_count == 0


def test_tampered_checkpoint_fails_reconciliation_and_persists_diagnostic(
    tmp_path: Path,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(tmp_path, clock, market)
    trader.run_once()
    empty_execution = serialize_execution_snapshot(
        SimulatedExecutionAdapter().snapshot()
    )
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute(
            "UPDATE paper_portfolio_checkpoints SET execution_json = ? "
            "WHERE session_id = 'rotation-paper'",
            (empty_execution,),
        )

    recovered = make_trader(tmp_path, clock, market)
    status = recovered.status()

    assert status.reconciled is False
    assert any(
        "stored quote_balance" in item
        for item in status.reconciliation_diagnostics
    )
    stored = recovered.repository.load_portfolio_session("rotation-paper")
    assert stored.reconciled is False
    restarted = make_trader(tmp_path, clock, market).status()
    assert restarted.reconciled is False
    assert restarted.reconciliation_diagnostics


def test_conflicting_persisted_ohlcv_is_detected_by_journal_digest(
    tmp_path: Path,
) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(tmp_path, clock, market)
    trader.run_once()
    with sqlite3.connect(tmp_path / "state.db") as connection:
        payload = connection.execute(
            "SELECT payload FROM paper_portfolio_candles "
            "WHERE session_id = 'rotation-paper' AND symbol = 'BTCUSDT' "
            "AND open_time = 0"
        ).fetchone()[0]
        connection.execute(
            "UPDATE paper_portfolio_candles SET payload = ? "
            "WHERE session_id = 'rotation-paper' AND symbol = 'BTCUSDT' "
            "AND open_time = 0",
            (payload.replace('"close":"100"', '"close":"101"'),),
        )

    status = make_trader(tmp_path, clock, market).status()

    assert status.reconciled is False
    assert any(
        "journal digest" in item
        for item in status.reconciliation_diagnostics
    )


def test_schema_v2_migration_preserves_legacy_tables(tmp_path: Path) -> None:
    clock = Clock(4 * HOUR)
    market = FakePortfolioMarket(clock)
    trader = make_trader(tmp_path, clock, market)
    trader.start()
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        for table in (
            "paper_portfolio_reconciliations",
            "paper_portfolio_controls",
            "paper_portfolio_checkpoints",
            "paper_portfolio_candles",
            "paper_portfolio_sessions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 2")

    from cryptolab.storage.paper_state_repository import PaperStateRepository

    repository = PaperStateRepository(database)
    repository.init_schema()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {
            item[0]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "paper_sessions" in tables
    assert "paper_portfolio_sessions" in tables


def test_multi_mode_cli_dispatch_and_single_mode_default(
    monkeypatch,
    capsys,
) -> None:
    called: list[str] = []

    class FakePortfolioTrader:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            called.append("multi")

        def run_once(self) -> MultiSymbolPaperCycleResult:
            return MultiSymbolPaperCycleResult(
                session_id="portfolio-cli",
                checked_at=HOUR,
                range_start=None,
                range_end=None,
                processed_candle_sets=0,
                backfilled_candle_sets=0,
                live_candle_sets=0,
                fills_created=0,
                rotation_target="USDT",
                pending_target=None,
                trading_paused=False,
                reconciled=True,
                message="no synchronized set",
            )

    monkeypatch.setattr(
        "cryptolab.cli.MultiSymbolPaperTrader", FakePortfolioTrader
    )

    assert main([
        "paper",
        "run-once",
        "--session-id",
        "portfolio-cli",
        "--mode",
        "multi",
        "--strategy",
        "rotation",
    ]) == 0
    assert called == ["multi"]
    assert "reconciled=true" in capsys.readouterr().out

    assert main([
        "paper",
        "run-once",
        "--session-id",
        "invalid-cli",
        "--mode",
        "multi",
        "--strategy",
        "trend",
    ]) == 1
    assert "requires --strategy rotation" in capsys.readouterr().err
