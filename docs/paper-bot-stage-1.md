# Paper Bot Stage 1: deterministic event replay

## Scope

Stage 1 is an offline, single-process replay of already available historical
Spot candles. It is an infrastructure bridge between the batch backtester and
a future paper-trading loop. It does not connect to a venue, use credentials,
poll for candles, persist recovery state, or submit real orders.

The implementation deliberately does not create another execution or
accounting engine. `DeterministicReplaySession` controls time and event order;
accepted decisions are executed by a stateful wrapper around the same
`BacktestEngine` execution kernel, `SimulatedExecutionAdapter`,
`OrderLifecycle`, and `PortfolioAccounting` used by normal backtests.

## Event-loop architecture

```text
historical Candle
       │
       ▼
DeterministicReplaySession ───────────── controlled replay clock
       │
       ├─ candle open
       │    └─ pending decision from the previous closed candle
       │         ├─ BacktestExecutionSession.prepare_sizing()
       │         │    └─ existing PositionSizer
       │         ├─ ReplayRiskPolicy.check()
       │         └─ BacktestExecutionSession.execute()
       │              └─ BacktestEngine._execute_decision()
       │                   ├─ OrderIntent
       │                   ├─ SimulatedExecutionAdapter
       │                   ├─ OrderLifecycle → Order → Fill
       │                   └─ PortfolioAccounting → FIFO lots/PnL/fees
       │
       └─ candle close
            ├─ market-data validation
            ├─ FeatureBuilder (known candle prefix only)
            ├─ StrategyRunner.evaluate_latest()
            ├─ new immutable StrategyDecision
            └─ pending until the next candle open

Every open and close phase ends with an immutable portfolio snapshot.
```

The conceptual path for one actionable decision is therefore:

```text
Closed Candle → Feature Update → Strategy Evaluation → StrategyDecision
→ Position Sizing → Risk Check → OrderIntent → OrderLifecycle
→ Simulated Fill → PortfolioAccounting → Portfolio Snapshot
```

The boundary between the two lines is the next candle open. Sizing, risk
checking, intent creation, and fill processing never happen at the signal
candle close.

## Components

### New replay components

- `DeterministicReplayRunner` is the convenience API and session factory.
- `DeterministicReplaySession` accepts one candle at a time, rejects gaps and
  conflicting duplicates, and owns the audit event sequence.
- `DeterministicReplayClock` advances only to explicitly supplied historical
  timestamps and cannot move backwards.
- `ReplayEvent` and `ReplayEventType` form the immutable ordered audit trail.
- `ReplayPortfolioSnapshot` records cash, base quantity, equity, realized PnL,
  and derived unrealized PnL at a controlled timestamp.
- `DeterministicReplayReport` combines replay-specific events with the normal
  `BacktestReport` used for exact compatibility checks.
- `ReplayRiskPolicy` is the narrow Stage 1 risk boundary. The default
  `AllowAllReplayRiskPolicy` adds no new trading restriction and therefore
  preserves batch-backtest behavior.

### Reused components

- `FeatureBuilder` validates each known historical prefix and calculates the
  existing indicators without future candles.
- `StrategyRunner.evaluate_latest()` applies the existing warm-up and decision
  validation rules to only the newest feature row.
- `BacktestExecutionSession` owns state for incremental execution but delegates
  sizing, pricing, fees, slippage, intent creation, fills, and settlement to
  existing `BacktestEngine` code.
- Existing `PositionSizer` implementations retain their exact allocation
  semantics.
- `SimulatedExecutionAdapter`, `OrderLifecycle`, and `PortfolioAccounting`
  retain their existing order, reservation, fill, FIFO, and PnL semantics.

## Processing order

For each consecutive candle:

1. Validate OHLCV values, interval alignment, exact `close_time`, and
   continuity with the preceding candle.
2. Advance the clock to the candle open.
3. Execute the previous candle's pending decision at this open. Only the open
   price and timestamp are projected into the execution kernel; the current
   candle high, low, close, and volume are not exposed.
4. Mark an after-open portfolio snapshot.
5. Advance the clock to `close_time + 1`, append the now-closed candle, and
   rebuild features from the known prefix.
6. Evaluate only the newest row. Warm-up produces the same explicit HOLD as
   batch `StrategyRunner` evaluation.
7. Store the decision for the next open, mark close equity, and record a close
   snapshot.
8. On finalization, keep the last actionable decision unexecuted when no next
   candle exists and report the standard skipped-decision reason.

A repeated candle event with the same deterministic ID and identical payload
returns its cached result without a second decision, intent, order, fill, or
accounting mutation. The same ID with a different payload fails loudly.

## Time and execution rules

- Candle timestamps are Unix milliseconds and must be interval-aligned.
- `close_time` is inclusive. A candle becomes usable at `close_time + 1`.
- A decision timestamp is its candle open; its availability timestamp is
  `close_time + 1`.
- A BUY or SELL decision is eligible only at the next consecutive candle open.
- Fee and adverse slippage use the existing `BacktestConfig` and
  `BacktestEngine` formulas.
- HOLD produces no order intent.
- A final actionable decision remains unexecuted; the replay never invents a
  terminal fill.
- The default capital is the existing `BacktestConfig` default of 1,000 USDT.

Stage 1 incremental execution rejects trailing-stop, protective-stop, and
time-exit configurations. Those models inspect the current candle path and
remain in the existing batch loop until their event semantics are explicitly
integrated. Rejecting them is safer than silently producing results that differ
from `BacktestEngine.run()`.

## Financial and event invariants

- Quote and base balances never become negative.
- One strategy source event creates at most one intent.
- Intent IDs, fill IDs, and replay event IDs are unique.
- Duplicate identical candle events are idempotent; conflicting duplicates are
  errors.
- Applied fills reconcile one-to-one with intents and filled lifecycle orders.
- No filled order retains a balance reservation.
- Base quantity and remaining cost basis reconcile with FIFO lots.
- Realized PnL equals completed-trade PnL.
- Fill fees equal portfolio fees paid.
- Final equity equals the last close-marked equity point.
- Total PnL equals both final equity minus initial cash and realized plus
  unrealized PnL.

`BacktestExecutionSession.finalize()` calls the same accounting invariant
checks as the batch engine.

## Reproduction

There is no Stage 1 CLI command. Replay is a Python API so no existing CLI
surface changes.

```python
from cryptolab.replay import DeterministicReplayRunner

report = DeterministicReplayRunner().run(
    strategy,
    "BTCUSDT",
    "1h",
    candles,
    exchange="bybit",
)

print(report.backtest.final_equity)
print(report.events)
```

Run the focused compatibility tests:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/unit/test_deterministic_replay.py \
  tests/unit/test_strategy_runner.py
```

At implementation time, the focused suite passed 19 tests and the complete
suite passed 470 tests. The release-wide verification commands are:

```bash
python -m pytest -q -p no:cacheprovider
ruff check .
python -m compileall -q src
git diff --check
```

## Backtest compatibility

The reference test constructs features once for `BacktestEngine.run()` and
feeds the same candles sequentially to replay. It compares the complete
`BacktestReport` with exact dataclass equality, including decisions, intents,
orders, execution fills, portfolio fills, FIFO results, skipped decisions,
balances, fees, realized/unrealized PnL, drawdown, and final equity. The test is
run for both legacy all-in/all-out and fixed-fraction position sizing. No
tolerance or float conversion is used.

## Stage 1 limitations

- one process, one exchange identity, one Spot symbol, and one strategy;
- historical closed candles supplied by the caller only;
- no networking, polling, scheduler, authentication, or venue order API;
- no durable event log, restart recovery, or reconciliation after process
  termination;
- no multi-symbol/shared-cash replay;
- no trailing, fixed protective, or timed-exit replay;
- the feature prefix is rebuilt for clarity and parity, so long streams have
  quadratic feature-calculation work;
- the risk policy is only an extension point, not a production risk manager.

## Stage 2 handoff

Stage 1 itself contains no durable state. The separately implemented Stage 2
wrapper adds a versioned event store, atomic order/accounting checkpoints, and
explicit restart reconciliation without changing this in-memory API. See
`docs/paper-bot-stage-2.md`. A controlled network market-data source and
incremental indicator state remain outside both stages.
