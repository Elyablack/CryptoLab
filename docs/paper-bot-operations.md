# Paper Bot Operations Runbook

## 1. Host preparation

Install Docker Engine with the Compose plugin. Create writable persistent
directories for the non-root container user:

```bash
mkdir -p data backups
sudo chown -R 10001:10001 data backups
chmod 750 data backups
```

Place the validated `market_data.db` in `data/`. Never put exchange private
keys in this project; they are not used.

Optional Telegram configuration should live outside Git:

```bash
cp .env.example /secure/cryptolab-paper.env
chmod 600 /secure/cryptolab-paper.env
```

Set the token and chat ID in that external file. Leave both empty to disable
Telegram.

## 2. Validate data before startup

```bash
cryptolab quality \
  --exchange bybit --symbol BTCUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db

cryptolab quality \
  --exchange bybit --symbol ETHUSDT --interval 1h \
  --start 2023-11-01 --end 2026-11-01 \
  --database data/market_data.db
```

## 3. Build and start

```bash
docker compose build --pull
docker compose --env-file /secure/cryptolab-paper.env config
docker compose --env-file /secure/cryptolab-paper.env up -d
docker compose ps
docker compose logs --tail=100 paper-bot
```

The Compose configuration starts exactly one multi-symbol Rotation paper
process. Metrics are available only through host loopback:

```bash
curl --fail http://127.0.0.1:9108/metrics
```

To integrate the existing monitoring stack, merge the job from
`deploy/prometheus-paper.yml`. Do not expose port 9108 publicly.

## 4. Graceful operation

```bash
docker compose stop --timeout 30 paper-bot
docker compose start paper-bot
docker compose restart --timeout 30 paper-bot
```

Compose sends SIGTERM. The process stops its polling loop, emits its shutdown
event, flushes a bounded notification batch, closes SQLite handles, and exits.

Manual BUY pause continues to allow ordinary risk-reducing SELL behavior:

```bash
docker compose exec paper-bot cryptolab paper pause \
  --mode multi --session-id btc-eth-rotation \
  --state-database /app/data/paper_state.db --json-logs

docker compose exec paper-bot cryptolab paper resume \
  --mode multi --session-id btc-eth-rotation \
  --state-database /app/data/paper_state.db --json-logs
```

Because the image entrypoint is already `cryptolab`, use `docker compose run
--rm paper-bot paper ...` for one-off commands when the service is stopped.

## 5. Health and integrity

Fast network-free health check:

```bash
cryptolab paper health \
  --mode multi --session-id btc-eth-rotation \
  --state-database data/paper_state.db
```

This checks SQLite integrity, schema compatibility, session existence, and the
durable reconciliation flag. It does not reconstruct the strategy.

Deep deterministic recovery check uses the same arguments as the running
session:

```bash
cryptolab paper integrity \
  --mode multi --session-id btc-eth-rotation --strategy rotation \
  --market-database data/market_data.db \
  --state-database data/paper_state.db
```

Do not start another writer while the continuous service is running. Health
and backup are read-only; `paper integrity` may append a reconciliation
revision and should be scheduled deliberately.

## 6. Online backup

Never use `cp` on an active SQLite database. Use the SQLite Backup API through
the CLI:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)

cryptolab paper backup \
  --source-database data/paper_state.db \
  --output "backups/paper_state-${stamp}.db"

cryptolab paper backup \
  --source-database data/market_data.db \
  --output "backups/market_data-${stamp}.db"
```

The command refuses an existing destination, creates a consistent snapshot,
runs `PRAGMA integrity_check`, and verifies the CryptoLab schema version.

Verify later without modifying it:

```bash
cryptolab paper verify-backup \
  --backup backups/paper_state-YYYYMMDDTHHMMSSZ.db
```

Store paper-state and market-data backups from the same maintenance window.
Protect Telegram environment files separately; credentials are intentionally
not stored in either database.

## 7. Controlled restore drill

Stop the writer before a recovery drill, but never overwrite the working DB:

```bash
docker compose stop --timeout 30 paper-bot

cryptolab paper restore \
  --backup backups/paper_state-YYYYMMDDTHHMMSSZ.db \
  --output data/restore-check/paper_state.db

cryptolab paper restore \
  --backup backups/market_data-YYYYMMDDTHHMMSSZ.db \
  --output data/restore-check/market_data.db

cryptolab paper health \
  --mode multi --session-id btc-eth-rotation \
  --state-database data/restore-check/paper_state.db

cryptolab paper integrity \
  --mode multi --session-id btc-eth-rotation --strategy rotation \
  --market-database data/restore-check/market_data.db \
  --state-database data/restore-check/paper_state.db
```

Only after both checks succeed may an operator deliberately change deployment
paths. The restore command itself never changes Compose or replaces a file.

The restored checkpoint prevents events already present in that backup from
being applied twice. However, any orders, fills, controls, candle checkpoints,
or sent-notification markers created after the backup timestamp are absent.
Starting from an old copy may therefore repeat later simulated decisions or
Telegram messages. Compare the stopped working database with the restored copy
and choose the authoritative checkpoint explicitly.

## 8. Incident response

### Reconciliation failure / fail-closed

1. Leave the service stopped or paused.
2. Preserve the original database with `paper backup`.
3. Run `paper health` and `paper integrity` against a copy.
4. Inspect structured events `reconciliation_failure` and `fail_closed`.
5. Do not edit balances, fills, reservations, or checkpoints manually.
6. Restore a verified copy to a new path if recovery is required.

### Stale or missing candles

1. Check public network availability and structured `stale_market_data` /
   `no_new_candle` events.
2. Run `quality` for both symbols over the affected range.
3. Allow the existing collector to recover gaps; do not synthesize candles.
4. No historical paper fills will be generated during backfill.

### Telegram failure

Paper execution continues. Pending messages stay in
`paper_notification_outbox` with a bounded retry time. Check environment
configuration and outbound HTTPS. Never print the token while diagnosing.

### Metrics failure

The process logs `metrics_start_failed` and continues paper execution. Check
the bind address/port and Docker networking. The exporter has no influence on
orders or accounting.

## 9. Local verification

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src
git diff --check
docker build -t cryptolab-paper:stage5 .
docker compose config
```

Local success verifies build/configuration only. It does not prove behavior on
the target VPS or success of hosted GitHub Actions.

