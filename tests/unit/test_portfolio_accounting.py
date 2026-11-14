from decimal import Decimal

import pytest

from cryptolab.backtesting.accounting import PortfolioAccounting
from cryptolab.execution import Instrument


INSTRUMENT = Instrument(
    exchange="bybit",
    market_type="spot",
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
)


def test_fifo_scale_in_and_partial_scale_out_reconcile() -> None:
    accounting = PortfolioAccounting(Decimal("1000"))
    first = accounting.record_buy(
        fill_id="buy-1",
        instrument=INSTRUMENT,
        quantity=Decimal("2"),
        execution_price=Decimal("100"),
        quote_notional=Decimal("200"),
        fee=Decimal("2"),
        executed_at=1,
    )
    second = accounting.record_buy(
        fill_id="buy-2",
        instrument=INSTRUMENT,
        quantity=Decimal("3"),
        execution_price=Decimal("110"),
        quote_notional=Decimal("330"),
        fee=Decimal("3"),
        executed_at=2,
    )

    realizations = accounting.record_sell(
        fill_id="sell-1",
        instrument=INSTRUMENT,
        quantity=Decimal("4"),
        execution_price=Decimal("120"),
        gross_proceeds=Decimal("480"),
        fee=Decimal("4.8"),
        executed_at=3,
    )

    assert first.original_cost_basis == Decimal("202")
    assert second.original_cost_basis == Decimal("333")
    assert [item.lot_id for item in realizations] == [
        "lot:buy-1",
        "lot:buy-2",
    ]
    assert [item.quantity for item in realizations] == [
        Decimal("2"),
        Decimal("2"),
    ]
    assert [item.cost_basis for item in realizations] == [
        Decimal("202"),
        Decimal("222"),
    ]
    assert [item.entry_fee for item in realizations] == [
        Decimal("2"),
        Decimal("2"),
    ]
    assert [item.exit_fee for item in realizations] == [
        Decimal("2.4"),
        Decimal("2.4"),
    ]
    assert accounting.base_balance(INSTRUMENT) == Decimal("1")
    assert accounting.cost_basis(INSTRUMENT) == Decimal("111")
    assert accounting.quote_balance == Decimal("940.2")
    assert accounting.realized_pnl == Decimal("51.2")
    assert accounting.unrealized_pnl(
        {INSTRUMENT: Decimal("120")}
    ) == Decimal("9")
    assert accounting.equity(
        {INSTRUMENT: Decimal("120")}
    ) == Decimal("1060.2")
    assert (
        accounting.equity({INSTRUMENT: Decimal("120")})
        - accounting.initial_quote_balance
        == accounting.realized_pnl
        + accounting.unrealized_pnl({INSTRUMENT: Decimal("120")})
    )
    assert accounting.cycles[0].is_open
    accounting.assert_invariants()


def test_final_partial_sell_closes_cycle_without_artificial_close() -> None:
    accounting = PortfolioAccounting(Decimal("1000"))
    accounting.record_buy(
        fill_id="buy-1",
        instrument=INSTRUMENT,
        quantity=Decimal("2"),
        execution_price=Decimal("100"),
        quote_notional=Decimal("200"),
        fee=Decimal("0"),
        executed_at=1,
    )
    accounting.record_sell(
        fill_id="sell-1",
        instrument=INSTRUMENT,
        quantity=Decimal("0.5"),
        execution_price=Decimal("110"),
        gross_proceeds=Decimal("55"),
        fee=Decimal("0"),
        executed_at=2,
    )
    assert accounting.cycles[0].is_open

    accounting.record_sell(
        fill_id="sell-2",
        instrument=INSTRUMENT,
        quantity=Decimal("1.5"),
        execution_price=Decimal("120"),
        gross_proceeds=Decimal("180"),
        fee=Decimal("0"),
        executed_at=3,
    )

    cycle = accounting.cycles[0]
    assert not cycle.is_open
    assert cycle.closed_at == 3
    assert cycle.entry_fill_ids == ("buy-1",)
    assert cycle.exit_fill_ids == ("sell-1", "sell-2")
    assert cycle.realized_pnl == Decimal("35")
    assert accounting.base_balance(INSTRUMENT) == 0
    assert accounting.cost_basis(INSTRUMENT) == 0


def test_duplicate_and_overfill_are_rejected_without_state_change() -> None:
    accounting = PortfolioAccounting(Decimal("1000"))
    accounting.record_buy(
        fill_id="buy-1",
        instrument=INSTRUMENT,
        quantity=Decimal("1"),
        execution_price=Decimal("100"),
        quote_notional=Decimal("100"),
        fee=Decimal("0"),
        executed_at=1,
    )

    with pytest.raises(ValueError, match="already processed"):
        accounting.record_buy(
            fill_id="buy-1",
            instrument=INSTRUMENT,
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            quote_notional=Decimal("100"),
            fee=Decimal("0"),
            executed_at=1,
        )
    with pytest.raises(ValueError, match="exceeds open position"):
        accounting.record_sell(
            fill_id="sell-1",
            instrument=INSTRUMENT,
            quantity=Decimal("2"),
            execution_price=Decimal("100"),
            gross_proceeds=Decimal("200"),
            fee=Decimal("0"),
            executed_at=2,
        )

    assert accounting.quote_balance == Decimal("900")
    assert accounting.base_balance(INSTRUMENT) == Decimal("1")
    assert accounting.realizations == ()
