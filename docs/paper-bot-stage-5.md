# Paper Bot Stage 5: production-style operations

## Safety boundary

Stage 5 prepares the existing paper process for long-running operation on an
Ubuntu VPS. It adds observability, notifications, container packaging, health
checks, and safe SQLite backup/restore. It does not add authenticated exchange
access, private keys, real orders, leverage, shorting, or remote control.

Strategy rules, sizing, fees, slippage, FIFO accounting, synchronized
BTC/ETH processing, and SELL-before-BUY rotation remain unchanged.

## Operational architecture

```text
ContinuousPaperTrader / MultiSymbolPaperTrader
                 │ durable financial commit
                 ▼
        PaperStateRepository (schema v4)
                 │
       ┌─────────┼──────────────┐
       ▼         ▼              ▼
 JSON stdout   PaperMetrics   notification outbox
 logs          /metrics       SQLite dedup/retry
                                  │ after commit
                                  ▼
                           TelegramNotifier
```

Observability is downstream of durable financial state. A metrics or Telegram
failure is logged and contained. Neither component participates in sizing,
risk approval, order lifecycle transitions, fill settlement, or accounting.

## Structured logging

`--json-logs` emits one JSON object per line to stderr. The container uses the
Docker `json-file` driver with `max-size=10m` and `max-file=5`; CryptoLab does
not create an unbounded application log file.

Common fields are:

- `timestamp`, `level`, `logger`, `message`, `event_type`;
- `session_id`, `mode`, `symbol`;
- `candle_timestamp`, `decision_id`, `order_id`, `fill_id`;
- `correlation_id`, `reason`, and safe error classifications.

Unsupported arbitrary record fields are omitted. Tokens, chat IDs, API keys,
passwords, environment dumps, request URLs containing Telegram tokens, and
response bodies are never intentionally logged. Exceptions record their type,
not their potentially sensitive HTTP URL.

JSON logging defaults to `INFO`; `--verbose` additionally enables `DEBUG`.
Legacy human-readable commands retain their previous warning-level default.

## Prometheus metrics

Enable the dependency-free HTTP endpoint with:

```bash
cryptolab paper run ... --metrics-port 9108
```

It binds to `127.0.0.1` unless `--metrics-host` is provided. Docker binds the
container listener to host loopback only. The endpoint exposes no identifiers
as labels, avoiding high cardinality.

| Metric | Type | Definition and restart behavior |
|---|---|---|
| `cryptolab_paper_up` | gauge | `1` after runtime start, `0` during graceful shutdown. Process-local. |
| `cryptolab_paper_last_candle_timestamp_seconds` | gauge | Open timestamp of the latest durable closed candle/set. Rebuilt from checkpoint. |
| `cryptolab_paper_last_decision_timestamp_seconds` | gauge | Latest decision availability timestamp. Rebuilt from durable replay/last synchronized candle. |
| `cryptolab_paper_data_lag_seconds` | gauge | `max(0, now - expected availability)` for the latest durable candle. |
| `cryptolab_paper_orders_total` | counter | Count of durable simulated orders. Initialized from lifecycle after restart. |
| `cryptolab_paper_fills_total` | counter | Count of durable confirmed fills. Initialized from lifecycle after restart. |
| `cryptolab_paper_risk_rejections_total` | counter | Durable risk-policy rejection count restored from risk state. |
| `cryptolab_paper_recovery_total` | counter | Recoveries performed by the current process; resets on process restart. |
| `cryptolab_paper_reconciliation_failures_total` | counter | New reconciliation failures observed by the current process; resets on restart. |
| `cryptolab_paper_cash_usdt` | gauge | Available virtual USDT balance. Rebuilt from accounting. |
| `cryptolab_paper_equity_usdt` | gauge | Latest marked paper equity. Rebuilt after recovery. |
| `cryptolab_paper_drawdown_ratio` | gauge | Positive peak-to-current drawdown. Uses the durable risk peak when available. |
| `cryptolab_paper_pending_orders` | gauge | Number of non-terminal simulated orders. |
| `cryptolab_paper_paused` | gauge | `1` when manual BUY pause is active. |

`/metrics` is read-only. `/healthz` reports only that the exporter thread is
responding. The CLI `paper health` performs the durable database check and,
when `--maximum-progress-lag-seconds` is supplied, rejects stale, missing, or
future checkpoint progress.

## Telegram delivery

Telegram is enabled when both variables exist:

```text
CRYPTOLAB_TELEGRAM_BOT_TOKEN
CRYPTOLAB_TELEGRAM_CHAT_ID
```

`--telegram` may be used to require configuration explicitly. Credentials are
never persisted in SQLite. Notifications cover startup/recovery, shutdown,
BUY/SELL fills, partial fills, risk rejection, stale/no-new-candle state,
manual pause/resume, reconciliation failure, and fail-closed errors.

Schema v4 adds `paper_notification_outbox`. Each event has a deterministic
`(session_id, event_type, dedup_key)` identity. Repeated enqueue after restart
is a no-op. Delivery is bounded per flush, rate limited, and retried with
exponential delay. A failed Telegram call never rolls back or blocks already
committed accounting.

The delivery guarantee is **at least once after durable enqueue**, not exactly
once. Telegram offers no application idempotency key. A crash after Telegram
accepts a message but before SQLite marks it `sent` may produce one duplicate.
A crash after the trading commit but before outbox enqueue may omit that
notification. These limitations are preferable to coupling remote messaging
to financial state.

## Persistence migration

Paper-state schema v3 migrates transactionally to v4 by adding only the
notification outbox and updating session schema markers. Existing single and
multi sessions, candles, decisions, orders, fills, reservations, FIFO state,
pending rotation, controls, and reconciliation history remain unchanged.

## Deployment files

- `Dockerfile`: digest-pinned Python 3.13 base, constrained offline wheelhouse,
  and non-root UID/GID `10001` runtime.
- `compose.yaml`: one paper process, one SQLite writer, graceful SIGTERM,
  restart policy, healthcheck, resource limits, read-only root filesystem,
  persistent bind mounts, bounded Docker logs, and loopback-only metrics.
- `.env.example`: names only; never commit real values.
- `deploy/prometheus-paper.yml`: optional scrape job for an existing internal
  Prometheus deployment.
- `deploy/prometheus-paper-alerts.yml`: optional target, process, stale-data,
  and reconciliation alert rules. They are inactive until loaded by an
  independent Prometheus/Alertmanager installation.

No second systemd service is provided. Docker Compose is the single documented
process supervisor.

## Limitations

- Public REST and public ticker remain the only exchange I/O.
- Telegram is operational notification, not an execution confirmation source.
- Metrics are stored in memory; only values derivable from durable state are
  reconstructed after restart.
- The health command is network-free and checks structural state plus optional
  bounded progress lag. `paper integrity` remains the deeper deterministic
  recovery/reconciliation check.
- One process and one SQLite writer remain required.
- Durable sessions stop before 10,000 candles; the operator must archive and
  rotate the session rather than allowing unbounded full-prefix work.
- Docker health alone is not external alerting. Telegram is optional and is
  disabled in the initial deployment.
- VPS behavior cannot be claimed from local container verification alone.

See [the operations runbook](paper-bot-operations.md) for deployment, backup,
restore, and incident commands.

