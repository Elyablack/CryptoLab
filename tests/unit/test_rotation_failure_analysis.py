from decimal import Decimal
from types import SimpleNamespace

from cryptolab.backtesting import (
    BacktestConfig,
    BacktestEngine,
    PortfolioBacktestInput,
)
from cryptolab.features.models import FeatureRow
from cryptolab.market_data.models import Candle
from cryptolab.research.validation import ResearchPeriod
from cryptolab.strategies import RelativeMomentumRotationPlanner, Signal
from scripts.analyze_rotation_failure import (
    build_episode_records,
    build_validation_decisions,
    calculate_pnl_decomposition,
)


HOUR = 3_600_000


def rows_and_candles(
    prices: list[str],
) -> tuple[tuple[FeatureRow, ...], tuple[Candle, ...]]:
    rows = []
    candles = []
    for index, raw in enumerate(prices):
        price = Decimal(raw)
        open_time = index * HOUR
        high = price + Decimal("1")
        low = price - Decimal("1")
        rows.append(FeatureRow(
            open_time=open_time,
            close_time=open_time + HOUR - 1,
            available_time=open_time + HOUR,
            open=price,
            high=high,
            low=low,
            close=price,
            volume=Decimal("10"),
            simple_return=None,
            log_return=None,
            sma20=None,
            sma50=None,
            ema20=None,
            rsi14=None,
            atr14=None,
            rvol20=None,
        ))
        candles.append(Candle(
            open_time=open_time,
            open=price,
            high=high,
            low=low,
            close=price,
            volume=Decimal("10"),
            close_time=open_time + HOUR - 1,
            quote_volume=Decimal("1000"),
            number_of_trades=None,
            taker_buy_base_volume=None,
            taker_buy_quote_volume=None,
        ))
    return tuple(rows), tuple(candles)


def study_for(
    btc_prices: list[str],
    eth_prices: list[str],
):
    btc_rows, btc_candles = rows_and_candles(btc_prices)
    eth_rows, eth_candles = rows_and_candles(eth_prices)
    planner = RelativeMomentumRotationPlanner(
        lookback_candles=2,
        rebalance_candles=2,
    )
    plan = planner.build(btc_rows, eth_rows)
    report = BacktestEngine(BacktestConfig()).run_portfolio((
        PortfolioBacktestInput(
            strategy=plan.strategy_for("BTCUSDT"),
            symbol="BTCUSDT",
            interval="1h",
            rows=btc_rows,
            exchange="bybit",
        ),
        PortfolioBacktestInput(
            strategy=plan.strategy_for("ETHUSDT"),
            symbol="ETHUSDT",
            interval="1h",
            rows=eth_rows,
            exchange="bybit",
        ),
    ))
    period = ResearchPeriod(
        name="full_range",
        start_index=0,
        end_index=len(btc_rows),
        start_time=0,
        end_time=len(btc_rows) * HOUR,
    )
    period_study = SimpleNamespace(
        period=period,
        primary=SimpleNamespace(report=report, plan=plan),
    )
    maps = {
        "BTCUSDT": {item.open_time: item for item in btc_candles},
        "ETHUSDT": {item.open_time: item for item in eth_candles},
    }
    ranges = (
        ResearchPeriod("development", 0, 4, 0, 4 * HOUR),
        ResearchPeriod("validation", 4, 7, 4 * HOUR, 7 * HOUR),
        ResearchPeriod("studied_test", 7, 8, 7 * HOUR, 8 * HOUR),
    )
    return period_study, maps, ranges


def test_closed_episode_export_reconciles_rotation_accounting() -> None:
    period_study, maps, ranges = study_for(
        ["100", "100", "120", "120", "110", "110", "90", "90"],
        ["100", "100", "110", "110", "130", "130", "100", "100"],
    )

    episodes = build_episode_records(period_study, maps, ranges)
    decisions = build_validation_decisions(period_study)
    decomposition = calculate_pnl_decomposition(period_study, episodes)

    assert len(episodes) == 2
    assert all(item.status == "closed" for item in episodes)
    assert [item.symbol for item in episodes] == ["BTCUSDT", "ETHUSDT"]
    assert episodes[0].crosses_period_boundary
    assert episodes[0].mae < episodes[0].mfe
    assert sum((item.net_pnl for item in episodes), Decimal("0")) == (
        period_study.primary.report.realized_pnl
    )
    assert [item.executed_orders for item in decisions] == [
        "BUY:BTCUSDT",
        "SELL:BTCUSDT;BUY:ETHUSDT",
        "SELL:ETHUSDT",
    ]
    assert all(item.actual_trade for item in decisions)
    assert decomposition.gross_trading_pnl - decomposition.fees == (
        period_study.primary.report.total_pnl
    )


def test_open_episode_is_marked_without_fabricated_exit() -> None:
    period_study, maps, ranges = study_for(
        ["100", "100", "120", "120", "144", "144", "170", "170"],
        ["100", "100", "110", "110", "115", "115", "120", "120"],
    )

    episodes = build_episode_records(period_study, maps, ranges)

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.status == "open"
    assert episode.exit_time_ms is None
    assert episode.net_pnl is None
    assert episode.unrealized_pnl == period_study.primary.report.unrealized_pnl
    assert episode.exit_reason == "open_mark_to_market"
    assert [item.side for item in period_study.primary.report.fills] == [
        Signal.BUY,
    ]
