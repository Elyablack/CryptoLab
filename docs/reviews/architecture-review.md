# CryptoLab: архитектурная карта и фактические потоки выполнения

Дата ревью: 2026-11-15
Ревизия: `35083da` (`main`)
Режим ревью: read-only; этот документ не предлагает скрытого изменения торговой семантики.

## 1. Контекст и системная граница

CryptoLab — синхронное локальное Python-приложение с одним CLI-процессом и двумя
SQLite-базами:

- `market_data.db` хранит неизменяемую историю публичных Spot-свечей;
- `paper_state.db` хранит journal, checkpoints, execution lifecycle, risk state,
  reconciliation и Telegram outbox бумажного бота.

Приложение не отправляет реальные или testnet-ордера. `ExecutionAdapter` — граница
для offline simulation; единственная venue-реализация в проекте —
`SimulatedExecutionAdapter`/`FakeVenueExecutionAdapter`.

```text
                    public Binance / Bybit HTTP
                              |
                              v
CLI ---> market_data ---> SQLiteRepository (market_data.db)
 |             |                    |
 |             +--> validation <----+
 |                                  |
 +--> analytics/features <----------+
             |
             v
        StrategyRunner ---> StrategyDecision (BUY/SELL/HOLD)
             |                         |
             |                         v
             |              sizing + risk policy
             |                         |
             v                         v
    BacktestEngine / Replay ----> OrderIntent -> Order -> Fill
             |                                      |
             +----------------> PortfolioAccounting <+
                                      |
                          FIFO lots, cash, fee, PnL
                                      |
              +-----------------------+--------------------+
              |                                            |
              v                                            v
      reports/research                     PaperStateRepository
                                            (paper_state.db)
                                                    |
                           logs / metrics / Telegram outbox
```

### Фактическое направление зависимостей

Основной поток направлен от данных к доменной логике и затем к orchestration:

```text
market_data -> analytics -> features -> strategies
                                  \        /
                                   backtesting
                                  /     |      \
                         execution  replay   research
                                      |         |
                                     paper <----+
                                      |
                                    storage
```

Граница не является строго слоистой. На уровне пакетов существуют обратные
зависимости:

- `market_data.data_quality` и `market_data.collector` импортируют
  `storage.SQLiteRepository`, а `storage.sqlite_repository` импортирует модели
  `market_data`;
- `replay.persistent` импортирует `storage.PaperStateRepository`, а сам
  repository импортирует replay-модели для сериализации checkpoint;
- `paper` собирает почти все нижние слои и является application service layer.

Это не обнаруженный runtime import cycle, но это архитектурная связность,
которую надо учитывать при изменениях моделей и persistence.

## 2. Точки входа

- Packaging entry point: `pyproject.toml` → `cryptolab = "cryptolab.cli:main"`.
- Главный dispatcher: `src/cryptolab/cli.py:98-153`.
- Исследовательские команды: `collect`, `quality`, `analyze`, `signals`,
  `backtest`, `research`, `optimize`, `signals-research`.
- Операционные paper-команды: `paper run-once`, `run`, `status`, `pause`,
  `resume`, `integrity`, `health`, `backup`, `verify-backup`, `restore`.
- Container entry point: `Dockerfile:31-32`; Compose запускает
  `cryptolab paper run --mode multi` (`compose.yaml:14-31`).

## 3. Карта пакетов и значимых модулей

### 3.1 `market_data`: получение, нормализация и качество свечей

| Модуль | Назначение и API | Вход → выход / состояние | Зависимости, callers и тесты |
|---|---|---|---|
| `market_data/models.py` | Frozen `Candle`, единая OHLCV-модель. Bybit-отсутствующие trade/taker поля представлены `None`, а не выдуманными значениями. | API values → `Decimal`, timestamps `int`; состояния нет. | Используется parser, validator, storage, analytics, replay. `test_parser.py`, `test_bybit_parser.py`, `test_validator.py`. |
| `market_data/markets.py` | Exchange/market constants и `validate_market_identity()`. | Строки → validated identity. | Используется CLI, repository, collector, backtest. Покрывается storage/CLI tests. |
| `market_data/intervals.py` | Поддерживаемые интервалы и `get_interval_milliseconds()`. | `1m…1d` → ms. | Validator, quality, strategies, services. Пограничные случаи проверяются validator/data-quality tests. |
| `market_data/client.py` | `BinanceSpotClient.get_klines()`. | Public REST params → raw Binance JSON. | Делегирует retries в `http.get_public_json`. `test_client.py`, `test_http_retry.py`. |
| `market_data/bybit_client.py` | `BybitSpotClient.get_klines()` и public `get_ticker()`, всегда Spot. | V5 public JSON → raw envelope / ticker. | Collector и paper market source. `test_bybit_client.py`, `test_http_retry.py`. |
| `market_data/http.py` | `RetryPolicy`, классификация 418/429/5xx/transport, exponential backoff, `Retry-After`. | HTTP request → decoded JSON или typed failure. Только локальное состояние попытки. | Оба клиента. `test_http_retry.py`. |
| `market_data/parser.py` | `parse_kline(s)` для Binance. | Raw arrays → `Candle`. | Collector. `test_parser.py`. |
| `market_data/bybit_parser.py` | Проверка V5 envelope, reverse chronological list → chronological candles. | Bybit envelope → `list[Candle]`. | Collector. `test_bybit_parser.py`, `test_bybit_collector.py`. |
| `market_data/validator.py` | `validate_candle`, timing и `validate_historical_candles`. | Candle sequence → success/`ValueError`; проверяет OHLCV, finite Decimal, alignment, close, continuity, closed status. | Features, collector, quality, replay/paper. `test_validator.py`, `test_features.py`. |
| `market_data/collector.py` | `HistoricalDataCollector`, forward Binance/backward Bybit pagination, `CollectionResult`/`CollectionFailure`. | Requested half-open range → validated pages + atomic repository writes + exact post-check. | Клиенты, parsers, repository, quality. `test_collector.py`, `test_bybit_collector.py`, CLI tests. |
| `market_data/data_quality.py` | `DataQualityChecker.check()` и immutable `DataQualityReport`. | Market identity + range → exact missing/unexpected/duplicate/invalid report. | Repository range query; CLI и collectors. `test_data_quality.py`, CLI tests. |

Durable state находится только в `SQLiteRepository`; HTTP clients и collector
не сохраняют скрытого cursor state.

### 3.2 `storage`: SQLite persistence

| Модуль | Назначение и API | Вход → выход / состояние | Зависимости, callers и тесты |
|---|---|---|---|
| `storage/sqlite_repository.py` | Market schema v2, legacy Binance migration, `save_candles`, range queries, conflict detection. | Candles ↔ rows; Decimal хранится как TEXT. PK включает exchange/market/symbol/interval/open_time. | Collector, quality, CLI, paper market source. `test_sqlite_repository.py`, migration/isolation/collector tests. |
| `storage/paper_state_repository.py` | Paper schema v4, journal/checkpoint/control/reconciliation/outbox, atomic commits and migrations v1→v4. | Replay/portfolio snapshots ↔ normalized tables + canonical JSON. | Persistent replay, both paper services, telemetry/outbox/operations. `test_persistent_replay.py`, `test_paper_stage3.py`, `test_paper_stage4.py`, `test_paper_stage5.py`. |

`PaperStateRepository.commit_candle()` starts `BEGIN IMMEDIATE`, writes journal
and all derived state, then commits (`paper_state_repository.py:371-452`).
Multi-symbol state has an analogous atomic commit path. In-memory state that was
mutated before a failed commit is discarded and rebuilt from the durable
checkpoint by service orchestration.

### 3.3 `analytics` и `features`: чистые вычисления

| Модуль | Назначение и API | Вход → выход | Callers / тесты |
|---|---|---|---|
| `analytics/returns.py` | Simple/log returns, sample volatility (`n-1`), non-positive maximum drawdown. | Candle/Decimal sequences → Decimal values. | Analyzer/FeatureBuilder. `test_returns.py`. |
| `analytics/indicators.py` | SMA, SMA-seeded EMA, Wilder RSI/ATR, prior-window RVOL. | Decimal/Candle sequence → aligned values with warm-up `None`. | FeatureBuilder/Analyzer. `test_indicators.py`. |
| `analytics/models.py` | Frozen `MarketAnalysisReport`. | Computed latest metrics. | Analyzer/formatter/CLI. |
| `analytics/analyzer.py` | `MarketAnalyzer.analyze()`, validation + last-value report. | Closed complete candles → report. | `analyze` CLI. `test_analyzer.py`, `test_cli_analyze.py`. |
| `analytics/formatter.py` | Human-readable analytics output. | Report → text. | CLI; formatter tests. |
| `features/models.py` | Frozen `FeatureRow` with OHLCV, indicators and `available_time`. | One closed candle + aligned metrics. | Strategies, replay, backtest, research. |
| `features/builder.py` | `FeatureBuilder.build()`. | Complete historical candles → one row per candle; `available_time=close_time+1`. | CLI loaders, replay, paper services. `test_features.py`. |

Эти функции не изменяют состояние. Основная гарантия против look-ahead — explicit
availability timestamp и проверка полного исторического ряда до вычислений.

### 3.4 `strategies`: исследовательские сигналы

| Модуль | Назначение и API | Состояние / зависимости | Тесты |
|---|---|---|---|
| `strategies/models.py` | `Signal` и frozen `StrategyDecision` с immutable diagnostics. SELL означает снижение Spot exposure, не short. | Состояния нет; Decimal reference price. | `test_strategy_models.py`. |
| `strategies/base.py` | `Strategy` Protocol и `make_decision()`. | Interface над prefix `Sequence[FeatureRow]`. | Все strategies/tests. |
| `strategies/runner.py` | `StrategyRunner`, read-only `_HistoryView`, warm-up HOLD и metadata consistency. | Future rows физически недоступны view. | `test_strategy_runner.py`, replay tests. |
| `trend_following.py` | SMA20/50 crossover + RSI/RVOL/price confirmation; configurable exit policy. | Только переданный history. | `test_strategies.py`. |
| `mean_reversion.py` | RSI/deviation/recovery event с optional recovery window. | Восстановление ищется только назад. | `test_strategies.py`. |
| `breakout.py` | Prior-only rolling range, RVOL/ATR confirmations, event policy. | Текущая candle не входит в собственный threshold. | `test_strategies.py`. |
| `trend_pullback.py` | Pullback candidate и fixed exit policies для research. | Stateful event semantics вычисляются из history, без mutable portfolio. | `test_trend_pullback.py`. |
| `relative_momentum.py` | `RelativeMomentumRotationPlanner`, weekly BTC/ETH/USDT target и leg strategies. | План формируется из синхронных histories. | `test_relative_momentum.py`, research/paper stage4 tests. |

Стратегии никогда не определяют quantity, fee, fill или portfolio state. Это
важная граница: `StrategyDecision` — наблюдение рынка, а не заявка.

### 3.5 `execution`: lifecycle без реального venue

| Модуль | Назначение и API | Изменяемое состояние | Тесты |
|---|---|---|---|
| `execution/models.py` | `Instrument`, `OrderIntent`, `Order`, `Fill`, `OrderSide`, `OrderStatus`, `IntentSource`. | Frozen validated value objects. | `test_execution_models.py`. |
| `execution/lifecycle.py` | `ReservationBook`, `OrderLifecycle`: submit, partial fill, cancel/reject/expire, duplicate/overfill protection. | Orders, fills, reservations held in memory and snapshot-able. | `test_order_lifecycle.py`. |
| `execution/adapter.py` | `ExecutionAdapter` Protocol, `SimulatedExecutionAdapter`, `FakeVenueExecutionAdapter`, snapshots/reconcile. | Wraps lifecycle + deterministic event sequence. | `test_execution_adapter.py`, persistent replay tests. |

`FakeVenueExecutionAdapter` остаётся offline. Наличие Protocol не означает
готовность authenticated venue integration.

### 3.6 `backtesting`: sizing, execution orchestration и accounting

| Модуль | Назначение и API | Состояние / callers | Тесты |
|---|---|---|---|
| `backtesting/models.py` | `BacktestConfig`, state/fill/trade/equity/report models; separate stop/timed fills. | Immutable audit outputs. | `test_backtest_engine.py`, formatter tests. |
| `backtesting/sizing.py` | `PositionSizer` Protocol; legacy all-in/out, fixed fraction, scale-in/out proposals. | Чистое sizing decision. | `test_position_sizing.py`. |
| `backtesting/accounting.py` | `PortfolioAccounting`, FIFO `PositionLot`, `LotRealization`, `PositionCycle`; cash, cost basis, fees, realized/unrealized PnL invariants. | Mutable accounting, altered only from confirmed fills. | `test_portfolio_accounting.py`. |
| `backtesting/engine.py` | `BacktestEngine.run/run_portfolio`, next-open execution, sizing, lifecycle, stops, accounting reconciliation; `BacktestExecutionSession` for replay. | One isolated in-memory portfolio per run/session. | `test_backtest_engine.py`, `test_multi_symbol_backtest.py`, replay tests, trailing-stop tests. |
| `backtesting/portfolio.py` | Shared-cash portfolio inputs/reports. | Immutable report data. | Multi-symbol tests. |
| `backtesting/benchmarks.py` | Cash and cost-comparable buy-and-hold reports. | Pure calculations. | `test_benchmarks.py`. |
| `backtesting/formatter.py` | Summary/trades/lots/cycles/portfolio formatting. | Report → text. | `test_formatter.py`, CLI backtest tests. |

### 3.7 `replay`: event-time simulation и recovery

| Модуль | Назначение и API | Состояние / callers | Тесты |
|---|---|---|---|
| `replay/models.py` | Replay modes/events/snapshots/checkpoint/report and `PriceObservation`. | Frozen audit values. | Deterministic/persistent tests. |
| `replay/risk.py` | `ReplayRiskPolicy` Protocol, allow-all policy, `ReplayRiskContext/Result`. | Pluggable state snapshot contract. | Replay/paper tests. |
| `replay/runner.py` | `DeterministicReplayRunner/Session`, explicit clock; candle open → pending decision execution → close/features/new decision. | In-memory candles, rows, decisions, events, execution session. | `test_deterministic_replay.py`. |
| `replay/persistence_models.py` | Typed persistence failures, crash points and stored records. | Value objects. | Persistent tests. |
| `replay/serialization.py` | Canonical JSON and exact Decimal/model round trips. | Object ↔ stable JSON. | Persistent/Stage tests. |
| `replay/persistent.py` | `PersistentReplayRunner/Session`: atomic persistence, fail-closed, recovery by deterministic journal replay and checkpoint comparison. | In-memory handle backed by paper DB. | `test_persistent_replay.py` with crash injection at every defined point. |

### 3.8 `paper`: continuous orchestration and operations

| Модуль | Назначение и API | Состояние / callers | Тесты |
|---|---|---|---|
| `paper/models.py` | Runtime, execution, risk, single/multi cycle/status and pending rotation models. | Validated config and immutable outputs. | Stage3/4 tests. |
| `paper/market.py` | `PaperMarketSource`: loads closed candles and observes public ticker. | Stateless except clients/repository. | Stage3 tests with fakes/mocks. |
| `paper/execution.py` | `PaperExecutionSimulator`: latency, slippage, fee and partial simulated fill; applies confirmed fill to FIFO accounting. | Uses offline adapter. | Stage3/4 tests. |
| `paper/risk.py` | `PaperRiskManager`: pause, freshness, max orders/notional/exposure, daily loss and drawdown. | Mutable counters/peaks with persistence snapshot. | Stage3/4 risk tests. |
| `paper/service.py` | `ContinuousPaperTrader`: single-symbol polling, backfill without trades, only newest fresh candle tradable, persistent replay. | Holds persistent session and injected clock/sleep. | `test_paper_stage3.py`, telemetry tests. |
| `paper/portfolio_service.py` | `MultiSymbolPaperTrader`: synchronized BTC/ETH sets, durable SELL→BUY rotation state, reconciliation and shared cash. | In-memory accounting/adapter rebuilt from durable journal/checkpoint. | `test_paper_stage4.py`, Stage5 telemetry tests. |
| `paper/observability.py` | Structured JSON logs, Prometheus text endpoint, telemetry counters/gauges. | In-memory metrics; downstream of financial commits. | `test_paper_stage5.py`. |
| `paper/notifications.py` | `TelegramNotifier` and durable, deduplicated, retrying `NotificationOutbox`. | Delivery state in paper DB; HTTP client in memory. | Stage5 MockTransport tests. |
| `paper/operations.py` | DB verify/health/online backup/non-destructive restore. | Reads source, creates a new verified destination. | Stage5 backup/health/CLI tests. |

### 3.9 `research`: diagnostics, splits и experiment reports

| Группа модулей | Архитектурная роль | Callers / тесты |
|---|---|---|
| `research/models.py`, `event_study_models.py`, `relative_momentum_models.py` | Immutable research reports and statistics. | Formatters/CLI; research tests. |
| `research/validation.py` | Chronological split, walk-forward periods, period-restricted strategy wrapper. | Research/optimization/event study. `test_research.py`, optimization tests. |
| `research/runner.py`, `relative_momentum_runner.py` | Reuse BacktestEngine and benchmarks; diagnose trades, inactive conditions and rotation episodes. | `research` CLI and tests. |
| `research/optimization.py` | Bounded predefined grids, walk-forward evaluation and baseline comparison. | `optimize`; `test_research_optimization.py`, CLI tests. |
| `research/event_study.py` | Signal-event collection, forward outcomes, matched controls, regimes, seeded bootstrap. | `signals-research`; `test_event_study.py`. |
| `research/pullback_exit_study.py`, `rotation_failure_analysis.py` | Focused, deterministic studies over existing strategy/report data. | Scripts/tests; no production execution. |
| `research/*formatter.py` | Text/JSON/CSV serialization. | CLI/scripts; dedicated formatter and CLI tests. |

### 3.10 Application, tests и deployment

- `src/cryptolab/cli.py` — composition root: argument validation, dependency
  construction, command dispatch and human-readable output. Financial accounting
  не реализуется в CLI.
- `tests/conftest.py` — autouse socket blocker; unit/integration tests offline.
- `tests/fixtures/` — deterministic version-controlled inputs and baseline data.
- `scripts/` — reproducible research/diagnostic frontends, not runtime services.
- `Dockerfile` — two-stage wheel build, non-root runtime.
- `compose.yaml` — one paper process, volumes, loopback metrics, bounded logs and
  resource limits.
- `deploy/prometheus-paper.yml` — scrape-only example.
- `.github/workflows/ci.yml` и `security.yml` — Python 3.12/3.13 tests,
  package build, Ruff, dependency audit and secret scan.

## 4. Фактические execution flows

### 4.1 Historical data ingestion

```text
cryptolab collect
  -> cli.main -> _run_collect
  -> SQLiteRepository.init_schema
  -> BinanceSpotClient | BybitSpotClient
  -> HistoricalDataCollector.collect
       -> get_klines (page)
       -> parse_klines | parse_bybit_klines
       -> validate_historical_candles / closed-range filtering
       -> SQLiteRepository.save_candles
            -> detect unchanged/conflict/insert
            -> SQLite transaction commit on context exit
       -> advance pagination cursor
  -> DataQualityChecker.check persisted exact range
  -> CollectionResult or CollectionFailure -> CLI exit code/summary
```

Unexpected empty pages, stalled cursors, invalid pages, conflicts or incomplete
coverage do not become success. Existing equal rows are idempotent; differing
historical rows are retained and reported as conflicts.

### 4.2 Feature calculation

```text
repository.get_candles_by_range
  -> validate_historical_candles
  -> FeatureBuilder.build
       -> simple/log returns
       -> SMA20/SMA50/EMA20
       -> Wilder RSI14/ATR14
       -> prior-only RVOL20
       -> FeatureRow(... available_time = close_time + 1)
```

Warm-up values остаются `None`. `as_of_time` делает historical simulation
детерминированной и исключает ещё недоступные свечи.

### 4.3 Strategy evaluation

```text
StrategyRunner.run/evaluate_latest
  -> validate FeatureRow grid and OHLC/timestamps
  -> _HistoryView(rows, 0..current)
  -> warm-up HOLD OR Strategy.evaluate(history)
  -> make_decision
  -> validate strategy/symbol/interval/candle/availability/reference price
  -> StrategyDecision
```

Торговое решение принимается только в `Strategy.evaluate()`. Quantity и portfolio
state стратегии не видят.

### 4.4 Batch backtest

```text
CLI _run_backtest
  -> _load_backtest_rows (quality + FeatureBuilder)
  -> StrategyRunner.run
  -> BacktestEngine.run | run_portfolio
       -> previous candle decision at current candle open
       -> PositionSizer.size
       -> OrderIntent
       -> SimulatedExecutionAdapter.submit
       -> OrderLifecycle reservation/order
       -> deterministic Fill (fee + adverse slippage)
       -> adapter.apply_fill
       -> PortfolioAccounting.record_buy/record_sell
       -> FIFO lots/realizations/cycles
       -> close-equity point and invariant checks
  -> BacktestReport -> formatter
```

Strategy SELL исполняется на open раньше candle-path stop. Trailing/protective
intrabar fills имеют собственные typed models и не получают выдуманный strategy
timestamp. Открытая позиция в конце только mark-to-market по последнему close.

### 4.5 Deterministic replay

```text
DeterministicReplaySession.process_candle(candle)
  -> validate next consecutive candle
  -> clock.advance_to(open_time)
  -> execute pending previous decision through BacktestExecutionSession
  -> open snapshot
  -> clock.advance_to(close_time + 1)
  -> append closed candle
  -> FeatureBuilder.build(all candles)
  -> StrategyRunner.evaluate_latest
  -> pending decision for a later execution
  -> close equity snapshot + deterministic events
```

Replay переиспользует execution/accounting kernel BacktestEngine, а не содержит
вторую финансовую реализацию.

### 4.6 Persistent replay and recovery

```text
PersistentReplaySession.process_*(event)
  -> inner replay mutates in-memory state
  -> risk policy state snapshot
  -> PaperStateRepository.commit_candle
       BEGIN IMMEDIATE
       insert immutable candle journal event
       sync events/decisions/intents/orders/fills/reservations/accounting
       write checkpoint + risk state
       COMMIT                         <-- durable boundary
  -> after-commit telemetry/caller work

restart:
PersistentReplayRunner.recover
  -> load configuration, journal, checkpoint and execution snapshot
  -> restore initial risk state
  -> replay every durable candle in sequence
  -> compare rebuilt checkpoint/risk/execution snapshot with stored values
  -> return available session OR fail closed
```

До COMMIT mutated Python objects не являются источником истины. После failed
commit service закрывает/сбрасывает handle и на следующем цикле reconstructs из
последнего durable checkpoint.

### 4.7 Single-symbol paper loop

```text
cryptolab paper run [--mode single]
  -> CLI builds repository, market source, risk manager, engine, telemetry
  -> ContinuousPaperTrader.start (create/recover persistent replay)
  -> run_forever
       -> run_once
          -> compute fully closed range
          -> PaperMarketSource.load_closed_candles
          -> exact-range validation
          -> backfilled candles: replay only, trading_enabled=False
          -> newest fresh candle: public ticker PriceObservation
          -> PersistentReplaySession.process_observed_candle
               -> strategy decision -> sizing -> PaperRiskManager.check
               -> PaperExecutionSimulator -> adapter fill
               -> FIFO accounting -> atomic checkpoint COMMIT
          -> telemetry/outbox after durable state
       -> poll sleep
```

Paper execution не симулирует historical next-open: после close она использует
отдельно наблюдённую public ticker price и observation time. Это намеренная,
документированная модель, поэтому paper fills не обязаны совпадать с backtest.

### 4.8 Multi-symbol rotation

```text
cryptolab paper run --mode multi --strategy rotation
  -> MultiSymbolPaperTrader.start
       -> restore/create -> reconcile(persist=True)
  -> load synchronized closed BTCUSDT + ETHUSDT sets
  -> FeatureBuilder per symbol
  -> RelativeMomentumRotationPlanner latest weekly review
  -> if target changed: persist PendingRotation(SELL)
  -> observe both public prices
  -> _execute_pending
       active asset != target:
         SELL -> risk -> simulated fill -> FIFO accounting
         COMMIT SELL and PendingRotation(BUY)      <-- crash boundary
       flat and target != CASH:
         BUY -> risk -> simulated fill -> accounting
         COMMIT completed target
  -> reconcile fill journal against newly rebuilt PortfolioAccounting
```

SELL-before-BUY и промежуточный durable checkpoint не позволяют после restart
одновременно владеть старой и новой позицией из-за одного rotation event.

### 4.9 Risk evaluation

```text
StrategyDecision + SizingProposal + PortfolioState + timing
  -> ReplayRiskContext
  -> PaperRiskManager.check
       execution state known?
       manually paused?
       fresh candle/observation?
       open order count?
       order/position/exposure limits?
       daily loss / peak drawdown?
  -> RiskCheckResult
       accepted -> execution
       rejected -> no order/fill; telemetry diagnostic
```

SELL, уменьшающий позицию, обрабатывается отдельно от risk-increasing BUY, но
неизвестное/reconciliation-failed состояние блокирует исполнение fail-closed.

### 4.10 Order lifecycle, fill и accounting

```text
OrderIntent
  -> SimulatedExecutionAdapter.submit
  -> OrderLifecycle.create_order
       -> ReservationBook.reserve(quote for BUY / base for SELL)
  -> PaperExecutionSimulator or BacktestEngine creates Fill
  -> adapter.apply_fill
       -> duplicate fill: idempotent no-op; conflict/overfill: error
       -> consume reservation; partial/filled status
  -> only if fill_applied:
       PortfolioAccounting.record_buy|record_sell
       -> cash + FIFO lots + fee + realization + position cycle
       -> assert_invariants
```

Cash/position изменяются только после подтверждённого simulated fill. `OrderIntent`
и `Order` сами balances не меняют.

### 4.11 SQLite checkpoint and reconciliation

Single-symbol checkpoint содержит event history и execution/accounting snapshot;
recovery проверяет deterministic reconstruction. Multi-symbol reconciliation
сравнивает execution fill journal с заново построенным `PortfolioAccounting`, а
adapter snapshot round-trip использует как integrity check. Это локальная
reconciliation, не сверка с независимым exchange venue.

### 4.12 Logs, metrics и Telegram outbox

```text
financial COMMIT succeeds
  -> PaperTelemetry event/gauges/counters
       -> structured logger
       -> in-memory Prometheus registry / HTTP endpoint
       -> NotificationOutbox.enqueue (durable dedup row)
  -> NotificationOutbox.flush
       -> TelegramNotifier.send
       -> mark sent OR defer with exponential retry
```

Metrics и Telegram не входят в финансовую транзакцию. Их отказ не откатывает
fill/checkpoint. Telegram предоставляет at-least-once после durable enqueue:
crash после send, но до `mark sent`, может дать duplicate.

### 4.13 Backup and restore

```text
cryptolab paper backup
  -> verify_sqlite_database(source, read-only integrity/version check)
  -> sqlite3.Connection.backup(temp destination)
  -> verify temp
  -> atomic Path.replace(new output)

cryptolab paper restore
  -> verify backup
  -> require destination does not exist
  -> same SQLite backup API into a new path
  -> verify restored DB
```

Команды не заменяют активную базу и не меняют Compose. Выбор восстановленной
базы остаётся явной operator action.

## 5. Durable и in-memory state

| State | In-memory owner | Durable representation | Recovery source |
|---|---|---|---|
| Market candles | Lists returned by repository | `candles` | Market DB query |
| Backtest portfolio | `PortfolioAccounting`, lifecycle | Нет | Run starts from config |
| Replay events/decision | Replay session | Paper journal/checkpoint | Replay durable candle journal |
| Orders/fills/reservations | `SimulatedExecutionAdapter` | Paper normalized tables + snapshot | Deserialize + compare |
| Cash/FIFO lots/PnL | `PortfolioAccounting` | Checkpoint/accounting JSON or rebuilt fill journal | Deterministic replay/reconciliation |
| Risk counters/peaks/pause | `PaperRiskManager` | risk JSON + controls | Restore snapshot/control row |
| Pending rotation | `MultiSymbolPaperTrader` | Portfolio checkpoint | Restore checkpoint |
| Metrics | `PaperMetrics` | Не durable; counts primed from durable state | Recomputed/primed at start |
| Notifications | `NotificationOutbox` | `paper_notification_outbox` | Pending rows retried |

## 6. Архитектурные выводы

1. Финансовое ядро разумно отделено от strategies и CLI.
2. Один процесс, sync I/O и SQLite соответствуют часовому paper workload; async,
   очереди и микросервисы сейчас не нужны.
3. Наиболее сильная часть — explicit time semantics, Decimal, idempotent IDs,
   FIFO invariants и deterministic recovery tests.
4. Наиболее сложная часть — `BacktestEngine` и persistent/paper orchestration:
   много режимов выхода, lifecycle и duplicated single/multi control flow.
5. Главный growth risk — полная пересборка features и полного checkpoint на
   каждую свечу; это требует измерений до длительной эксплуатации.
6. Не следует переписывать accounting, order lifecycle или replay «с нуля»:
   существующие invariants и regression coverage ценнее косметического упрощения.
7. Расширять реальным venue, новыми strategies или distributed infrastructure до
   устранения SRE-блокеров и доказательства simulation correctness не следует.

Подробные риски и приоритеты находятся в
[`engineering-audit.md`](engineering-audit.md).
