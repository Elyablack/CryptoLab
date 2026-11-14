# Backtesting and execution architecture

CryptoLab keeps research signals, allocation, execution state, and financial
accounting separate:

```text
StrategyDecision (quantity-free BUY/SELL/HOLD)
        │
        ▼
PositionSizer + current portfolio/open price
        │
        ▼
OrderIntent (instrument, side, base quantity, earliest execution)
        │
        ▼
ExecutionAdapter → OrderLifecycle → Order / one-or-more Fill values
        │
        ▼
PortfolioAccounting → PositionLot → LotRealization → PositionCycle
        │
        ▼
equity, realized/unrealized PnL, fees, drawdown, reports
```

## Compatibility modes

- `LegacyAllInAllOutSizer` is the default and preserves the original
  single-entry all-in/all-out results.
- `FixedFractionPositionSizer` retains quote cash but still rejects scale-in
  and exits completely.
- `FixedFractionScaleSizer` explicitly permits repeated entries and fractional
  exits. It is a Python API policy, not a strategy rule.

All entry fees belong to FIFO lot cost basis. Exit fees reduce the proceeds of
the realization that consumed the lot. Any residual position remains open at
the end and is marked at the last close; it is never closed artificially.

## Trailing stop with lots

The high watermark belongs to the aggregate position cycle. A scale-in or
partial strategy SELL does not reset it. A pending strategy SELL executes at
the candle open first. If quantity remains, the same candle may subsequently
cross the already-active stop and close only that remainder. A full strategy
SELL prevents a second stop execution.

## Shared-cash portfolios

`BacktestEngine.run_portfolio()` requires identical timestamp grids. Events at
one open are sorted by exchange, market type, symbol, and strategy name. Each
event sees the quote balance left by earlier events, so constrained-capital
allocation is deterministic and explicit. Open equity is the shared quote cash
plus every position valued at its current candle close.

## Order lifecycle and adapters

Orders transition from `NEW` to `PARTIALLY_FILLED` or `FILLED`, or to one of
the terminal `CANCELLED`, `REJECTED`, and `EXPIRED` states. Reservations prevent
several active orders from claiming the same venue/market asset balance.
Terminal transitions release any remainder.

`SimulatedExecutionAdapter` is used by backtests. `FakeVenueExecutionAdapter`
accepts explicitly identified offline fill events for unit/integration tests.
Snapshots contain intents, orders, fills, reservations, and ordered events;
restoration validates their consistency. Reconciliation reports differences
without silently modifying either side.

These adapters contain no HTTP client, authentication, API keys, or exchange
order submission. They are not live or testnet trading implementations.
