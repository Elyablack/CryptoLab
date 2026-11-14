# CryptoLab

CryptoLab v1.0 is a local, research-only platform for collecting and
validating public cryptocurrency Spot candles, calculating indicators,
generating deterministic research signals, and backtesting long-only virtual
portfolios.

It supports Binance Spot and Bybit V5 Spot. CryptoLab does not use private API
keys, connect to exchange accounts, or submit real orders.

## Requirements and installation

- Python 3.12 or 3.13;
- SQLite support included with Python;
- internet access only for `collect` and initial package installation.

Create an isolated environment and install the constrained test setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -c requirements/constraints.txt -e ".[test]"
python -m pytest -q -p no:cacheprovider
cryptolab --help
```

For runtime-only installation:

```bash
python -m pip install -c requirements/constraints.txt .
```

The constraints file pins the verified Python 3.12/3.13 dependency set. The
package metadata also places compatible upper bounds on direct dependencies.

## Complete workflow

Collect public Bybit BTCUSDT hourly candles:

```bash
cryptolab collect \
  --exchange bybit \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2025-01-01 \
  --end 2025-02-01 \
  --database data/market_data.db
```

Validate the exact persisted range:

```bash
cryptolab quality \
  --exchange bybit \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2025-01-01 \
  --end 2025-02-01 \
  --database data/market_data.db
```

Then analyze, inspect signals, run independent backtests, and produce the
baseline research report:

```bash
cryptolab analyze  --exchange bybit --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01 --database data/market_data.db
cryptolab signals  --exchange bybit --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01 --database data/market_data.db --strategy all
cryptolab backtest --exchange bybit --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01 --database data/market_data.db --strategy all --show-trades
cryptolab research --exchange bybit --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2025-02-01 --database data/market_data.db --fee-rate 0.001
```

All date ranges use UTC and half-open semantics: `[start, end)`.

## CLI commands

| Command | Purpose | Network access |
|---|---|---|
| `collect` | Download, validate, and persist public Spot candles | Yes |
| `quality` | Validate exact stored coverage and candle integrity | No |
| `analyze` | Calculate returns, volatility, drawdown, and indicators | No |
| `signals` | Inspect historical BUY/SELL research events | No |
| `backtest` | Simulate independent long-only Spot portfolios | No |
| `research` | Compare strategies with buy-and-hold and cash benchmarks | No |

Every command supports `--exchange {binance,bybit}`, `--symbol`, `--interval`,
`--start`, `--end`, and `--database`. Binance remains the default exchange.
Place `--verbose` after the subcommand to include debug diagnostics on stderr.

Supported intervals are `1m`, `5m`, `15m`, `1h`, `4h`, and `1d`.

## Reliable collection behavior

The public clients use explicit timeouts and at most three attempts by default.
Timeouts, transport failures, HTTP 429, and HTTP 5xx use bounded exponential
backoff with additive jitter. `Retry-After` is a minimum delay. Bybit API code
`10006` is treated as a rate limit. Binance HTTP 418 stops immediately to avoid
extending an IP restriction. Other HTTP 4xx responses fail immediately.

`collect` verifies the requested range against SQLite before returning success.
An empty or partial response cannot produce exit code 0 while candles are
missing. Ranges before an exchange listing date are reported as explicit
no-data/incomplete ranges; CryptoLab never invents candles. Adjust `--start` to
the actual market listing boundary and rerun.

The collection summary fields have exact meanings:

- `expected`: interval slots in the requested range;
- `received`: parsed candles returned by HTTP, including unfinished candles;
- `inserted`: newly persisted rows;
- `existing`: incoming candles identical to retained rows;
- `conflicting`: same market key but different values;
- `missing`: expected timestamps absent after collection;
- `invalid`: rejected response pages plus invalid persisted candles;
- `unexpected`: stored timestamps outside the expected interval grid;
- `excluded_unfinished`: returned candles not yet closed at collection time;
- `empty_pages`: empty responses encountered before coverage completion.

Success requires zero conflicts, missing, invalid, and unexpected candles and
no collection failure. Repeating an unchanged collection is idempotent.

## Data validation and conflicts

Quality validation checks:

- finite positive OHLC prices and valid high/low relationships;
- finite nonnegative volumes and optional exchange fields;
- nonnegative trade counts;
- interval-aligned `open_time`;
- exact inclusive `close_time`;
- closed-candle status;
- duplicate, missing, and unexpected timestamps;
- exact expected-versus-actual coverage;
- exchange and market-type isolation.

Historical conflict policy is **detect and retain**. If an exchange later
returns different values for an existing primary key, CryptoLab keeps the
stored row, reports the timestamp, and returns a nonzero collection exit code.
It never silently rewrites research history. Resolve a conflict only after an
explicit review and backup.

Bybit V5 does not provide trade count or taker-buy volumes in its kline
response. Those fields remain SQL `NULL`; they are not estimated.

## SQLite schema and migration

Schema v2 identifies a candle by:

```text
(exchange, market_type, symbol, interval, open_time)
```

Prices and volumes are stored as decimal text and restored as `Decimal`.
`PRAGMA user_version` records the schema version.

Opening a legacy Binance-only database performs a transactional migration. The
new table is populated and row counts are verified before commit. The original
table is retained as `candles_legacy_v1_backup`; it is not deleted
automatically. Existing v2 databases with `user_version=0` are adopted only
after their required columns are verified.

Back up before upgrades:

```bash
mkdir -p backups
sqlite3 data/market_data.db ".backup 'backups/market_data-$(date +%Y%m%d).db'"
sqlite3 -readonly backups/market_data-YYYYMMDD.db "PRAGMA integrity_check;"
```

For recovery, stop all CryptoLab processes, preserve the failed database for
diagnosis, copy the verified backup into a new path, and point `--database` at
that path. SQLite supports one local writer at a time; do not run concurrent
collectors against the same file.

## Features and strategies

Feature rows include simple/log returns, SMA20, SMA50, EMA20, Wilder RSI14,
Wilder ATR14, and RVOL20. A row becomes available only after its candle closes.
Historical validation enforces ordering, interval continuity, timestamps, and
closed status. An explicit `as_of_time` makes research deterministic.

The unchanged research strategies are:

- Trend Following;
- Mean Reversion;
- Breakout.

Signals are market-research events, not orders. Spot `SELL` means reduce or
exit an existing long exposure; it never opens a short position.

## Backtesting assumptions

Each selected strategy receives an independent virtual portfolio starting with
1000 USDT. By default BUY is all-in while flat and SELL is all-out while
holding. Decisions formed at a candle close execute at the next candle open.
Defaults are 0.1% fee per fill and 0.05% deterministic adverse slippage.

CLI position sizing can allocate a fixed fraction of available quote cash to
each permitted BUY while retaining the legacy single-entry, all-out SELL mode:

```bash
cryptolab backtest ... --position-size-fraction 0.25 --show-trades
```

The strategy still emits quantity-free research signals. At the next candle
open a `PositionSizer` calculates the allocation. The execution path records
typed `OrderIntent`, `Order`, and `Fill` audit objects and applies the fill to
FIFO `PositionLot` accounting. These objects never submit an exchange order.

Scale-in/scale-out is an explicit opt-in. Both fractions are required to
select `FixedFractionScaleSizer`; the sell fraction may be `1` when scale-in
with full exits is desired:

```bash
cryptolab backtest ... \
  --position-size-fraction 0.25 \
  --position-sell-fraction 0.50 \
  --show-accounting
```

An aligned multi-symbol portfolio uses the primary `--symbol` plus one or more
`--portfolio-symbol` values. It requires one explicit strategy and a buy
fraction so shared-capital allocation cannot be implicit:

```bash
cryptolab backtest \
  --exchange bybit \
  --symbol BTCUSDT \
  --portfolio-symbol ETHUSDT \
  --interval 1h \
  --start 2025-01-01 \
  --end 2025-02-01 \
  --database data/market_data.db \
  --strategy trend \
  --position-size-fraction 0.50 \
  --show-trades \
  --show-accounting
```

The CLI quality-checks every instrument before feature construction.
Simultaneous events use the stable engine instrument order. `PositionCycle`
and `LotRealization` preserve flat-to-flat and FIFO audit history.

An optional research trailing stop is enabled with, for example:

```bash
cryptolab backtest ... --trailing-stop-rate 0.05 --show-trades
```

The stop uses only the prior high watermark. Strategy SELL has priority at the
next open; otherwise a gap exits at the candle open and an intrabar crossing is
modeled at the active stop before slippage. The current candle high updates the
watermark only after the stop check. Hourly OHLC does not reveal the exact
intrabar path or fill time.

Further model limitations:

- next-open execution is idealized;
- fixed slippage is not an order-book simulation;
- open positions are marked at the final close without hypothetical exit fee;
- drawdown uses candle-close equity, not intrabar lows;
- lot size, tick size, minimum notional, funding, leverage, and liquidation
  are not modeled by the backtest price model;
- the order lifecycle supports partial venue fills for offline adapter tests,
  while a candle backtest still creates one immediate fill per intent;
- multi-symbol backtests currently require identical candle timelines and one
  common exchange, interval, strategy, and USDT quote balance;
- fees and slippage are assumptions, not account-specific exchange rates.

## Offline execution adapter

`ExecutionAdapter` is a venue-neutral contract for future paper/testnet work.
`SimulatedExecutionAdapter` and `FakeVenueExecutionAdapter` are local-only:
they use no network, credentials, or private exchange API. They support order
reservations, partial fills, cancellation/rejection/expiration, idempotent
fill events, reconciliation reports, and immutable restart snapshots.

There is no Binance/Bybit authenticated adapter and no command that submits a
real or testnet order. Connecting a real venue requires a separate security
review, credential boundary, exchange-filter handling, and operational design.

## Testing, build, and CI

Tests use temporary SQLite databases and mocked HTTP transports. A global test
safeguard rejects real socket connections.

```bash
python -m pytest -q -p no:cacheprovider
ruff check src tests scripts
git diff --check
```

Build distributions and verify them locally:

```bash
python -m pip install -c requirements/constraints.txt -e ".[release]"
python -m build
python -m venv /tmp/cryptolab-wheel-test
/tmp/cryptolab-wheel-test/bin/python -m pip install dist/cryptolab-*.whl
/tmp/cryptolab-wheel-test/bin/cryptolab --help
```

GitHub Actions runs the offline suite on Python 3.12 and 3.13, compiles source
files, smoke-tests all six commands, builds and installs the wheel, runs Ruff,
audits dependencies with `pip-audit`, and scans the repository with Gitleaks.
CI results must be checked on GitHub; local success is not a claim that hosted
CI has run.

Generated databases, reports, build output, local environments, and `.env`
files are ignored. Deterministic small fixtures live under `tests/fixtures`.

## Security boundary

CryptoLab uses only public market-data endpoints. Never add private keys or
authenticated exchange endpoints without a separate architectural and security
review. Real order submission is prohibited by project policy. Logs contain
request classifications and timestamps, not credentials or full private
headers.

See [architecture](docs/architecture.md),
[ADR 0001](docs/decisions/0001-initial-architecture.md), and
[ADR 0002](docs/decisions/0002-data-integrity-and-release.md).
