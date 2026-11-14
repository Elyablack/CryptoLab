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

`BacktestEngine.run_portfolio()` requires identical timestamp grids. The CLI
uses the primary `--symbol` plus repeatable `--portfolio-symbol`; it rejects
`--strategy all` and requires `--position-size-fraction` in this mode. Events at
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

## Incremental historical replay

`BacktestExecutionSession` exposes the normal strategy execution kernel as an
incremental stateful boundary. `DeterministicReplaySession` owns the historical
clock and event order, performs sizing followed by the replay risk gate, and
then delegates accepted decisions to that boundary. It does not copy fee,
slippage, lifecycle, reservation, or FIFO formulas. The resulting standard
`BacktestReport` is exactly comparable with `BacktestEngine.run()`.

Stage 1 supports only normal next-open strategy decisions. Stop and timed-exit
models remain batch-only because they require explicit current-candle path
events; an incremental session rejects those configurations instead of
changing their semantics implicitly.

## Persistent replay recovery

`PersistentReplayRunner` stores each complete replay candle boundary in a
separate versioned SQLite database. The input event, decisions, intents,
orders, fills, reservations, adapter snapshot, FIFO/accounting snapshot, risk
state, and checkpoint share one transaction. A pre-commit failure invalidates
the current process handle; recovery starts from the last committed candle.

Recovery does not assign stored balances directly. It reprocesses the durable
candle stream through the normal replay, execution adapter, lifecycle, and
portfolio accounting services, then compares the reconstructed objects with
the persisted state. Any mismatch fails closed. Identical event retries are
no-ops, while conflicting payloads using an existing ID are integrity errors.
