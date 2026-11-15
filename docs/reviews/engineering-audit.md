# CryptoLab: engineering, financial-correctness и SRE audit

Дата: 2026-11-15
Ревизия: `35083da` (`main`)
Scope: фактический код, tests, CI, Docker/Compose и Stage 1–5 documentation.

## 1. Методика и обозначения

- **CONFIRMED** — поведение прямо следует из прочитанного кода или
  воспроизводится существующей проверкой.
- **SUSPECTED** — правдоподобный риск, но для утверждения о дефекте нужен
  benchmark, contention/fault test либо реальный runtime.
- **NOT VERIFIED** — область невозможно подтвердить локальным чтением/тестами,
  например hosted CI, VPS kernel/storage/network или Telegram availability.

Severity оценивает возможный ущерб, а не качество автора:

- **Critical** — вероятная потеря/искажение финансового состояния или нарушение
  safety boundary без разумного обхода;
- **High** — блокирует надёжный unattended VPS deployment;
- **Medium** — заметный operational/maintenance risk, но контролируемый;
- **Low** — качество, ясность или hardening.

Отсутствие live trading не является дефектом. Наоборот, offline-only execution —
правильная текущая security boundary.

## 2. Executive summary

CryptoLab уже сильнее типичного учебного backtester: Decimal accounting,
look-ahead-safe time model, order/fill separation, FIFO lots, atomic SQLite
checkpoints, idempotent event IDs, crash injection и network-blocked tests
реализованы последовательно. Existing strategies и execution assumptions не
следует менять в рамках эксплуатационного hardening.

Для первого unattended VPS deployment выявлены пять обоснованных блокеров:

1. poll sleep не прерывается stop event, а Compose даёт только 30 секунд;
2. backup/health schema verification принимает неполную или malformed schema;
3. долгосрочная стоимость replay/checkpoint растёт как минимум квадратично;
4. healthcheck не проверяет freshness/progress и не обеспечивает внешний alert;
5. Docker build не фиксирует base digest и не использует repository constraints.

Critical-дефектов и evidence реального нарушения accounting на проверенной
ревизии не обнаружено. Это не означает, что стратегии прибыльны или что paper
fill model эквивалентна реальной бирже.

## 3. Top 10 engineering risks

| # | Severity | Status | Риск | VPS blocker |
|---:|---|---|---|:---:|
| 1 | High | CONFIRMED | SIGTERM может закончиться принудительным kill во время 60s sleep при 30s grace | Да |
| 2 | High | CONFIRMED | Backup verifier проверяет только две таблицы/версию, не полный schema contract | Да |
| 3 | High | CONFIRMED | Full feature rebuild + full checkpoint sync/recovery replay создают O(n²)-рост работы | Да, пока не измерено/ограничено |
| 4 | High | CONFIRMED | Healthcheck может быть green при давно остановившемся progress; внешних alert rules нет | Да |
| 5 | High | CONFIRMED | Container build зависит от mutable image tag и unconstrained dependency resolution | Да для воспроизводимого release |
| 6 | Medium | CONFIRMED | Market schema v2 «принимается» по именам columns без PK/type/constraint verification | Нет, но проверить DB до deployment |
| 7 | Medium | CONFIRMED | Multi-symbol `status` изменяет durable state через reconciliation commit | Нет |
| 8 | Medium | CONFIRMED | `Retry-After` может обойти configured maximum delay и надолго остановить single process | Нет |
| 9 | Medium | CONFIRMED | Sent outbox rows не очищаются; burst фактически отправляется по одному item на flush | Нет |
| 10 | Medium | CONFIRMED | CI smoke list не включает optimize/signals-research/paper; Actions не pinned by SHA | Нет |

## 4. Детальные findings

### F-01 — непрерываемый poll sleep конфликтует с Docker grace period

- **Severity / status:** High / CONFIRMED.
- **Код:** `src/cryptolab/paper/service.py:252-305`, особенно 298-299;
  `src/cryptolab/paper/portfolio_service.py:309-347`, особенно 343-344;
  `src/cryptolab/cli.py:1577-1627`; `compose.yaml:7-10`.
- **Доказательство:** `run_forever()` проверяет `Event`, но затем вызывает
  injected `time.sleep(poll_seconds)`. Default poll — 60s; Compose sends SIGTERM
  and waits 30s. Signal handler только устанавливает event и не прерывает sleep.
- **Сценарий:** SIGTERM приходит сразу после начала 60-second sleep. Python
  handler returns, sleep продолжается; Docker sends SIGKILL at 30s.
- **Последствия:** `finally` может не выполнить telemetry stop/close; SQLite
  transaction не обязана быть повреждена, но shutdown не является заявленно
  graceful, а operator получает неоднозначность.
- **Исправление:** ждать через `stop.wait(poll_seconds)` (при сохранении injected
  wait abstraction для tests) и установить grace, покрывающий worst-case HTTP
  timeout/retry plus close. Не полагаться только на больший grace.
- **До VPS:** да.
- **Test:** запустить loop в thread с большим poll, выставить event и проверить
  завершение существенно раньше poll/grace; отдельно SIGTERM container test.
- **Coverage gap:** текущий `test_continuous_loop_honors_graceful_stop` заменяет
  sleep callback функцией, которая сама ставит event; внешнее прерывание реального
  ожидания не проверяется (`tests/unit/test_paper_stage3.py:221-236`).

### F-02 — backup verification не валидирует полный schema contract

- **Severity / status:** High / CONFIRMED.
- **Код:** `src/cryptolab/paper/operations.py:38-77`, `138-178`;
  для сравнения ожидаемый список paper tables находится в
  `src/cryptolab/storage/paper_state_repository.py:246-309`.
- **Доказательство:** verifier выполняет `integrity_check`, читает
  `user_version`, а paper DB распознаёт только по наличию `paper_sessions` и
  `paper_checkpoints`. Columns, PK/UNIQUE/FK и остальные v4 tables не проверяются.
  Market DB распознаётся по одному `candles`.
- **Сценарий:** из v4 DB удалена `paper_fills` или колонка checkpoint, но version
  осталась 4. `verify-backup` и `backup_sqlite` возвращают success; restore drill
  создаёт структурно непригодную базу.
- **Последствия:** ложная уверенность в recoverability; incident restore fails
  только после переключения на backup.
- **Исправление:** единый read-only schema manifest с table columns, PK/indexes и
  critical constraints для market/paper schemas. Repository init и operations
  должны использовать один validator без destructive migration в verify mode.
- **До VPS:** да.
- **Test:** удалить/переименовать каждую critical table/column, изменить PK,
  оставить correct `user_version`; verify/backup/restore должны fail before output.

### F-03 — incremental paper path пересчитывает и синхронизирует всю историю

- **Severity / status:** High / CONFIRMED архитектурный рост; фактическая
  latency на целевом VPS — NOT VERIFIED.
- **Код:** `src/cryptolab/replay/runner.py:316-344`;
  `src/cryptolab/features/builder.py:25-86`;
  `src/cryptolab/replay/persistent.py:172-227`;
  `src/cryptolab/storage/paper_state_repository.py:371-452,1137-1244` и далее.
- **Доказательство:** на каждой новой candle `FeatureBuilder.build(self._candles)`
  вычисляет весь prefix. Checkpoint содержит growing sequences; `_write_state`
  снова формирует/syncs все events, decisions, orders и snapshots. Recovery
  последовательно replay-ит весь journal, а каждый replay step снова builds full
  prefix. При N candles суммарная работа имеет квадратичную компоненту.
- **Сценарий:** многомесячный/многолетний hourly service регулярно рестартует;
  startup и commit latency растут до пропуска polling windows или watchdog timeout.
- **Последствия:** stale data, delayed decisions, extended restart, high SQLite
  write amplification/disk growth.
- **Исправление:** сначала benchmark N=1k/10k/25k с commit/recover timings и DB
  growth. Затем минимально: incremental indicator state или bounded warm-up
  history, append-only new derived rows, periodic compact checkpoint + journal
  tail. Сохранить deterministic replay oracle для проверки equivalence.
- **До VPS:** да — либо исправить, либо измерить и установить documented maximum
  session length/rotation policy, достаточный для planned uptime.
- **Test:** performance regression budget и continuous-vs-incremental equivalence;
  restart from compact checkpoint produces byte-equivalent financial state.

### F-04 — healthcheck не измеряет progress/freshness и не является watchdog

- **Severity / status:** High / CONFIRMED.
- **Код:** `src/cryptolab/paper/operations.py:80-135`;
  `compose.yaml:45-60`; `deploy/prometheus-paper.yml:1-8`.
- **Доказательство:** health проверяет schema, presence checkpoint и multi
  `reconciled`; timestamp не сравнивается с current time/interval. Docker health
  не перезапускает running-but-unhealthy container сам по себе. Prometheus file
  содержит scrape job, но не alert rules.
- **Сценарий:** process остаётся жив, каждый cycle получает network error или
  зависает вне финансовой транзакции. Старый reconciled checkpoint продолжает
  давать healthy. Telegram внутри того же процесса может не доставить alert.
- **Последствия:** часы/дни без новых candles/decisions при green health history.
- **Исправление:** разделить structural health и readiness/progress; учитывать
  last candle/checkpoint age с interval-aware grace; external Prometheus alerts
  на target `up`, paper process gauge, data lag, checkpoint age, reconciliation,
  outbox age. Указать owner/escalation в runbook.
- **До VPS:** да для unattended operation.
- **Test:** fixed clock + stale checkpoint => readiness nonzero; fresh empty-start
  semantics отдельно; alert rules validate through `promtool` when available.

### F-05 — Docker image build не полностью воспроизводим

- **Severity / status:** High / CONFIRMED.
- **Код:** `Dockerfile:1-23`; `requirements/constraints.txt`; `pyproject.toml:1-26`.
- **Доказательство:** `python:3.13-slim` — mutable tag в обоих stages;
  `pip wheel .` не получает `-c requirements/constraints.txt`, а constraints даже
  не copied. Runtime wheel dependencies могут разрешиться иначе в разные дни.
- **Сценарий:** rebuild того же Git commit получает новый base OS или httpx
  transitive versions; поведение/уязвимости меняются без source diff.
- **Последствия:** incident rollback и audit нельзя гарантированно воспроизвести.
- **Исправление:** pin base image digest (с управляемым update process), copy/use
  constraints или hashes/lock для wheelhouse, record image digest/SBOM. Не хранить
  secrets в image.
- **До VPS:** да для release/deploy artifact.
- **Test:** build twice from clean cache, compare installed package/version list
  and artifact provenance; container CLI/health smoke tests.

### F-06 — market schema adoption проверяет только набор columns

- **Severity / status:** Medium / CONFIRMED.
- **Код:** `src/cryptolab/storage/sqlite_repository.py:89-132`.
- **Доказательство:** если `exchange` и `market_type` существуют и все required
  names присутствуют, repository выставляет `user_version=2`. Не проверяются
  composite PK, NOT NULL, SQLite types или column order/constraints.
- **Сценарий:** вручную созданная/malformed unversioned table имеет нужные names,
  но PK только `open_time` либо отсутствует; repository «adopts» её.
- **Последствия:** exchange isolation/duplicate guarantee не обеспечена schema,
  collection получает conflicts или потерю rows.
- **Исправление:** validate `PRAGMA table_info`, PK ordinal и indexes before adopt;
  unsupported schema fail non-destructively with backup instructions.
- **До VPS:** не блокирует новую чистую DB; существующую deployment DB надо
  один раз строго проверить.
- **Test:** required columns with wrong composite PK/type/nullability must fail
  without changing `user_version` or rows.

### F-07 — multi-symbol status является write operation

- **Severity / status:** Medium / CONFIRMED.
- **Код:** `src/cryptolab/paper/portfolio_service.py:162-192,349-380`.
- **Доказательство:** `start()` уже делает `reconcile(persist=True)`, после чего
  `status()` снова вызывает `reconcile(persist=True)`. `_commit_state` advances
  durable revision/reconciliation history.
- **Сценарий:** monitoring/operator регулярно вызывает status параллельно bot;
  каждое чтение создаёт writes и может конкурировать с writer.
- **Последствия:** лишний journal/database growth, lock contention, удивляющая
  non-read-only semantics.
- **Исправление:** status использует уже полученный report или
  `reconcile(persist=False)`; отдельная explicit integrity command может persist.
  Обновить docs только если write semantics намеренно сохраняется.
- **До VPS:** нет, если status не используется как frequent probe.
- **Test:** DB revision/reconciliation row count до и после status остаётся тем же;
  integrity command behavior проверяется отдельно.

### F-08 — untrusted `Retry-After` не ограничен maximum delay

- **Severity / status:** Medium / CONFIRMED.
- **Код:** `src/cryptolab/market_data/http.py:154-181,184-218`.
- **Доказательство:** exponential part capped, затем
  `max(exponential_delay, minimum_delay)` принимает arbitrary server value.
  `float("inf")` также проходит parser и приводит к unbounded sleep.
- **Сценарий:** ошибочный/proxy-injected `Retry-After: 86400` или `inf` блокирует
  collect/paper public request path значительно дольше configured maximum.
- **Последствия:** process кажется зависшим; data freshness нарушается.
- **Исправление:** отдельный documented maximum server-directed delay и finite
  validation. Для Binance 418 продолжать immediate fail; не превращать rate-limit
  handling в aggressive retry.
- **До VPS:** желательно, но external progress alert ограничивает ущерб.
- **Test:** huge, negative, non-finite, HTTP-date и malformed headers; recorded
  sleep remains finite and bounded.

### F-09 — outbox retention и burst-drain не имеют capacity policy

- **Severity / status:** Medium / CONFIRMED.
- **Код:** `src/cryptolab/paper/notifications.py:90-177`;
  `src/cryptolab/storage/paper_state_repository.py:190-203,1127-1135`.
- **Доказательство:** sent rows не purged. В `flush()` значение `now` фиксировано;
  после первого success `latest=now`, поэтому при positive minimum interval loop
  прекращает текущий batch. При poll 60s backlog обычно drains по одному/cycle,
  несмотря на default `max_batch=5`.
- **Сценарий:** Telegram недоступен несколько часов и накоплено много events;
  после recovery backlog остаётся надолго, DB растёт бесконечно.
- **Последствия:** delayed alerts, потеря их operational value, growth paper DB.
- **Исправление:** явно выбрать desired rate semantics; добавить pending count,
  oldest age, failure metrics/alerts; documented retention/archive/purge для sent
  rows после backup. Не делать delivery частью financial commit.
- **До VPS:** нет при малом event volume, но monitoring backlog нужен.
- **Test:** fixed clock burst, restart, rate limiting, retention boundary and
  duplicate delivery window.

### F-10 — CI coverage и supply-chain hardening отстают от CLI

- **Severity / status:** Medium / CONFIRMED.
- **Код:** `.github/workflows/ci.yml:18-38,40-65,67-80`;
  `.github/workflows/security.yml:13-35`; `src/cryptolab/cli.py:98-150`.
- **Доказательство:** smoke loop перечисляет шесть historic commands, но не
  `optimize`, `signals-research` и nested `paper`. Actions pinned major tags, а не
  immutable commit SHA. Ruff configured only `E9,F` (`pyproject.toml:36-41`).
- **Сценарий:** packaging/parser regression ломает paper command, но wheel job
  остаётся green; compromised/mutated action tag меняет CI behavior.
- **Последствия:** release false positive и supply-chain exposure.
- **Исправление:** derive/maintain complete smoke matrix including paper help and
  offline health fixtures; pin actions SHA with update automation; постепенно
  расширить Ruff, не делая mass reformat unrelated code.
- **До VPS:** paper CLI smoke обязателен; full lint expansion можно отложить.
- **Test:** clean-wheel smoke для каждой public command family.

### F-11 — SQLite concurrency policy зависит от defaults

- **Severity / status:** Medium / SUSPECTED.
- **Код:** `src/cryptolab/storage/paper_state_repository.py:371-452,1485-1491`;
  operator commands в `src/cryptolab/cli.py` и `paper/operations.py`.
- **Доказательство:** design предполагает one writer, transactions используют
  `BEGIN IMMEDIATE`, но explicit `busy_timeout`, WAL policy и writer lock/lease
  отсутствуют. Python sqlite default timeout может скрыть или задержать collision.
- **Сценарий:** bot commit совпадает с pause/status/integrity/backup на медленном
  VPS disk.
- **Последствия:** transient `database is locked`, cycle failure/recovery;
  вероятнее availability issue, чем corruption.
- **Исправление:** сначала contention test и documented single-writer operator
  procedure; затем bounded busy timeout/lock strategy, если измерение подтвердит.
- **До VPS:** documented single-writer discipline — да; code change — только
  после проверки.
- **Test:** two connections, held `BEGIN IMMEDIATE`, timed operator command and
  subsequent recovery; assert fail-closed/no partial state.

### F-12 — architecture documentation содержит устаревшее deployment statement

- **Severity / status:** Low / CONFIRMED.
- **Код:** `docs/architecture.md:322-354`, особенно 329; одновременно существуют
  `Dockerfile`, `compose.yaml`, `docs/paper-bot-stage-5.md`.
- **Доказательство:** architecture doc говорит, что Docker/service deployment не
  требуется и перечисляет no authenticated paper adapter limitation из более
  раннего release context; Stage5 уже документирует continuous paper deployment.
- **Сценарий:** новый инженер выбирает неверную operational model.
- **Последствия:** confusion, неверный review scope/runbook.
- **Исправление:** после согласования аудита обновить canonical architecture doc
  или явно разделить «v1.0 historical CLI» и «Stage5 paper runtime».
- **До VPS:** нет, если Stage5 runbook объявлен authoritative.
- **Test:** docs link/command smoke; automated prose verification не требуется.

## 5. Trading correctness review

### Подтверждённые сильные гарантии

- `FeatureRow.available_time = close_time + 1`; StrategyRunner validates exact
  interval grid and decision metadata (`strategies/runner.py:168-250`).
- `_HistoryView` даёт read-only prefix without future rows.
- Breakout thresholds и RVOL используют prior data; warm-up values explicit.
- Batch execution uses previous decision on current next open. Last actionable
  decision has no invented fill and becomes `SkippedDecision`.
- Strategy signal и order intent/quantity разделены.
- Pending strategy SELL имеет open precedence над intrabar stops; current high
  updates trailing watermark only after stop check.
- Fee/slippage use Decimal. Entry fee included in FIFO cost basis; exit fee
  reduces proceeds. `PortfolioAccounting.assert_invariants()` and engine report
  reconciliation verify cash/lots/PnL/fees.
- Duplicate fills idempotent; conflicting duplicate/overfill rejected before
  accounting mutation. Reservation release is tested.
- Shared portfolio settles SELL before BUY at the same execution open.
- Open position is marked at last close and is never artificially sold.

### Intentional limitations, not defects

- OHLC cannot determine intrabar path; stop model chooses explicit gap/open or
  stop-price behavior but is not order-book simulation.
- Fixed fee/slippage are assumptions; no lot-size/min-notional/partial venue fill
  realism beyond configured simulator.
- Maximum equity drawdown uses close marks, not intrabar lows.
- Paper execution observes a ticker after close and is not historical next-open.
- Reconciliation is against the local fill journal, not independent exchange
  account state.
- Strategies are research hypotheses and have not demonstrated independent
  profitability. Inactivity is not success.

### Не подтверждено

- Bit-for-bit equivalence с user-owned long historical reports не запускалась в
  этом audit, потому что report artifacts не являются canonical source fixture.
- Numerical sensitivity при custom low Decimal context не исследована как
  property; один targeted regrouping test существует.
- Реальная exchange latency, spread, market impact и API availability не могут
  быть подтверждены offline.

## 6. Persistence and recovery review

### Хорошо реализовано

- `BEGIN IMMEDIATE` + single transaction journal/derived state/checkpoint.
- Deterministic event IDs and conflicting duplicate detection.
- Crash injection at defined pre/post commit points and continuous-vs-recovered
  report equality.
- Configuration identity check prevents recovery under changed strategy/runtime.
- Canonical JSON preserves Decimal as strings.
- Multi-symbol SELL checkpoint precedes BUY, so restart resumes a known phase.
- Existing schema migrations are additive/transactional and retain rows.
- Backup uses SQLite online backup API and never overwrites destination.

### Ограничения

- Full history replay is correctness-oriented but unbounded (F-03).
- Schema verification is table-name-oriented, not structural (F-02/F-06).
- No independent venue truth exists by design.
- One-writer assumption is documented implicitly rather than enforced (F-11).

## 7. Python architecture quality

### Separation/cohesion

Хорошо: strategies, sizing, lifecycle и accounting имеют разные models and
responsibilities; Protocols permit injection; most value objects are frozen.
Сложно: `BacktestEngine` (более 2k lines), `PaperStateRepository` (более 1.6k)
и `MultiSymbolPaperTrader` (более 1.3k) концентрируют много orchestration paths.
Их нельзя механически разделять: сначала characterization tests и stable seams.

### Dependency direction

Conceptual layers понятны, но package-level cycles `market_data ↔ storage` и
`replay ↔ storage` усложняют reuse. В будущем neutral persistence DTO/ports могли
бы уменьшить coupling, но сейчас это debt, не deployment blocker.

### Typing/dataclasses/Decimal

Используются consistently. Frozen dataclasses и MappingProxy diagnostics уменьшают
accidental mutation. Runtime validation подробна. Недостаток: type checker в CI
отсутствует; добавлять его следует постепенно после baseline, не как mass cleanup.

### Sync/async

Sync stack оправдан: hourly candles, один process, SQLite и простой polling.
Async не исправляет F-03/F-04 и добавит cancellation/transaction complexity.

### Error handling

Market errors классифицированы; paper financial integrity failures stop
fail-closed, transient cycle errors rebuild from checkpoint. CLI возвращает
nonzero statuses. Улучшить нужно bounded server wait, progress health и explicit
operator error taxonomy, но новый logging framework не нужен.

## 8. SRE and security review

### Положительные свойства

- Container runs non-root, root filesystem read-only, `/tmp` tmpfs, metrics bound
  to loopback, logs and resources bounded.
- No API secrets in repository; Telegram optional via environment.
- Tests block live sockets globally.
- Telemetry is downstream of financial commits, so monitoring failure не меняет
  balances.
- Backups/restores non-destructive and documented.
- Binance 418 immediately stops; 429/5xx/timeouts bounded by attempts.

### Hardening backlog

- Add `cap_drop: ALL`, `security_opt: no-new-privileges:true` where compatible.
- Prefer Docker secrets/read-only secret files over plain env for future secrets;
  Telegram token в environment виден privileged host operators.
- Define host filesystem backup retention/encryption/off-host copy and perform an
  actual restore drill.
- Add external alert rules and owner/escalation.
- Define image update/rollback procedure and scan built image, not only Python env.

## 9. Testing audit

### Existing coverage

- Offline safeguard in `tests/conftest.py` blocks socket connections.
- Deterministic tests cover indicators, parsers, collectors, migrations, quality,
  strategies, look-ahead, batch/replay equivalence, accounting invariants,
  lifecycle partial/duplicate/overfill, stops, shared portfolio and CLI exits.
- Persistent tests inject every `CrashPoint` and compare recovered report.
- Stage3/4/5 tests use controlled clocks, fake market sources and MockTransport.
- Package and clean-wheel behaviors have CI jobs.

### Gaps

1. Нет real wait interruption/container SIGTERM test (F-01).
2. Backup tests validate happy path, но не malformed same-version schemas (F-02).
3. Нет performance/soak/recovery scaling budget (F-03).
4. Нет stale-progress health/alert rule test (F-04).
5. Нет DB writer contention/fault test (F-11).
6. Нет property-based sequences для arbitrary partial fills/FIFO/reservations.
   Это полезный backlog, но новая dependency требует отдельного решения.
7. Нет contract tests against captured full exchange responses/version drift;
   existing parsers are deterministic, но public API schema change остаётся risk.
8. CI CLI smoke incomplete (F-10).
9. Hosted CI и dependency audit status в рамках локального ревью NOT VERIFIED.
10. Long-running memory/disk growth and Telegram backlog soak NOT VERIFIED.

Tests mostly assert real domain behavior over fakes rather than merely verifying
mock call counts. Mocked HTTP is appropriate because unit suite обязана быть
offline; public API tests должны быть отдельными opt-in checks, не CI requirement.

## 10. Architecture quality and growth

### Что не следует переписывать

- `PortfolioAccounting` и его FIFO models/invariants;
- `OrderLifecycle`/reservations/idempotent fill handling;
- StrategyDecision time semantics и `_HistoryView`;
- deterministic replay oracle и crash-injection suite;
- conflict-retain market data policy;
- offline-only execution boundary.

### Что чрезмерно сложно

- Engine combines strategy execution, three exit mechanisms, shared portfolio,
  lifecycle wiring and report assembly.
- Paper repository stores normalized rows plus full snapshots; correctness strong,
  write amplification high.
- Single and multi paper loops duplicate control/telemetry/poll handling.

Упрощение должно идти через маленькие tested helpers (interruptible scheduler,
schema validator, checkpoint policy), не через новый engine/service framework.

### Три направления развития

#### Reliability / SRE

1. Исправить F-01…F-05.
2. Restore drill + external alert rules + runbook ownership.
3. Measure lock/contention and long-run state growth.
4. Image provenance, retention and capacity budgets.

#### Python software engineering

1. Постепенно выделить schema validation и polling primitive.
2. Добавить architectural import tests/documented allowed dependencies.
3. Уменьшать large classes только после characterization coverage.
4. Расширять Ruff/type checking по одному package без unrelated churn.

#### Trading simulation correctness

1. Property/invariant tests over random but deterministic order/fill sequences.
2. Explicit execution-model comparison and sensitivity reports.
3. Captured exchange contract fixtures and delisting/no-data cases.
4. Не добавлять strategies, пока baseline simulation/research evidence недостаточны.

## 11. Prioritized action plan

### A. Must fix before VPS deployment

1. Interruptible stop plus realistic Compose grace and SIGTERM test.
2. Full structural schema verification for backup/restore/health.
3. Benchmark and bound replay/checkpoint cost for planned retention; fix if budget
   exceeded.
4. Progress/freshness readiness and external Prometheus alert rules.
5. Reproducible container build from pinned base and constrained dependencies.
6. Complete installed-paper CLI/container smoke and actual restore drill.

### B. Technical debt backlog

1. Make multi status read-only.
2. Bound/validate `Retry-After`.
3. Outbox capacity metrics, retention and explicit burst policy.
4. Validate market schema PK/types; document one-writer procedure and test locks.
5. Complete CI CLI matrix; pin Actions SHAs; gradually widen static checks.
6. Resolve architecture documentation drift.
7. Add property-based accounting/lifecycle tests after dependency approval.
8. Container capabilities hardening and off-host backup policy.

### C. Learning plan

1. CLI/composition root → domain value models.
2. Market data/validation/storage → features/time semantics.
3. Strategy Protocol/runner → backtest sizing/execution/accounting.
4. SQLite transactions → deterministic replay/recovery.
5. Single paper loop/risk → multi-symbol state machine/reconciliation.
6. Logging/metrics/outbox → Docker/backup/CI.
7. Pytest fixtures/mocks/fault injection throughout all stages.

Полная программа находится в
[`learning-roadmap.md`](learning-roadmap.md), первые занятия — в
[`guided-lessons-01-05.md`](guided-lessons-01-05.md).

## 12. Проверенные и непроверенные области

### Реально изучено

- `AGENTS.md`, `README.md`, `pyproject.toml`, constraints and packaging config;
- все Python modules под `src/cryptolab` на уровне imports/public APIs, а critical
  data, strategy, accounting, execution, replay, persistence и paper paths — по
  implementation branches;
- все test modules inventoried; основные domain/replay/paper/CLI tests прочитаны;
- Stage 1–5, architecture, execution, research and ADR documentation;
- Dockerfile, Compose, Prometheus example и оба GitHub workflows.

### NOT VERIFIED

- Hosted GitHub Actions status и artifacts;
- Docker Engine runtime/VPS kernel/filesystem behavior;
- реальная Binance/Bybit/Telegram network behavior;
- production-scale memory, CPU, disk and SQLite contention;
- off-host backup, restore RTO/RPO and Prometheus/Grafana alert delivery;
- profitability или независимая statistical validity strategies;
- private/authenticated exchange integration — intentionally absent.

## 13. Проверки

Результаты интерпретируются буквально: локальный pass не подтверждает hosted CI
или VPS.

| Проверка | Фактический результат |
|---|---|
| `git status --short` до ревью | clean |
| `git log -1 --oneline` | `35083da Production-Style Paper Bot Operations` |
| `python -m pytest ...` | не запущен: глобальной команды `python` нет |
| `python3 -m pytest ...` | не запущен: глобальный Python 3.13.1 не содержит pytest |
| `.venv/bin/python -m pytest -q -p no:cacheprovider` | **537 passed in 1.83s** |
| `.venv/bin/ruff check src tests scripts` | **All checks passed** |
| `PYTHONPYCACHEPREFIX=/tmp/cryptolab-review-pycache .venv/bin/python -m compileall -q src tests scripts` | exit 0, output отсутствует |
| `git diff --check` | exit 0, output отсутствует |
| `.venv/bin/cryptolab --help` + help всех top-level и `paper` subcommands | exit 0 |
| `docker compose config -q` | exit 0 |

`compileall` направил bytecode в `/tmp`, поэтому не создавал runtime artifacts в
repository. Docker image не собирался и container не запускался: это read-only
audit, а не deployment verification.

### Выполненные inspection-команды

- `git status --short`, `git diff --stat`, `git diff --check`, `git log -1`;
- `rg --files`, `find`, `wc -l` для inventory source/tests/docs/deployment;
- `sed -n`, `nl -ba`, `rg` для чтения implementation, imports, public APIs,
  test names, TODO/FIXME и точных line references;
- package dependency extraction по фактическим `from cryptolab...` imports;
- перечисленные в таблице tests/static/compile/CLI/Compose checks.

Единственное изменение working tree после ревью — четыре новых Markdown-файла в
`docs/reviews/`; source, tests, config, existing docs, database и reports не
изменялись.
