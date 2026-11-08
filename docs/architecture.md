# CryptoLab v1.0 Architecture

## Scope

CryptoLab is one local Python CLI application for historical public Spot market
research. It contains no authenticated API, real order execution, portfolio
service, scheduler, web service, queue, or background worker.

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

`BacktestEngine` evaluates one strategy and one independent virtual long-only
portfolio. It executes signals at the next open, applies Decimal fees and
slippage, retains an equity curve, and asserts accounting invariants. Optional
trailing-stop fills share the normal SELL settlement path but carry separate
audit metadata rather than fabricated strategy timestamps.

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

Adding live execution requires a separate architecture and security project; it
is not an extension point of the current Strategy Engine.

## Known limitations

- no automatic exchange listing-date discovery;
- no historical conflict auto-resolution;
- no multi-writer SQLite coordination;
- no scheduler or monitoring service;
- no machine-readable CLI output yet;
- backtests do not model order books or exchange quantity filters.
