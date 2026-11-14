# Paper Bot Stage 2: persistent state and crash recovery

## Scope

Stage 2 adds a versioned SQLite checkpoint store around the Stage 1
deterministic replay. It remains offline and single-symbol. It does not connect
to an exchange, poll for candles, use credentials, or submit real orders.

The paper-state database is independent from the market-data database. The
caller supplies its path, for example `data/paper_state.db`; CryptoLab never
opens or migrates `data/market_data.db` through this API.

Persistence does not contain another execution or FIFO implementation. During
recovery, committed candles are replayed through `DeterministicReplaySession`,
`BacktestEngine`, `SimulatedExecutionAdapter`, `OrderLifecycle`, and
`PortfolioAccounting`. The reconstructed state is then compared exactly with
every stored checkpoint table.

## API

```python
from cryptolab.replay import PersistentReplayRunner

runner = PersistentReplayRunner("data/paper_state.db")
session = runner.create(
    "btc-hourly-paper-1",
    strategy,
    "BTCUSDT",
    "1h",
    exchange="bybit",
)

session.process_candle(closed_candle)
print(session.current_state)
session.close()

# A new process supplies the same deterministic configuration and strategy.
recovered = PersistentReplayRunner("data/paper_state.db").recover(
    "btc-hourly-paper-1",
    strategy,
    "BTCUSDT",
    "1h",
    exchange="bybit",
)
recovered.process_candle(next_closed_candle)
report = recovered.finish()
```

`close()` closes only the in-process handle. It does not delete or finalize the
durable session. `finish()` derives the normal replay report; terminal report
formatting is reproducible after recovery and is not a separate trading event.

## SQLite schema

Paper-state schema version is stored in `PRAGMA user_version` and is currently
`1`. The serialization format is independently versioned as `1` in each
session row. Unknown versions and unversioned non-empty databases fail closed.

| Table | Responsibility |
|---|---|
| `paper_sessions` | Session ID, schema/serialization versions, immutable configuration, initial risk state, UTC audit times |
| `paper_candle_events` | Stable input event ID, sequence, open time, exact candle payload |
| `paper_replay_events` | Ordered Stage 1 event journal |
| `paper_decisions` | One immutable strategy decision per committed candle |
| `paper_order_intents` | Immutable approved intents |
| `paper_orders` | Current lifecycle status for each order |
| `paper_fills` | Immutable execution fills |
| `paper_reservations` | Current reservation state, including released zero balances |
| `paper_execution_adapter` | Complete typed adapter snapshot: intents, lifecycle, fills, reservations and event sequence |
| `paper_accounting` | Portfolio state, backtest fills, completed trades, FIFO lots, realizations and position cycles |
| `paper_checkpoints` | Last committed candle, full replay snapshot, risk state and replay event count |

All financial `Decimal` values are serialized using exact decimal strings.
They are never converted to JSON or SQLite floating-point numbers. Historical
times remain Unix milliseconds. Storage audit timestamps use timezone-aware
ISO 8601 UTC values.

## Atomic boundary

One fully processed closed candle is the atomic unit.

```text
in-memory Stage 1 processing
        │
        ├─ validate candle / build features / decision
        ├─ execute previous decision at this open
        └─ validate execution/accounting checkpoint
        │
        ▼
SQLite BEGIN IMMEDIATE
        ├─ candle input event
        ├─ replay event journal and decision
        ├─ intents and current orders
        ├─ fills and reservations
        ├─ typed execution-adapter snapshot
        ├─ FIFO/accounting snapshot
        └─ checkpoint written last
SQLite COMMIT
```

The checkpoint cannot advance independently of the order, fill, reservation,
and accounting rows because all writes share the same transaction. Immutable
tables are checked against their existing deterministic prefix; conflicting
IDs are errors rather than updates.

If in-memory processing succeeds but any pre-commit storage step fails, SQLite
rolls the transaction back and the current handle becomes fail-closed. It may
not process another candle. A new handle must be reconstructed from the last
committed database checkpoint. This prevents continued operation from an
uncommitted in-memory portfolio.

If commit succeeds but the process fails before acknowledging the result, the
input event is already durable. Retrying its stable ID after recovery is a
no-op.

## Event identity and idempotency

Default candle IDs retain the Stage 1 identity:

```text
closed-candle:{exchange}:{market_type}:{symbol}:{interval}:{open_time}
```

Rules:

- identical committed event ID and payload returns the cached replay result;
- the same event ID with different content is an integrity error;
- candle sequence and checkpoint sequence must be contiguous;
- decisions and replay events must match the durable prefix exactly;
- intent and fill rows are immutable after insertion;
- order and reservation rows are replaced only inside the same transaction as
  their checkpoint;
- duplicate fills remain idempotent through the existing `OrderLifecycle`;
- a repeated decision cannot create a second intent because deterministic
  source IDs are reconstructed and verified.

Persistent idempotency therefore does not depend on the Stage 1 in-memory
sets or result cache.

## Recovery procedure

1. Validate SQLite schema and serialization versions.
2. Load session identity and compare exchange, market, symbol, interval,
   `BacktestConfig`, strategy configuration, position sizer, and risk-policy
   configuration with the requested recovery process.
3. Restore the risk policy to its persisted initial state.
4. Deserialize committed candles in sequence and process them through a fresh
   Stage 1 session.
5. Recreate orders, fills, reservations, FIFO lots, fees, realized PnL, and
   balances through the existing execution/accounting path.
6. Deserialize the stored adapter snapshot and validate it with
   `SimulatedExecutionAdapter.from_snapshot()`.
7. Compare the reconstructed risk state, full replay checkpoint, event
   journal, decisions, intents, orders, fills, reservations, and accounting
   snapshot with SQLite.
8. Expose the recovered handle only if every comparison succeeds.

No stored balance or lot is silently repaired. Corrupt, missing, extra, or
incompatible data raises `ReplayStateIntegrityError` or
`ReplayConfigurationError`.

## Risk-policy persistence

A risk policy used by persistent replay must implement
`PersistentReplayRiskPolicy`:

- `persistence_configuration()` returns immutable parameters;
- `snapshot_state()` returns the current scalar state;
- `restore_state()` restores an initial scalar state before deterministic
  reconstruction.

The default `AllowAllReplayRiskPolicy` has empty configuration and state.
Mutable risk-policy state is stored at every candle checkpoint and checked
after recovery.

## Invariants

Before persistence and after reconstruction CryptoLab checks:

- quote and base balances are non-negative;
- reserved USDT does not exceed available quote cash;
- every committed candle has one decision;
- candle IDs, replay event IDs, intent IDs, order IDs, and fill IDs are unique;
- replay and candle sequences are contiguous;
- lifecycle orders reconcile with fills and reservations;
- terminal orders have no remaining reservation;
- adapter events retain deterministic sequence numbers;
- fill fees equal portfolio fees paid;
- realized PnL equals completed-trade PnL;
- base quantity and cost basis reconcile with FIFO lots;
- checkpoint sequence equals the durable candle count;
- normalized journal tables equal the full reconstructed checkpoint.

## Crash injection coverage

`CrashPoint` provides deterministic test failures at:

1. before in-memory processing;
2. after processing but before `BEGIN IMMEDIATE`;
3. after the candle event write;
4. after the decision journal;
5. after order intents;
6. after orders;
7. after fills;
8. after reservations;
9. after FIFO/accounting state;
10. before checkpoint update;
11. after commit but before caller acknowledgement.

Every injected crash closes the handle logically. Tests construct a new runner,
recover from SQLite, retry the uncertain candle, finish the replay, and compare
the complete report and portfolio state with an uninterrupted run.

The execution snapshot codec is also tested with an actual partially filled
order and an actual cancelled order. It preserves order status, fill identity,
event order, and remaining/released reservations, and the existing adapter
continues or rejects transitions according to `OrderLifecycle` rules.

## Verification

Focused Stage 2 tests:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/test_persistent_replay.py
```

Full verification:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src
git diff --check
```

At implementation time the Stage 2 file passed 20 tests and the complete suite
passed 490 tests.

## Failure modes

- Unknown, incomplete, or newer schema: refuse to open.
- Existing session ID on create: refuse to overwrite.
- Configuration mismatch: refuse recovery.
- Conflicting duplicate input: fail the handle and retain stored state.
- Storage failure before commit: rollback and require recovery.
- Failure after commit: retry after recovery is a no-op.
- Strategy/risk code producing different historical results: reconstructed
  checkpoint mismatch and fail closed.
- Corrupt order, fill, reservation, event, or accounting row: fail closed.

## Stage 2 limitations

- one local process, exchange identity, Spot symbol, and strategy per session;
- one SQLite writer is assumed; no distributed locking or leader election;
- no network data source, polling, scheduling, venue authentication, or order
  submission;
- no automatic schema migration beyond the initial version;
- persistent strategies and position sizers must have deterministic,
  serializable configuration;
- persistent replay currently requires the standard stateless
  `FeatureBuilder`;
- Stage 1 simulated strategy orders fill completely and immediately. Partial
  and cancelled lifecycle snapshots are losslessly serializable and validated,
  but the primary replay loop does not invent those transitions;
- the terminal `finish()` report is deterministically rebuilt rather than
  stored as a separate durable event;
- feature prefixes retain the Stage 1 recomputation cost;
- no multi-symbol/shared-cash persistent replay;
- no persistent trailing, protective, or timed-exit replay.

Stage 3 is not implemented by this change.
