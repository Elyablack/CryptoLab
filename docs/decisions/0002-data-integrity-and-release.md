# ADR 0002: Data integrity and v1.0 release policy

- Status: accepted
- Date: 2026-11-08

## Context

CryptoLab now supports two public exchanges, analytics, research strategies,
and deterministic backtesting. A reliable release must not report a successful
collection when history is incomplete, and it must not silently rewrite stored
history when an exchange changes a candle.

## Decisions

### Collection success is based on persisted coverage

HTTP completion alone is insufficient. Every collection ends with an exact
SQLite quality check over the requested half-open range. Missing, invalid,
unexpected, conflicting, or prematurely empty results produce a nonzero CLI
exit code.

### Retry only classified transient failures

Timeouts, transport failures, HTTP 429/5xx, and Bybit rate-limit code 10006 use
bounded exponential backoff. Binance 418 and non-rate-limit 4xx responses stop
immediately. The implementation uses HTTPX and the Python standard library;
no retry framework is added.

### Detect and retain historical conflicts

For an existing primary key:

- identical values are counted as idempotent `existing` data;
- differing values are reported as a conflict;
- the existing row is retained;
- collection fails until a human reviews the discrepancy.

Automatic overwrite was rejected because it would change prior research input
without an audit decision. Automatic version tables were deferred because they
would expand the storage model beyond current requirements.

### Version SQLite explicitly and keep a migration backup

`PRAGMA user_version` records schema v2. Legacy rows are copied transactionally
into the exchange-aware table, verified by row count, and the original table is
retained as `candles_legacy_v1_backup`.

### Local package is the deployment target

The supported v1.0 deployment is a constrained Python 3.12/3.13 package in a
virtual environment. Docker, services, schedulers, PostgreSQL, Redis, and
Kubernetes are not required for the local research scope.

### Tests must be offline and self-contained

All automated tests block socket connections and use versioned small fixtures,
mocked transports, and temporary SQLite databases. Generated reports and local
datasets are not test dependencies.

## Consequences

- collection failures are explicit and sometimes require adjusting a range to
  the true listing date;
- corrected exchange candles require manual conflict review;
- migrations consume additional disk space until the user archives the backup;
- clean installation and release artifacts can be verified independently of
  local databases and reports;
- live trading remains outside the architecture.
