"""FIFO portfolio accounting shared by deterministic execution paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from cryptolab.execution.models import Instrument


ZERO = Decimal("0")


def _require_positive_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal")


def _require_non_negative_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")


@dataclass(frozen=True)
class PositionLot:
    """Remaining FIFO cost basis created by one BUY fill."""

    lot_id: str
    instrument: Instrument
    entry_fill_id: str
    opened_at: int
    entry_price: Decimal
    original_quantity: Decimal
    remaining_quantity: Decimal
    original_cost_basis: Decimal
    remaining_cost_basis: Decimal
    entry_fee: Decimal
    remaining_entry_fee: Decimal

    def __post_init__(self) -> None:
        for name in ("lot_id", "entry_fill_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if type(self.opened_at) is not int or self.opened_at < 0:
            raise ValueError("opened_at must be a non-negative integer")
        _require_positive_decimal("entry_price", self.entry_price)
        _require_positive_decimal("original_quantity", self.original_quantity)
        _require_non_negative_decimal(
            "remaining_quantity", self.remaining_quantity
        )
        _require_positive_decimal(
            "original_cost_basis", self.original_cost_basis
        )
        _require_non_negative_decimal(
            "remaining_cost_basis", self.remaining_cost_basis
        )
        _require_non_negative_decimal("entry_fee", self.entry_fee)
        _require_non_negative_decimal(
            "remaining_entry_fee", self.remaining_entry_fee
        )
        if self.remaining_quantity > self.original_quantity:
            raise ValueError("remaining_quantity cannot exceed original_quantity")
        if self.remaining_cost_basis > self.original_cost_basis:
            raise ValueError(
                "remaining_cost_basis cannot exceed original_cost_basis"
            )
        if self.remaining_entry_fee > self.entry_fee:
            raise ValueError(
                "remaining_entry_fee cannot exceed entry_fee"
            )
        if (self.remaining_quantity == 0) != (
            self.remaining_cost_basis == 0
        ):
            raise ValueError(
                "A closed lot must have zero quantity and zero cost basis"
            )


@dataclass(frozen=True)
class LotRealization:
    """One FIFO portion consumed by one SELL fill."""

    realization_id: str
    lot_id: str
    entry_fill_id: str
    exit_fill_id: str
    instrument: Instrument
    quantity: Decimal
    cost_basis: Decimal
    entry_fee: Decimal
    gross_proceeds: Decimal
    exit_fee: Decimal
    net_proceeds: Decimal
    realized_pnl: Decimal
    realized_at: int

    def __post_init__(self) -> None:
        for name in (
            "realization_id",
            "lot_id",
            "entry_fill_id",
            "exit_fill_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        _require_positive_decimal("quantity", self.quantity)
        _require_positive_decimal("cost_basis", self.cost_basis)
        _require_non_negative_decimal("entry_fee", self.entry_fee)
        _require_positive_decimal("gross_proceeds", self.gross_proceeds)
        _require_non_negative_decimal("exit_fee", self.exit_fee)
        _require_non_negative_decimal("net_proceeds", self.net_proceeds)
        if not isinstance(self.realized_pnl, Decimal) or not self.realized_pnl.is_finite():
            raise ValueError("realized_pnl must be a finite Decimal")
        if type(self.realized_at) is not int or self.realized_at < 0:
            raise ValueError("realized_at must be a non-negative integer")
        if self.net_proceeds != self.gross_proceeds - self.exit_fee:
            raise ValueError("net_proceeds must equal gross proceeds minus fee")
        if self.realized_pnl != self.net_proceeds - self.cost_basis:
            raise ValueError("realized_pnl must reconcile with cost and proceeds")


@dataclass(frozen=True)
class PositionCycle:
    """One flat-to-flat position lifecycle, possibly still open."""

    cycle_id: str
    instrument: Instrument
    opened_at: int
    closed_at: int | None
    entry_fill_ids: tuple[str, ...]
    exit_fill_ids: tuple[str, ...]
    realization_ids: tuple[str, ...]
    realized_pnl: Decimal

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass
class _CycleBuilder:
    cycle_id: str
    instrument: Instrument
    opened_at: int
    closed_at: int | None = None
    entry_fill_ids: list[str] = field(default_factory=list)
    exit_fill_ids: list[str] = field(default_factory=list)
    realization_ids: list[str] = field(default_factory=list)
    realized_pnl: Decimal = ZERO

    def snapshot(self) -> PositionCycle:
        return PositionCycle(
            cycle_id=self.cycle_id,
            instrument=self.instrument,
            opened_at=self.opened_at,
            closed_at=self.closed_at,
            entry_fill_ids=tuple(self.entry_fill_ids),
            exit_fill_ids=tuple(self.exit_fill_ids),
            realization_ids=tuple(self.realization_ids),
            realized_pnl=self.realized_pnl,
        )


class PortfolioAccounting:
    """Mutable accounting service with immutable auditable outputs.

    Entry fees are part of FIFO lot cost basis. Exit fees reduce the net
    proceeds allocated to each realized lot portion. No market or strategy
    decisions are made here.
    """

    def __init__(self, initial_quote_balance: Decimal) -> None:
        _require_positive_decimal(
            "initial_quote_balance", initial_quote_balance
        )
        self._initial_quote_balance = initial_quote_balance
        self._quote_balance = initial_quote_balance
        self._lots: dict[Instrument, list[PositionLot]] = {}
        self._realizations: list[LotRealization] = []
        self._cycles: list[_CycleBuilder] = []
        self._active_cycles: dict[Instrument, _CycleBuilder] = {}
        self._processed_fill_ids: set[str] = set()
        self._realized_pnl = ZERO
        self._fees_paid = ZERO

    @property
    def initial_quote_balance(self) -> Decimal:
        return self._initial_quote_balance

    @property
    def quote_balance(self) -> Decimal:
        return self._quote_balance

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def fees_paid(self) -> Decimal:
        return self._fees_paid

    @property
    def realizations(self) -> tuple[LotRealization, ...]:
        return tuple(self._realizations)

    @property
    def cycles(self) -> tuple[PositionCycle, ...]:
        return tuple(cycle.snapshot() for cycle in self._cycles)

    def lots(self, instrument: Instrument) -> tuple[PositionLot, ...]:
        return tuple(self._lots.get(instrument, ()))

    def all_lots(self) -> tuple[PositionLot, ...]:
        return tuple(
            lot
            for instrument in sorted(self._lots, key=_instrument_key)
            for lot in self._lots[instrument]
        )

    def base_balance(self, instrument: Instrument) -> Decimal:
        return sum(
            (lot.remaining_quantity for lot in self._lots.get(instrument, ())),
            ZERO,
        )

    def cost_basis(self, instrument: Instrument) -> Decimal:
        return sum(
            (lot.remaining_cost_basis for lot in self._lots.get(instrument, ())),
            ZERO,
        )

    def record_buy(
        self,
        *,
        fill_id: str,
        instrument: Instrument,
        quantity: Decimal,
        execution_price: Decimal,
        quote_notional: Decimal,
        fee: Decimal,
        executed_at: int,
    ) -> PositionLot:
        self._validate_new_fill(fill_id, instrument, executed_at)
        _require_positive_decimal("quantity", quantity)
        _require_positive_decimal("execution_price", execution_price)
        _require_positive_decimal("quote_notional", quote_notional)
        _require_non_negative_decimal("fee", fee)
        if not _decimal_close(
            quote_notional,
            quantity * execution_price,
        ):
            raise ValueError(
                "BUY quote_notional must equal quantity times execution price"
            )
        total_cost = quote_notional + fee
        if total_cost > self._quote_balance:
            raise ValueError("BUY cost exceeds available quote balance")

        was_flat = self.base_balance(instrument) == 0
        lot = PositionLot(
            lot_id=f"lot:{fill_id}",
            instrument=instrument,
            entry_fill_id=fill_id,
            opened_at=executed_at,
            entry_price=execution_price,
            original_quantity=quantity,
            remaining_quantity=quantity,
            original_cost_basis=total_cost,
            remaining_cost_basis=total_cost,
            entry_fee=fee,
            remaining_entry_fee=fee,
        )
        self._quote_balance -= total_cost
        self._fees_paid += fee
        self._lots.setdefault(instrument, []).append(lot)
        self._processed_fill_ids.add(fill_id)

        if was_flat:
            cycle = _CycleBuilder(
                cycle_id=(
                    f"cycle:{_instrument_key(instrument)}:"
                    f"{executed_at}:{len(self._cycles) + 1}"
                ),
                instrument=instrument,
                opened_at=executed_at,
            )
            self._cycles.append(cycle)
            self._active_cycles[instrument] = cycle
        self._active_cycles[instrument].entry_fill_ids.append(fill_id)
        self.assert_invariants()
        return lot

    def record_sell(
        self,
        *,
        fill_id: str,
        instrument: Instrument,
        quantity: Decimal,
        execution_price: Decimal,
        gross_proceeds: Decimal,
        fee: Decimal,
        executed_at: int,
    ) -> tuple[LotRealization, ...]:
        self._validate_new_fill(fill_id, instrument, executed_at)
        _require_positive_decimal("quantity", quantity)
        _require_positive_decimal("execution_price", execution_price)
        _require_positive_decimal("gross_proceeds", gross_proceeds)
        _require_non_negative_decimal("fee", fee)
        if not _decimal_close(
            gross_proceeds,
            quantity * execution_price,
        ):
            raise ValueError(
                "SELL gross_proceeds must equal quantity times execution price"
            )
        available = self.base_balance(instrument)
        if quantity > available:
            raise ValueError("SELL quantity exceeds open position")
        if instrument not in self._active_cycles:
            raise RuntimeError("SELL has no active position cycle")

        remaining_to_sell = quantity
        remaining_gross = gross_proceeds
        remaining_fee = fee
        lots = self._lots[instrument]
        updated_lots: list[PositionLot] = []
        realizations: list[LotRealization] = []

        for lot in lots:
            if remaining_to_sell == 0:
                updated_lots.append(lot)
                continue
            consumed = min(lot.remaining_quantity, remaining_to_sell)
            is_last_sell_piece = consumed == remaining_to_sell
            is_full_lot = consumed == lot.remaining_quantity

            if is_full_lot:
                cost_basis = lot.remaining_cost_basis
                entry_fee_piece = lot.remaining_entry_fee
            else:
                cost_basis = (
                    lot.remaining_cost_basis
                    * consumed
                    / lot.remaining_quantity
                )
                entry_fee_piece = (
                    lot.remaining_entry_fee
                    * consumed
                    / lot.remaining_quantity
                )
            if is_last_sell_piece:
                gross_piece = remaining_gross
                fee_piece = remaining_fee
            else:
                fraction = consumed / quantity
                gross_piece = gross_proceeds * fraction
                fee_piece = fee * fraction
            net_piece = gross_piece - fee_piece
            realization = LotRealization(
                realization_id=(
                    f"realization:{fill_id}:{len(realizations) + 1}"
                ),
                lot_id=lot.lot_id,
                entry_fill_id=lot.entry_fill_id,
                exit_fill_id=fill_id,
                instrument=instrument,
                quantity=consumed,
                cost_basis=cost_basis,
                entry_fee=entry_fee_piece,
                gross_proceeds=gross_piece,
                exit_fee=fee_piece,
                net_proceeds=net_piece,
                realized_pnl=net_piece - cost_basis,
                realized_at=executed_at,
            )
            realizations.append(realization)
            remaining_to_sell -= consumed
            remaining_gross -= gross_piece
            remaining_fee -= fee_piece

            leftover_quantity = lot.remaining_quantity - consumed
            leftover_cost = lot.remaining_cost_basis - cost_basis
            leftover_entry_fee = (
                lot.remaining_entry_fee - entry_fee_piece
            )
            if leftover_quantity > 0:
                updated_lots.append(PositionLot(
                    lot_id=lot.lot_id,
                    instrument=lot.instrument,
                    entry_fill_id=lot.entry_fill_id,
                    opened_at=lot.opened_at,
                    entry_price=lot.entry_price,
                    original_quantity=lot.original_quantity,
                    remaining_quantity=leftover_quantity,
                    original_cost_basis=lot.original_cost_basis,
                    remaining_cost_basis=leftover_cost,
                    entry_fee=lot.entry_fee,
                    remaining_entry_fee=leftover_entry_fee,
                ))

        if remaining_to_sell != 0:
            raise RuntimeError("FIFO accounting did not consume SELL quantity")
        self._lots[instrument] = updated_lots
        self._quote_balance += gross_proceeds - fee
        self._fees_paid += fee
        self._realized_pnl += sum(
            (item.realized_pnl for item in realizations), ZERO
        )
        self._realizations.extend(realizations)
        self._processed_fill_ids.add(fill_id)

        cycle = self._active_cycles[instrument]
        cycle.exit_fill_ids.append(fill_id)
        cycle.realization_ids.extend(
            item.realization_id for item in realizations
        )
        cycle.realized_pnl += sum(
            (item.realized_pnl for item in realizations), ZERO
        )
        if self.base_balance(instrument) == 0:
            cycle.closed_at = executed_at
            del self._active_cycles[instrument]
        self.assert_invariants()
        return tuple(realizations)

    def equity(self, mark_prices: Mapping[Instrument, Decimal]) -> Decimal:
        equity = self._quote_balance
        for instrument, lots in self._lots.items():
            if not lots:
                continue
            if instrument not in mark_prices:
                raise ValueError(
                    f"Missing mark price for {instrument.symbol}"
                )
            mark = mark_prices[instrument]
            _require_positive_decimal("mark price", mark)
            equity += self.base_balance(instrument) * mark
        return equity

    def unrealized_pnl(
        self,
        mark_prices: Mapping[Instrument, Decimal],
    ) -> Decimal:
        result = ZERO
        for instrument, lots in self._lots.items():
            if not lots:
                continue
            if instrument not in mark_prices:
                raise ValueError(
                    f"Missing mark price for {instrument.symbol}"
                )
            mark = mark_prices[instrument]
            _require_positive_decimal("mark price", mark)
            result += (
                self.base_balance(instrument) * mark
                - self.cost_basis(instrument)
            )
        return result

    def assert_invariants(self) -> None:
        if self._quote_balance < 0:
            raise RuntimeError("Quote balance cannot be negative")
        if self._fees_paid < 0:
            raise RuntimeError("Fees paid cannot be negative")
        seen_lots: set[str] = set()
        for instrument, lots in self._lots.items():
            quantity = ZERO
            cost = ZERO
            for lot in lots:
                if lot.instrument != instrument:
                    raise RuntimeError("Lot stored under the wrong instrument")
                if lot.lot_id in seen_lots:
                    raise RuntimeError("Lot identifiers must be unique")
                seen_lots.add(lot.lot_id)
                quantity += lot.remaining_quantity
                cost += lot.remaining_cost_basis
            if quantity < 0 or cost < 0:
                raise RuntimeError("Position quantity and cost must be non-negative")
            if (quantity == 0) != (cost == 0):
                raise RuntimeError("Position quantity and cost basis diverged")
        realized = sum(
            (item.realized_pnl for item in self._realizations), ZERO
        )
        if realized != self._realized_pnl:
            raise RuntimeError("Realized PnL does not match lot realizations")
        realization_ids = [item.realization_id for item in self._realizations]
        if len(realization_ids) != len(set(realization_ids)):
            raise RuntimeError("Lot realization identifiers must be unique")

    def _validate_new_fill(
        self,
        fill_id: str,
        instrument: Instrument,
        executed_at: int,
    ) -> None:
        if not isinstance(fill_id, str) or not fill_id.strip():
            raise ValueError("fill_id must be a non-empty string")
        if fill_id in self._processed_fill_ids:
            raise ValueError(f"Fill already processed: {fill_id}")
        if not isinstance(instrument, Instrument):
            raise ValueError("instrument must be an Instrument")
        if type(executed_at) is not int or executed_at < 0:
            raise ValueError("executed_at must be a non-negative integer")


def _instrument_key(instrument: Instrument) -> str:
    return ":".join((
        instrument.exchange,
        instrument.market_type,
        instrument.symbol,
    ))


def _decimal_close(left: Decimal, right: Decimal) -> bool:
    """Allow only arithmetic-context rounding, never material mismatches."""
    scale = max(Decimal("1"), abs(left), abs(right))
    return abs(left - right) <= scale * Decimal("1e-24")
