# Персональная программа изучения CryptoLab по реальному коду

Цель программы — научиться самостоятельно объяснять execution path, проверять
изменения AI-агента, диагностировать отказ и безопасно менять систему. Это не
курс синтаксиса Python и не инструкция по live trading.

Рекомендуемый режим: отдельная учебная Git branch, временные SQLite-файлы через
`tmp_path`, network только через `httpx.MockTransport`. Рабочие `data/*.db` и
paper sessions не использовать. Общая оценка: **70–92 учебных часа**.

## Краткий порядок

| № | Модуль | Уровень | Часы | Результат |
|---:|---|---|---:|---|
| 1 | Package structure и entry point | Базовый | 3–4 | Проследить запуск `cryptolab` |
| 2 | CLI и `argparse` composition root | Базовый | 4–5 | Объяснить parsing/dispatch/exit codes |
| 3 | Domain models: dataclass, Enum, typing, Decimal | Базовый | 4–5 | Читать и проверять value objects |
| 4 | Functions, composition и dependency injection | Базовый+ | 3–4 | Подменять I/O безопасными fakes |
| 5 | HTTP, JSON и ingestion | Средний | 5–6 | Объяснить retries/pagination/errors |
| 6 | Validation и market SQLite | Средний | 5–6 | Проверить data integrity/migration |
| 7 | Candle analytics и FeatureBuilder | Средний | 4–5 | Доказать alignment/no-look-ahead |
| 8 | Strategy Protocol и decisions | Средний | 4–5 | Проследить BUY/SELL/HOLD event |
| 9 | Backtest execution | Средний+ | 5–6 | Объяснить next-open и stops |
| 10 | Orders, fills, sizing и FIFO | Продвинутый | 6–8 | Сверить accounting invariants |
| 11 | SQLite transactions и migrations | Продвинутый | 5–6 | Объяснить atomicity/crash boundary |
| 12 | Deterministic replay | Продвинутый | 5–6 | Сопоставить replay и batch |
| 13 | Persistent recovery | Продвинутый | 5–7 | Восстановить state from journal |
| 14 | Single-symbol paper service и risk | Продвинутый | 5–6 | Диагностировать polling/risk gate |
| 15 | Multi-symbol state machine | Продвинутый | 6–8 | Объяснить SELL→checkpoint→BUY |
| 16 | Exceptions, logs, metrics, Telegram, operations | Продвинутый | 5–7 | Провести incident trace |
| 17 | Pytest, Docker, CI, monitoring и backup | Продвинутый | 6–8 | Самостоятельно проверить release |

## Модуль 1. Package structure, imports и реальная точка входа

**Сложность:** базовая. **Предпосылки:** запуск shell-команд, Git status.
**Время:** 3–4 часа.

**Читать по порядку:**

1. `pyproject.toml:1-41`;
2. `src/cryptolab/__init__.py`;
3. `src/cryptolab/cli.py:94-153`;
4. `src/cryptolab/market_data/__init__.py`, `backtesting/__init__.py`,
   `strategies/__init__.py`, `paper/__init__.py`;
5. `tests/unit/test_package.py`.

**Ключевые API:** `[project.scripts]`, `cli.main`, package exports.
**Теория:** `src` layout, import resolution, `__init__.py`, public API, module vs
package, virtual environment, wheel entry point, call stack.

**Flow:** shell `cryptolab` → generated console script → `cryptolab.cli:main` →
parser → command handler.

**Контрольные вопросы:**

1. Почему `pythonpath=["src"]` помогает tests, но установленный wheel не зависит
   от него?
2. Что является public API пакета, а что лишь internal implementation?
3. Где создаётся exit code и кто превращает его в process status?
4. Почему package imports могут создать cycle даже при правильных каталогах?

**Практика:**

- построить вручную import tree для команды `quality`;
- в чистой временной venv сравнить `python -m cryptolab.cli --help` и console
  entry point (без установки новых версий зависимостей);
- написать в учебной заметке список composition roots.

**Проверка:** объяснить путь запуска без открытия editor search.
**Gate:** умеете назвать entry point, dispatcher и пять package boundaries.

## Модуль 2. CLI и `argparse`

**Сложность:** базовая. **Предпосылки:** модуль 1. **Время:** 4–5 часов.

**Читать:** `src/cryptolab/cli.py:98-155`, затем handlers `548-1158`, parser
`1160-1574`, shared arguments/validators `1577-2038`, CLI tests
`tests/unit/test_cli.py`, `test_cli_backtest.py`, `test_cli_research.py`.

**Ключевые API:** `_create_parser`, `_add_common_arguments`, `_run_collect`,
`_run_backtest`, `_run_paper`, parsing helpers.
**Теория:** subparsers, `type=`, default, mutually compatible flags,
`parser.error`, dependency construction, exception-to-exit-code boundary.

**Flow:** argv → Namespace → date/rate validation → repository/services →
formatter/stdout → integer exit code.

**Вопросы:**

1. Какие ошибки даёт argparse с code 2, а какие handler с code 1?
2. Почему CLI не должен считать fee или менять lots?
3. Где backward-compatible defaults превращаются в config objects?
4. Как проверить, что новый flag не изменяет baseline output?

**Практика:**

- составить таблицу всех command families, required/default arguments и exits;
- через `main([...])` в изолированном test воспроизвести invalid date/rate;
- поставить breakpoint после `parse_args` и проследить Namespace для backtest.

**Проверка:** существующие CLI tests и ручной `--help`.
**Gate:** способны добавить мысленный flag design без помещения domain logic в CLI.

## Модуль 3. Dataclasses, type hints, Enum и Decimal

**Сложность:** базовая. **Предпосылки:** Python functions/classes. **Время:** 4–5 ч.

**Читать:** `market_data/models.py`, `features/models.py`,
`strategies/models.py`, `execution/models.py`, `backtesting/models.py:1-310`,
`tests/unit/test_strategy_models.py`, `test_execution_models.py`.

**Ключевые типы:** `Candle`, `FeatureRow`, `Signal`, `StrategyDecision`,
`Instrument`, `OrderIntent`, `Order`, `Fill`, `BacktestConfig`.
**Теория:** `@dataclass(frozen=True)`, `__post_init__`, Enum identity, Protocol vs
concrete class, `MappingProxyType`, `Decimal` context/finite values, `None` types.

**Flow:** untrusted JSON → validated domain value → immutable signal/order/fill →
report.

**Вопросы:**

1. Почему money нельзя переводить во float?
2. Что frozen защищает, а что внутри nested mutable object не защищает?
3. Чем `Signal.SELL` отличается от `OrderSide.SELL`?
4. Почему diagnostics копируется в `MappingProxyType`?
5. Где optional Bybit fields semantically отличаются от zero?

**Практика:**

- вручную создать valid/invalid `StrategyDecision` и предсказать error;
- проверить Decimal `0.1+0.2` против float в REPL и описать влияние на fees;
- написать isolated test неизменяемости diagnostics.

**Проверка:** `test_strategy_models.py`, `test_execution_models.py`.
**Gate:** объясняете каждый field и invariant основных value objects.

## Модуль 4. Composition и dependency injection

**Сложность:** базовая+. **Предпосылки:** 1–3. **Время:** 3–4 часа.

**Читать:** `strategies/base.py`, `backtesting/sizing.py:1-240`,
`execution/adapter.py:60-120`, `market_data/collector.py:20-155`, constructors в
`paper/service.py:60-145`.

**Ключевые API:** `Strategy`, `PositionSizer`, `ExecutionAdapter`, `KlineClient`,
constructor injection clock/sleep/transport.
**Теория:** composition over inheritance, structural typing/Protocol, pure core vs
I/O shell, fake vs mock, dependency lifetime.

**Flow:** CLI selects concrete dependencies → service accepts interfaces → tests
inject controlled client/clock/sleep without changing domain code.

**Вопросы:**

1. Почему Protocol не создаёт runtime service сам?
2. Какая подмена является финансово опасной?
3. Чем fake clock лучше patching global time?
4. Где dependency injection упрощает fault testing?

**Практика:**

- нарисовать object graph `paper run-once`;
- реализовать только в учебном test minimal fake KlineClient;
- проследить lifetime HTTP client vs repository connection.

**Проверка:** test должен быть offline и deterministic.
**Gate:** умеете выбрать seam для test без добавления новой abstraction.

## Модуль 5. Public HTTP, JSON и historical ingestion

**Сложность:** средняя. **Предпосылки:** 1–4, HTTP basics. **Время:** 5–6 часов.

**Читать:** `market_data/http.py`, `client.py`, `bybit_client.py`, `parser.py`,
`bybit_parser.py`, `collector.py`, затем `test_http_retry.py`, `test_collector.py`,
`test_bybit_collector.py`.

**Ключевые API:** `RetryPolicy`, `get_public_json`, оба `get_klines`,
`HistoricalDataCollector.collect`, `CollectionResult/Failure`.
**Теория:** timeout, transient/permanent failure, exponential backoff/jitter,
rate-limit headers, pagination cursor, half-open range, chronological normalization,
idempotency, Protocol.

**Flow:** requested range → page request → raw JSON validation → Candle parsing →
closed-page validation → save → cursor advance → exact coverage check.

**Вопросы:**

1. Почему Binance 418 не retry-ится?
2. Почему Bybit page разворачивается в chronological order?
3. Как empty page отличается от legitimate no-data/listing boundary?
4. Когда retry создаёт больший вред?
5. Почему collector проверяет persisted coverage после downloads?

**Практика:**

- MockTransport: 503→success и 429 with Retry-After, записать delays;
- создать two-page Bybit fixture и вручную вычислить cursor;
- воспроизвести pagination stall и объяснить CollectionFailure fields.

**Проверка:** focused collector/http tests.
**Gate:** можете по log/result отличить transient network, bad schema, stall,
conflict и incomplete range.

## Модуль 6. Validation, quality и market SQLite

**Сложность:** средняя. **Предпосылки:** 3, 5, basic SQL. **Время:** 5–6 часов.

**Читать:** `market_data/validator.py`, `data_quality.py`,
`storage/sqlite_repository.py`, `docs/decisions/0002-data-integrity-and-release.md`,
`test_validator.py`, `test_data_quality.py`, `test_sqlite_repository.py`.

**Ключевые API:** `validate_candle[_timing]`, `validate_historical_candles`,
`DataQualityChecker.check`, `SQLiteRepository.init_schema/save/get_*`.
**Теория:** OHLC invariants, finite Decimal, alignment/continuity, PK, transactions,
parameterized SQL, schema version, non-destructive migration, conflict-retain.

**Flow:** Candle → validation → insert/equal/conflict → range read → exact grid
quality report.

**Вопросы:**

1. Почему conflict нельзя silently overwrite?
2. Чем duplicate отличается от conflicting same key?
3. Почему range `[start,end)` удобен для hourly grid?
4. Как `close_time` связывает interval и closed status?
5. Что гарантирует Python transaction context, а что — PK?

**Практика:**

- создать temp legacy v1 DB и пройти migration, сравнив rows;
- вручную повредить OHLC/close_time в temp DB и получить quality failure;
- создать malformed schema с wrong PK и оценить текущую verifier gap.

**Проверка:** inspect `PRAGMA table_info/index_list`, full focused tests.
**Gate:** можете доказать data isolation и объяснить safe migration/backup.

## Модуль 7. Candles, indicators и FeatureBuilder

**Сложность:** средняя. **Предпосылки:** Decimal/sequences. **Время:** 4–5 часов.

**Читать:** `analytics/returns.py`, `analytics/indicators.py`,
`features/models.py`, `features/builder.py`, `test_returns.py`,
`test_indicators.py`, `test_features.py`.

**Ключевые API:** return/volatility/drawdown functions, SMA/EMA/RSI/ATR/RVOL,
`FeatureBuilder.build`.
**Теория:** sliding window, SMA-seeded EMA, Wilder smoothing, sample variance,
warm-up `None`, alignment, prior-only denominator, availability time.

**Flow:** validated candles → aligned arrays → FeatureRow per candle.

**Вопросы:**

1. Почему volatility делит на `n-1` и не annualized?
2. Почему RVOL current volume не входит в свой average?
3. На каком index впервые доступны RSI14 и ATR14?
4. Почему `available_time` на 1 ms позже inclusive close?
5. Как future-candle mutation test обнаруживает look-ahead?

**Практика:**

- вручную рассчитать 3-period SMA/EMA и сравнить тестом;
- построить 21 candle и отметить все warm-up boundaries;
- изменить последнюю будущую candle и доказать неизменность prior rows.

**Проверка:** focused analytics/features tests.
**Gate:** можете вручную объяснить каждое поле одной `FeatureRow`.

## Модуль 8. Strategy interface и decision semantics

**Сложность:** средняя. **Предпосылки:** 7. **Время:** 4–5 часов.

**Читать:** `strategies/base.py`, `models.py`, `runner.py`, затем
`trend_following.py`, `mean_reversion.py`, `breakout.py`; после этого
`test_strategy_runner.py`, `test_strategies.py`.

**Ключевые API:** `Strategy.evaluate/minimum_history`, `make_decision`,
`StrategyRunner.run/evaluate_latest`, `_HistoryView`.
**Теория:** Protocol, event vs state, warm-up, read-only Sequence view, crossover,
event policy, diagnostic metadata, spot SELL semantics.

**Flow:** row prefix → warm-up/strategy rules → immutable decision → metadata
validation.

**Вопросы:**

1. Почему strategy не должна знать quantity/position?
2. Как `_HistoryView` предотвращает future access?
3. Чем continued condition отличается от new breakout event?
4. Почему oversold не равен гарантированному BUY?
5. Что произойдёт при inconsistent reference price?

**Практика:**

- трассировать один crossover на synthetic rows;
- написать audit strategy, записывающую видимые open times;
- создать invalid decision и проверить runner rejection.

**Проверка:** strategy/runner tests.
**Gate:** можете отделить market signal от execution/portfolio action.

## Модуль 9. Backtest execution и time semantics

**Сложность:** средняя+. **Предпосылки:** 8. **Время:** 5–6 часов.

**Читать:** `backtesting/models.py`, `engine.py:118-446`, далее exit methods
`1110-1465`, `formatter.py`, `test_backtest_engine.py`.

**Ключевые API:** `BacktestConfig`, `BacktestEngine.run`, `_execute_decision`,
stop/timed exit methods, `BacktestReport`.
**Теория:** signal vs execution timestamp, next-open, gap vs intrabar stop,
precedence, adverse slippage, close-mark equity, open position.

**Flow:** decision at candle close → next candle open intent/fill → optional stop
path → close equity point → report invariants.

**Вопросы:**

1. Почему last BUY остаётся unexecuted?
2. Почему strategy SELL имеет priority над same-candle stop?
3. Как high watermark avoids look-ahead?
4. Что означает trailing intrabar timestamp?
5. Почему open position не закрывается в конце?

**Практика:**

- вручную посчитать one BUY/SELL с fee/slippage;
- воспроизвести gap stop и intrabar stop на synthetic rows;
- сравнить disabled stop с baseline report object.

**Проверка:** backtest/formatter tests.
**Gate:** можете восстановить timestamp/price каждого fill без догадки.

## Модуль 10. Position sizing, orders, fills и FIFO accounting

**Сложность:** продвинутая. **Предпосылки:** 3, 9. **Время:** 6–8 часов.

**Читать:** `backtesting/sizing.py`, `execution/models.py`,
`execution/lifecycle.py`, `execution/adapter.py`, `backtesting/accounting.py`,
engine `_buy/_sell/_settle_sell`, соответствующие five test modules.

**Ключевые API:** sizers, `OrderIntent`, `ReservationBook`, `OrderLifecycle`,
`PortfolioAccounting.record_buy/record_sell/assert_invariants`.
**Теория:** allocation vs fill, reservation, partial fill, terminal transition,
idempotency, FIFO cost basis, proportional fees, realized/unrealized PnL.

**Flow:** signal → sizing proposal → intent → reservation/order → fill → accounting
→ lot realization/cycle.

**Вопросы:**

1. Почему balance меняется только после fill?
2. Как entry fee распределяется при partial FIFO sell?
3. Чем duplicate fill отличается от conflicting duplicate?
4. Какая сумма резервируется для BUY/SELL?
5. Докажите `total PnL = realized + unrealized`.

**Практика:**

- вручную провести два BUY и partial SELL по FIFO;
- создать overfill/duplicate/cancel sequence и предсказать state;
- проверить invariants после каждого transition в test.

**Проверка:** accounting/order/adapter/sizing tests.
**Gate:** сводите cash, lots, fees и PnL до последнего Decimal.

## Модуль 11. SQLite transactions, schema versions и migrations

**Сложность:** продвинутая. **Предпосылки:** 6, 10. **Время:** 5–6 часов.

**Читать:** `storage/paper_state_repository.py:1-310`, commit paths
`311-700,946-1245`, migrations `1493-1636`, persistent migration tests.

**Ключевые API:** `init_schema`, `commit_candle`, portfolio commit/load,
control/outbox methods.
**Теория:** `BEGIN IMMEDIATE`, atomic commit/rollback, WAL vs rollback journal,
foreign keys, normalized tables + snapshot, schema/serialization version,
single-writer assumption.

**Flow:** journal insert + derived state + checkpoint within one transaction;
post-commit operations outside it.

**Вопросы:**

1. Где точная durable boundary?
2. Что остаётся после crash before/after COMMIT?
3. Зачем одновременно normalized rows и checkpoint JSON?
4. Почему migration не должна rebuild user DB destructively?
5. Какие schema properties текущий verifier не проверяет?

**Практика:**

- inspect temp DB tables/PK/FK/user_version;
- удержать transaction и исследовать second writer behavior;
- пройти v1→v4 migration на fixture и сверить counts.

**Проверка:** persistence/Stage migration tests; original temp DB checksum.
**Gate:** объясняете atomicity и recovery без фразы «SQLite сама всё делает».

## Модуль 12. Deterministic event replay

**Сложность:** продвинутая. **Предпосылки:** 7–10. **Время:** 5–6 часов.

**Читать:** `replay/models.py`, `replay/risk.py`, `replay/runner.py`,
`backtesting/engine.py:2019-2327`, `test_deterministic_replay.py`.

**Ключевые API:** `DeterministicReplayClock`, runner/session,
`process_candle`, `process_observed_candle`, `BacktestExecutionSession`.
**Теория:** event time vs wall time, deterministic IDs/order, pending decision,
exactly-once effect via idempotency, replay oracle.

**Flow:** candle open → previous execution → close → feature → new decision →
snapshot.

**Вопросы:**

1. Почему decision current candle не исполняется на её open?
2. В каком порядке clock advances?
3. Как duplicate event становится no-op?
4. Почему replay reuses engine kernel?
5. Чем observed-after-close mode отличается от batch?

**Практика:**

- записать event sequence для трёх candles;
- повторить identical/conflicting event IDs;
- сравнить full replay report с batch report.

**Проверка:** deterministic equality tests.
**Gate:** можете предсказать event, timestamp и portfolio snapshot по шагам.

## Модуль 13. Persistent replay и crash recovery

**Сложность:** продвинутая. **Предпосылки:** 11–12. **Время:** 5–7 часов.

**Читать:** `replay/persistence_models.py`, `serialization.py`,
`persistent.py`, repository commit/load helpers, `test_persistent_replay.py`.

**Ключевые API:** `PersistentReplayRunner.create/recover`, persistent session,
`CrashPoint`, typed integrity/configuration errors.
**Теория:** write-ahead event journal, canonical serialization, fail-closed,
deterministic reconstruction, uncertain outcome retry, crash consistency.

**Flow:** process event in memory → transaction → commit/no commit → on restart
replay journal → compare checkpoint/risk/lifecycle.

**Вопросы:**

1. Почему post-commit crash требует retry того же event?
2. Как system отличает duplicate from conflict?
3. Что делает session unavailable после failed persistence?
4. Почему changed config blocks recovery?
5. Что reconciliation действительно доказывает?

**Практика:**

- пройти все CrashPoint states на 3-candle fixture;
- изменить one serialized Decimal/event и получить integrity failure;
- измерить recovery time на 100/1000 synthetic candles.

**Проверка:** recovered report equals continuous exactly.
**Gate:** способны провести incident reconstruction по DB journal.

## Модуль 14. Single-symbol paper loop и risk controls

**Сложность:** продвинутая. **Предпосылки:** 5, 10, 13. **Время:** 5–6 часов.

**Читать:** `paper/models.py:1-163`, `market.py`, `risk.py`, `execution.py`,
`service.py`, `test_paper_stage3.py`.

**Ключевые API:** `PaperBotConfig`, `PaperMarketSource`, `PaperRiskManager`,
`PaperExecutionSimulator`, `ContinuousPaperTrader.run_once/run_forever`.
**Теория:** polling, freshness, backfill vs live, pause, risk-increasing order,
actual observation timestamp, graceful shutdown, failure isolation.

**Flow:** closed range → exact validation → backfill no trade → latest fresh
ticker → strategy/sizing/risk → simulated fill → durable checkpoint.

**Вопросы:**

1. Почему backfilled signals не торгуются?
2. Что делает ticker observation valid?
3. Какие risk gates блокируют BUY и почему?
4. Что service делает после failed commit?
5. Почему текущий sleep не полностью graceful под Compose?

**Практика:**

- controlled clock: stale vs fresh latest candle;
- manual pause test с продолжающимся candle processing;
- external stop event during long wait test design.

**Проверка:** Stage3 focused tests и paper status on temp DB.
**Gate:** можете диагностировать no-fill как data, signal, sizing, risk или execution.

## Модуль 15. Multi-symbol state machine и reconciliation

**Сложность:** продвинутая. **Предпосылки:** 10, 13–14. **Время:** 6–8 часов.

**Читать:** `strategies/relative_momentum.py`, `paper/models.py:164-290`,
`paper/portfolio_service.py`, `backtesting/portfolio.py`,
`test_relative_momentum.py`, `test_paper_stage4.py`.

**Ключевые API:** planner/reviews/leg strategy, `PendingRotation`,
`MultiSymbolPaperTrader._execute_pending/reconcile`, shared `PortfolioAccounting`.
**Теория:** synchronized streams, explicit state machine, deterministic ordering,
shared quote capital, saga-like SELL/checkpoint/BUY, local reconciliation.

**Flow:** weekly review → target changed → pending SELL → fill/accounting/COMMIT →
pending BUY → fill/accounting/COMMIT → rebuild from fill journal.

**Вопросы:**

1. Почему SELL должен быть durable до BUY?
2. Что произойдёт при crash между legs?
3. Как гарантируется one active asset?
4. Почему status currently writes?
5. Почему local fill-journal reconciliation не равна venue reconciliation?

**Практика:**

- нарисовать все `RotationPhase` transitions;
- inject crash after SELL checkpoint and recover;
- создать missing/misaligned ETH candle set и проверить fail-closed.

**Проверка:** Stage4 tests + accounting invariants.
**Gate:** объясняете shared cash и every restart state без «магии».

## Модуль 16. Exceptions, logging, metrics, Telegram и operations

**Сложность:** продвинутая. **Предпосылки:** 13–15. **Время:** 5–7 часов.

**Читать:** `paper/observability.py`, `notifications.py`, `operations.py`, CLI paper
handler `156-540`, `docs/paper-bot-stage-5.md`, `paper-bot-operations.md`,
`test_paper_stage5.py`.

**Ключевые API:** typed replay/market failures, `JsonLogFormatter`, `PaperMetrics`,
`PaperTelemetry`, `NotificationOutbox`, verify/health/backup/restore.
**Теория:** error taxonomy, structured log context, counter vs gauge, downstream
telemetry, at-least-once delivery, dedup key, retry/backoff, readiness/liveness,
RPO/RTO.

**Flow:** financial commit → telemetry → durable outbox → Telegram attempt;
operator health/backup uses read-only source and new destination.

**Вопросы:**

1. Почему Telegram failure не откатывает fill?
2. Где duplicate notification всё ещё возможен?
3. Какие metrics восстанавливаются, а какие теряются?
4. Почему structural health не доказывает progress?
5. Что current backup verifier не проверяет?

**Практика:**

- MockTransport failure→defer→restart→success;
- создать stale checkpoint и оценить current health result;
- повредить temp backup schema при valid integrity/version и проверить gap.

**Проверка:** Stage5 tests и ручной audit generated logs without token.
**Gate:** умеете провести fail/commit/notify timeline и назвать delivery guarantee.

## Модуль 17. Pytest, Docker, CI, monitoring и backup drill

**Сложность:** продвинутая. **Предпосылки:** все предыдущие. **Время:** 6–8 часов.

**Читать:** `tests/conftest.py`, representative tests каждого слоя,
`Dockerfile`, `compose.yaml`, `deploy/prometheus-paper.yml`, `.github/workflows/*`,
`requirements/constraints.txt`, README testing/build section.

**Ключевые инструменты:** pytest fixtures/parametrize/monkeypatch/MockTransport,
temp DB, crash injection, Ruff, compileall, Docker health/log/resource options,
GitHub Actions matrices, SQLite backup API.
**Теория:** unit vs integration/contract/soak, deterministic fixture, test oracle,
fault injection, clean-wheel install, supply-chain pinning, container signals,
external monitoring and restore drills.

**Flow:** source → offline tests/static/compile → wheel → clean install/CLI smoke →
container → health/metrics → backup/restore verification.

**Вопросы:**

1. Что autouse network fixture реально блокирует?
2. Как test может «проверять mock», но не behavior?
3. Почему full suite не доказывает VPS readiness?
4. Какие public CLI commands CI сейчас пропускает?
5. Как доказать backup recoverability, а не только existence?

**Практика:**

- написать fault test, не asserting internal mock calls;
- локально выполнить release checklist и записать actual outputs;
- провести restore drill только на temp paths, сравнив schema/session/checkpoint.

**Проверка:** full commands из `AGENTS.md`, clean-wheel smoke, Compose config/build
если Docker доступен; hosted CI status проверять отдельно.
**Gate:** способны самостоятельно дать честный PASS/FAIL release assessment и
отделить локально проверенное от предположений.

## Итоговый критерий самостоятельности

Программа завершена, когда инженер без подсказки может:

1. проследить candle от HTTP до durable fill и report;
2. доказать отсутствие future data для конкретного decision;
3. вручную свести FIFO lot, fee, cash, realized/unrealized PnL;
4. объяснить состояние до/после каждого crash boundary;
5. отличить strategy inactivity, risk rejection, execution rejection и persistence
   failure;
6. безопасно воспроизвести incident на temp DB;
7. проверить AI diff по invariants, tests, compatibility и deployment effects;
8. отказаться от изменения, если финансовая семантика неоднозначна.
