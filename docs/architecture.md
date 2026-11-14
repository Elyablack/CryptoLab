# CryptoLab v1.0 Architecture

## Scope

CryptoLab is one local Python CLI application for historical public Spot market
research. It contains no authenticated API, real order execution, portfolio
daemon, scheduler, web service, queue, or background worker. Execution-domain
objects and offline adapters are simulations only.

Supported sources are Binance Spot and Bybit V5 Spot. Supported intervals are
`1m`, `5m`, `15m`, `1h`, `4h`, and `1d`. All ranges are UTC and half-open.

## Component map

```text
CLI
├── collect ── public client ── parser ── validator ── collector ── SQLite
├── quality ─────────────────── DataQualityChecker ──────────────── SQLite
├── analyze ── quality ── MarketAnalyzer ── indicators/returns
├── signals ── quality ── FeatureBuilder ── StrategyRunner
├── backtest ─ quality ── FeatureBuilder ── BacktestEngine/formatter
│                                      ├── PositionSizer
│                                      ├── OrderIntent → Order → Fill
│                                      ├── FIFO PortfolioAccounting
│                                      └── optional trailing stop
└── research ─ quality ── StrategyResearchRunner
                         ├── three independent BacktestEngine runs
                         ├── buy-and-hold and cash-only
                         └── signal/trade diagnostics
```

### Market data

- `client.py`: Binance public klines.
- `bybit_client.py`: Bybit V5 public Spot klines.
- `http.py`: shared retry and HTTP failure classification.
- `parser.py`: exchange payloads to immutable `Candle` values.
- `validator.py`: numeric, OHLCV, sequence, timestamp, and closed checks.
- `collector.py`: pagination, unfinished-candle exclusion, persistence, and
  final coverage verification.
- `data_quality.py`: exact persisted-range validation.
- `intervals.py` and `markets.py`: supported domain identities.

### Storage

`SQLiteRepository` owns schema creation, migration, source-isolated reads, and
atomic page writes. It uses only parameterized SQL and opens short-lived
connections per operation.

### Analytics and features

The Analytics Layer calculates simple/log returns, nonannualized sample
volatility, close-to-close maximum drawdown, SMA, EMA, Wilder RSI/ATR, and RVOL.
`FeatureBuilder` aligns all values to candles and preserves warm-up `None`
values. A feature becomes available at `close_time + 1`.

### Strategies

Strategies implement one read-only interface and receive history views ending
at the current row. Decisions include the signal candle, availability time,
reference price, explanation, and immutable diagnostics. Strategy code has no
access to exchange clients or portfolio state.

### Backtesting

`BacktestEngine.run()` evaluates one strategy and one independent virtual
long-only portfolio. `run_portfolio()` evaluates aligned instrument streams
against a shared quote balance. The CLI exposes the latter through repeatable
`--portfolio-symbol`, one explicit `--strategy`, and mandatory fractional
capital allocation. It executes signals at the next open, applies
Decimal fees and slippage, retains an equity curve, and asserts accounting
invariants. Optional trailing-stop fills share the normal SELL settlement path
and use risk-source intents without fabricated strategy timestamps.

Strategy decisions remain quantity-free. At an eligible next-open event a
`PositionSizer` approves either a quote budget for BUY or a base quantity for
SELL. The default `LegacyAllInAllOutSizer` reproduces the original behavior;
`FixedFractionPositionSizer` can retain part of the quote cash. An approved
strategy action becomes an immutable `OrderIntent` with deterministic source
and intent identifiers. `FixedFractionScaleSizer` enables explicit partial
allocation without changing StrategyDecision.

`PortfolioAccounting` owns FIFO lots, partial realizations, fees, cost basis,
position cycles, and realized/unrealized reconciliation. Scale-in adds a lot;
scale-out consumes lots oldest first. The legacy sizer still rejects repeated
BUY while holding and closes the full position, preserving original results.

`OrderLifecycle` owns order status transitions and balance reservations. One
order may receive multiple unique fills but cannot overfill. Duplicate fill
events are idempotent only when their payload is identical. The backtest uses
`SimulatedExecutionAdapter`, which immediately fills an intent at the modeled
price. `FakeVenueExecutionAdapter` adds offline external-event simulation,
restart snapshots, and state reconciliation. Neither adapter performs I/O.

### Research

`StrategyResearchRunner` runs unchanged strategies, deterministic benchmarks,
and diagnostic condition counts. Generated reports and local databases are
runtime artifacts rather than source fixtures.

## Collection state machine

1. Validate exchange, interval, range ordering, and range alignment.
2. Request a page using the exchange-specific pagination direction.
3. Retry only transient transport, timeout, rate-limit, and 5xx failures.
4. Parse the full payload and reject invalid structures.
5. Validate each candle and its interval timing.
6. Exclude candles whose inclusive `close_time` has not passed.
7. Atomically classify candidates as inserted, identical, or conflicting.
8. Advance by observed candle time and reject non-progressing/out-of-range
   responses.
9. Validate the complete requested grid from SQLite.
10. Return success only for exact, valid persisted coverage without conflicts.

An empty page before reaching the requested boundary is recorded explicitly.
A range before market listing is legitimate no-data, but it remains incomplete
and returns nonzero. CryptoLab has no authoritative listing-date registry and
does not guess or fill the missing prefix.

## HTTP failure policy

The default policy permits three total attempts. Delay is capped exponential
backoff plus additive jitter. Server `Retry-After` and Bybit reset timestamps
are lower bounds.

| Condition | Behavior |
|---|---|
| Timeout/transport failure | Retry, then `RetryExhaustedError` |
| HTTP 429 | Retry respecting server delay |
| HTTP 5xx | Retry |
| Binance HTTP 418 | Immediate stop |
| Other HTTP 4xx | Immediate `HTTPStatusError` |
| Bybit `retCode=10006` | Retry as rate limit |
| Invalid JSON/payload | Immediate data error |

No URL query contains credentials because all supported endpoints are public.

## Collection result semantics

`CollectionResult` contains exchange/market/range identity and exact counters:

- expected interval slots;
- HTTP candles received;
- new, identical, and conflicting persistence classifications;
- missing, invalid, and unexpected final rows;
- excluded unfinished candles and empty page count;
- bounded timestamp previews and a human-readable failure reason.

The model is immutable. `succeeded` is true only when no failure or integrity
issue remains.

## Data integrity

`Candle` holds Decimal prices/volumes, millisecond timestamps, and optional
exchange-specific fields. Validation rejects nonfinite values, bad OHLC
relationships, negative volumes/trades, unaligned opens, incorrect closes,
future/unclosed candles, missing slots, extras, and duplicates.

The database primary key is:

```text
(exchange, market_type, symbol, interval, open_time)
```

Historical conflict policy is detect-and-retain. An existing row never changes
implicitly. Conflicts make the collection unsuccessful and must be reviewed
after a backup. This preserves reproducible research inputs.

## Schema lifecycle

Schema v2 is stored in `PRAGMA user_version`. A new database is created at v2.
An existing v2-shaped database with version zero is verified and adopted.
Unknown newer or partial schemas fail safely.

Legacy Binance-only migration runs under `BEGIN IMMEDIATE`:

1. rename the original table to `candles_legacy_v1_backup`;
2. create the source-aware v2 table;
3. copy every row as `binance/spot`;
4. compare source and destination row counts;
5. set user version and commit.

The backup table is retained. SQLite transaction rollback restores the original
state if any migration step fails.

## Determinism and look-ahead safety

- Candle and decision models are immutable.
- Decimal is used for stored values and portfolio accounting.
- Feature and strategy evaluation accept explicit `as_of_time`.
- Indicators use current and past candles only.
- Breakout thresholds exclude the current candle.
- Strategy histories are read-only prefix views.
- Decisions are available after close and execute at the next open.
- Position sizing uses the portfolio and open price observable at that
  execution event, never the candle's future high, low, or close.
- Backtest intents map one-to-one to filled simulated orders.
- One fill ID is processed at most once; conflicting duplicates fail loudly.
- FIFO lot quantity, remaining cost, fees, and realizations reconcile after
  every accounting event.
- Trailing high updates only after the current candle stop check.

## Operational model

CryptoLab is supported as a local Python package and CLI. SQLite is appropriate
for one local writer; concurrent collectors are unsupported. Human-readable
stdout is stable for research use, while diagnostics use standard `logging` on
stderr. `--verbose` enables tracebacks at command boundaries.

There is intentionally no Docker or service deployment requirement for v1.0.
GitHub Actions verifies Python 3.12/3.13, tests, package installation, CLI,
static checks, dependency audit, and secret scanning.

## Security boundaries

- public endpoints only;
- no private keys, authentication, account data, or orders;
- parameterized SQLite statements;
- TLS verification provided by HTTPX defaults;
- constrained dependencies and automated audit;
- runtime `.env`, database, and report artifacts excluded from source.

The offline `ExecutionAdapter` is only a contract boundary. Adding live
execution still requires a separate architecture and security project; no
current implementation authenticates or communicates with a trading endpoint.

## Known limitations

- no automatic exchange listing-date discovery;
- no historical conflict auto-resolution;
- no multi-writer SQLite coordination;
- no scheduler or monitoring service;
- no machine-readable CLI output yet;
- no authenticated paper/testnet/live venue adapter or durable order store;
- multi-symbol backtests require aligned timelines, one strategy, and a
  common exchange/interval/USDT balance;
- backtests do not model order books or exchange quantity filters.
