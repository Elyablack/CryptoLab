# Paper Bot Stage 4: multi-symbol portfolio and reconciliation

## Scope and safety boundary

Stage 4 adds an optional `multi` mode to the existing public-data paper
service. It operates a single Bybit Spot portfolio containing `BTCUSDT`,
`ETHUSDT`, and shared virtual `USDT`. It never authenticates to Bybit and
never creates, changes, or cancels a real exchange order.

The trading rule is the existing, unchanged Relative Momentum Rotation:

- 30 calendar days (720 hourly candles) of momentum;
- review every seven days (168 hourly candles);
- hold the asset with the strongest positive momentum;
- hold USDT when both momentum values are non-positive.

No strategy parameter is exposed for optimization in paper mode.

## Architecture

```text
Bybit public candles/tickers
          │
          ▼
BybitPaperMarketSource
  ├─ exact BTCUSDT closed range
  └─ exact ETHUSDT closed range
          │ aligned timestamps only
          ▼
MultiSymbolPaperTrader
  ├─ FeatureBuilder (one history per symbol)
  ├─ RelativeMomentumRotationPlanner (unchanged rules)
  ├─ PaperRiskManager (portfolio equity/exposure/cash)
  ├─ PaperPositionSizer
  ├─ PaperExecutionSimulator
  │    └─ SimulatedExecutionAdapter / OrderLifecycle
  ├─ PortfolioAccounting (shared USDT, FIFO lots, fees, PnL)
  └─ PaperStateRepository schema v3
       ├─ synchronized candle journal
       ├─ execution snapshot and reservations
       ├─ portfolio checkpoint and pending rotation
       └─ reconciliation history
```

Stage 4 is an orchestration layer. It does not add a second backtest engine,
accounting implementation, or order lifecycle. Confirmed simulated fills are
the only input to the existing `PortfolioAccounting` service.

## Synchronized data rule

A portfolio candle event always contains both symbols with identical
`open_time`, `close_time`, and `1h` interval. Before a pair is committed, each
range must be complete, consecutive, closed, and valid. A delayed or missing
leg rejects the entire range; neither symbol advances its checkpoint.

The existing market source checks local SQLite quality first and uses the
bounded-retry public collector only to repair missing coverage. Invalid or
conflicting stored OHLCV is rejected rather than overwritten.

Startup history rebuilds features but cannot create retroactive paper orders.
Only the newest synchronized, sufficiently fresh set may create a new
rotation. BTC is never evaluated with a newer close than ETH, or vice versa.

## Rotation execution

Every target change is persisted as `PendingRotation` and progresses through
explicit `sell` and `buy` phases:

1. sell the currently held asset;
2. apply the actual confirmed fill to FIFO accounting;
3. persist the resulting cash, remaining lots, lifecycle, and pending phase;
4. only when the old base balance is zero, size and submit the target BUY;
5. use only settled shared USDT for that BUY.

Thus BTC → ETH and ETH → BTC always execute SELL before BUY. A transition to
USDT has no BUY leg. If a SELL is partial, the remaining old position is kept,
the new asset is not bought, and the pending rotation survives restart. An
open partial order also blocks a competing order while its reservation is
active. A terminal cancelled remainder releases only the lifecycle
reservation; it does not remove the unfilled position.

The fake venue continues to use the configured fee, adverse slippage,
quantity step, minimum quantity/notional, latency timestamp, and deterministic
partial-fill fraction. Public ticker observations are not an order-book or a
claim of fillability.

## Reconciliation

Reconciliation runs at creation/recovery, after state changes, every polling
cycle, and for `paper status`/`paper integrity`. It checks:

- unique and internally valid orders and fills;
- order reservations against fills and terminal states;
- reserved USDT/base amounts against available balances;
- shared cash, BTC and ETH balances;
- FIFO lots, fees, and realized PnL rebuilt from the immutable fill journal;
- marked unrealized PnL when a position is open;
- one-active-asset Rotation invariant;
- synchronized processed-candle counts and timestamps;
- rotation target and actual holdings;
- checkpoint sequence and persistent candle journal.

The financially independent comparison available to an offline simulator is
the execution fill journal versus a newly constructed
`PortfolioAccounting`. Serializing and restoring the same adapter checkpoint
is only a round-trip integrity check; it is not described as independent
venue reconciliation because Stage 4 has no external account.

Any mismatch is stored in the checkpoint and reconciliation journal, marks the
session unreconciled, and blocks all new order intents. `paper integrity`
returns a failure rather than silently repairing financial state.

## Persistence and recovery

Paper-state schema version 3 adds only new `paper_portfolio_*` tables. The
v2-to-v3 migration is transactional, updates the schema marker for legacy
single-symbol sessions, and preserves every Stage 1–3 event, decision, order,
fill, reservation, accounting row, and checkpoint. Existing databases are not
rebuilt.

Each synchronized BTC/ETH set is inserted atomically with the portfolio
checkpoint. Execution-only revisions are also atomic. In a BTC → ETH crash
after the SELL commit, recovery reconstructs FIFO accounting from durable
fills, restores the pending `buy` phase and reservations, reconciles, and only
then considers the BUY. Deterministic order/fill IDs and the existing
lifecycle prevent duplicate fill application.

The checkpoint restores:

- shared USDT and fill-derived BTC/ETH lots;
- orders, fills, and active reservations;
- realized PnL and fees;
- Rotation target and pending phase;
- risk counters and manual pause;
- latest mark prices;
- last synchronized candle set.

## Portfolio risk

The existing `PaperRiskManager` receives shared cash, total portfolio equity,
active reservations, open-order count, and the one active position. It applies
maximum order/position notional, total exposure, minimum cash, daily loss,
portfolio drawdown, data freshness, and durable manual pause before creating
an intent. Risk-reducing SELL remains allowed under ordinary limits and pause;
unknown/unreconciled execution state blocks every new order.

## CLI

Old commands remain single-symbol by default:

```bash
cryptolab paper run-once --session-id btc-trend --strategy trend
```

Start or inspect the shared portfolio explicitly:

```bash
cryptolab paper run-once \
  --mode multi \
  --session-id btc-eth-rotation \
  --strategy rotation

cryptolab paper run \
  --mode multi \
  --session-id btc-eth-rotation \
  --strategy rotation

cryptolab paper status \
  --mode multi \
  --session-id btc-eth-rotation \
  --strategy rotation

cryptolab paper integrity \
  --mode multi \
  --session-id btc-eth-rotation \
  --strategy rotation

cryptolab paper pause \
  --mode multi \
  --session-id btc-eth-rotation

cryptolab paper resume \
  --mode multi \
  --session-id btc-eth-rotation
```

`--portfolio-warmup-candles` defaults to 721 so the first live-eligible review
has the fixed 720-hour lookback. The existing `--warmup-candles` option and all
single-symbol defaults are unchanged. Financial and execution options must be
identical when recovering an existing session; a mismatch fails closed.

## Limitations

- one process and one SQLite writer are assumed;
- only Bybit Spot, BTCUSDT + ETHUSDT, USDT quote, and 1h are supported;
- only the existing fixed Relative Momentum Rotation is available in `multi`;
- public REST polling is not websocket market data;
- the simulator has no independent exchange account, order book, testnet, or
  private reconciliation endpoint;
- active partial remainders are restored but no future fill is fabricated;
- no leverage, shorting, live orders, credentials, notifications, or Stage 5
  deployment functionality is included.

## Verification

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src
git diff --check
```
