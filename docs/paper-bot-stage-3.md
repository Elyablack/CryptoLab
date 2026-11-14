# Paper Bot Stage 3: continuous public-data paper trading

## Scope and safety boundary

Stage 3 is a single-process, single-symbol Bybit Spot paper-trading loop for
`BTCUSDT` `1h`. It reads public closed candles and a public ticker, evaluates
one existing strategy, creates only local simulated orders, and persists every
completed candle cycle in SQLite.

It never authenticates to Bybit, reads an exchange account, or submits,
changes, or cancels a real order. `SimulatedExecutionAdapter` and
`PaperExecutionSimulator` are offline models, not exchange integrations.

## Architecture

```text
Bybit public V5 kline/ticker REST
              │
              ▼
BybitPaperMarketSource ── DataQualityChecker ── market_data.db
              │                 │
              │        exact closed-candle range
              ▼
ContinuousPaperTrader
  ├─ recover/create PersistentReplaySession
  ├─ historical gaps → observed replay with trading_enabled=False
  ├─ newest fresh candle → separate public PriceObservation
  ├─ FeatureBuilder → existing StrategyRunner
  ├─ PaperPositionSizer → PaperRiskManager
  ├─ existing Backtest execution kernel
  │    ├─ OrderIntent / OrderLifecycle
  │    ├─ SimulatedExecutionAdapter
  │    └─ PortfolioAccounting / FIFO lots / fees / PnL
  └─ atomic PaperStateRepository checkpoint ── paper_state.db
```

Market history and paper state intentionally use separate SQLite files. One
process and one paper-state writer are assumed.

## Time model

The loop retains four different timestamps:

1. `Candle.open_time` identifies the interval.
2. Inclusive `Candle.close_time` determines when its OHLCV can first be used.
3. `received_at` is the local time at which the process obtained and validated
   the closed range.
4. `PriceObservation.observed_at` is the local time after receipt at which the
   public `lastPrice` response was observed and the simulated fill may occur.

The strategy decision keeps the feature row's true availability time
(`close_time + 1`). The intent and simulated fill use the later observation
time. A paper decision is never assigned the already-past next-candle open.

On startup or after downtime, every older missing candle is replayed with
`trading_enabled=False`. Those candles rebuild the exact feature prefix and
risk state but actionable decisions are recorded as suppressed backfill; they
cannot create retroactive orders. Only the newest fully covered candle may be
traded, and only while it is within the configured freshness limit.

## Candle acquisition and failure policy

For every cycle the service calculates the last fully closed hourly boundary,
then requests the exact range after the durable checkpoint. The public source:

- checks SQLite coverage before downloading;
- uses the existing bounded-retry Bybit collector only for missing rows;
- rejects invalid OHLCV, timestamp misalignment, incorrect `close_time`,
  conflicts, duplicates, unexpected rows, gaps, and unfinished candles;
- validates exact first/last boundaries and consecutive timestamps before any
  replay event is processed;
- obtains a public V5 ticker only for a fresh live-eligible candle.

A network or ticker failure does not advance the affected candle checkpoint.
The continuous command logs the failure and tries again after the configured
poll interval. A state-integrity failure stops the process fail-closed.

## Paper execution model

Fees and adverse slippage use the unchanged `BacktestEngine` formulas and
`Decimal` accounting. `PaperPositionSizer` adds configurable quantity step,
minimum quantity, and minimum notional checks to an existing `PositionSizer`.
The default remains legacy all-in/all-out; `--position-size-fraction` is an
explicit alternative.

The continuous adapter supports a deterministic immediate fill fraction. A
fraction below one creates a real `PARTIALLY_FILLED` lifecycle state rather
than disguising a partial fill as smaller allocation. By default the unfilled
remainder is cancelled and its reservation released. The
`--keep-partial-order-open` diagnostic option retains the remaining
reservation so restart/integrity recovery can be tested; Stage 3 does not
fabricate a later fill for that remainder. Active orders therefore block new
BUY intents through the risk manager. Existing externally supplied fill event
handling, duplicate-fill idempotency, and recovery remain in
`FakeVenueExecutionAdapter`/`OrderLifecycle`.

Configured latency waits before moving the simulated execution timestamp after
the public observation. It does not query an order book again at that later
instant; the same observed ticker price is used with adverse slippage.
Consequently this remains an explicit model, not a claim of fillability or
real venue behavior.

`PaperExecutionSimulator` exposes the same deterministic full/partial,
cancellation, fee, slippage, lifecycle, and FIFO settlement building blocks
for isolated tests.

## Risk manager

`PaperRiskManager` runs after sizing and before intent creation. Its immutable
configuration contains:

- maximum order notional;
- maximum marked position notional;
- maximum total exposure as an equity fraction;
- minimum remaining available cash;
- maximum UTC-day loss;
- maximum peak-to-current portfolio drawdown;
- maximum closed-candle age;
- durable operator pause.

It also checks active reservations, active orders, positive equity, and whether
execution state is known. Unknown execution state blocks every new order.
Manual pause, loss, drawdown, exposure, cash, and freshness controls block new
BUY orders. A SELL that reduces an existing long remains allowed during those
controls, but it is still blocked when execution state itself is unknown.

Peak equity, daily anchor, counters, and last equity are checkpointed with the
risk policy. Manual pause is stored in `paper_controls`, separately from
deterministic replay state, so an operator action survives restart without
rewriting historical checkpoints.

## Persistence and recovery

Paper-state schema version 2 adds `paper_controls` to the Stage 2 tables. The
v1-to-v2 migration creates this table transactionally, initializes existing
sessions as not paused, updates stored session versions, and preserves every
candle, event, decision, order, fill, reservation, accounting row, and
checkpoint.

Observed replay input stores the candle, actual receipt timestamp, optional
price observation, and `trading_enabled` flag. Recovery restores the initial
risk state, replays those exact inputs, restores order lifecycle snapshots,
and compares all normalized rows and the final checkpoint byte-for-byte at the
canonical JSON level. Configuration changes, corrupt rows, conflicting event
payloads, duplicate fills, or unreconciled state fail closed.

Each candle plus all resulting events, risk state, intent, order, fill,
reservation, FIFO/accounting state, and checkpoint is one `BEGIN IMMEDIATE`
transaction. A retry after an uncertain post-commit crash is an idempotent
event lookup; a pre-commit failure leaves the previous checkpoint active.

## CLI

Run one cycle:

```bash
cryptolab paper run-once \
  --session-id btc-trend-paper \
  --strategy trend \
  --symbol BTCUSDT \
  --interval 1h \
  --market-database data/market_data.db \
  --state-database data/paper_state.db
```

Run continuously and stop cleanly with `SIGINT`/`SIGTERM`:

```bash
cryptolab paper run --session-id btc-trend-paper --strategy trend
```

Inspect, pause, resume, and fully reconstruct the session:

```bash
cryptolab paper status --session-id btc-trend-paper --strategy trend
cryptolab paper pause --session-id btc-trend-paper
cryptolab paper resume --session-id btc-trend-paper
cryptolab paper integrity --session-id btc-trend-paper --strategy trend
```

Recovery commands must use the same strategy, financial assumptions, sizing,
execution constraints, and risk limits used to create the session. A mismatch
is rejected instead of silently changing behavior. `--help` lists every
explicit limit and execution assumption.

## Failure modes and limitations

- Bybit public REST unavailable: retain the last checkpoint and retry later.
- Missing/conflicting/invalid candles: do not process any part of that fetched
  range until exact coverage is available.
- Stale newest candle: replay it for history but do not create a BUY/SELL.
- Public ticker unavailable: do not commit the live-eligible candle, allowing a
  later cycle to retry without losing the decision.
- Corrupt/incompatible paper state: stop fail-closed.
- Operator pause: block BUY, continue data processing, allow risk-reducing
  SELL.

The first release supports one `BTCUSDT` `1h` strategy session, one process,
and one writer. It has no websocket, multi-symbol portfolio, exchange account
reconciliation, private credentials, real/testnet orders, leverage, shorts,
deployment service, or notification integration. Public `lastPrice` is not an
order-book simulation. Open partial remainders are recoverable but receive no
automatically invented later fills.

## Verification

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src
git diff --check
```
