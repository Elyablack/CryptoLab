# Guided code reading: первые пять занятий

Формат рассчитан на 90–150 минут на занятие. Все эксперименты выполняются в
отдельной учебной ветке и только с `tmp_path`/synthetic fixtures. Рабочие базы,
paper sessions и сеть не используются. Готовых решений к заданиям здесь нет.

## Занятие 1. От команды `cryptolab quality` до exit code

### 1. Что открыть

1. `pyproject.toml:15-16`;
2. `src/cryptolab/cli.py:94-153`;
3. `src/cryptolab/cli.py:611-678` (`_run_quality`);
4. `src/cryptolab/cli.py:1160-1225` и shared parser helpers
   `1756-1915`;
5. `src/cryptolab/market_data/data_quality.py:61-185`;
6. `tests/unit/test_cli.py` — найти tests quality/argument errors;
7. `tests/unit/test_data_quality.py`.

Первое действие — не читать argparse tutorial, а запустить реальную точку входа:

```bash
cryptolab --help
cryptolab quality --help
```

### 2. Порядок чтения функций

`[project.scripts]` → `main()` → `_create_parser()`/common arguments →
`_parse_utc_date()` → dispatch branch `quality` → `_run_quality()` →
`SQLiteRepository.init_schema()` → `DataQualityChecker.check()` → formatter/print
→ return code.

### 3. Конструкции Python

- function annotations и `list[str] | None`;
- `argparse` subparsers, `type`, `choices`, defaults;
- early validation через `parser.error()`;
- `Path`, UTC `datetime`, half-open integer timestamps;
- `try/except` на application boundary;
- frozen report dataclass и property `is_valid`.

### 4. Ручная трассировка

Возьмите `quality --exchange bybit --symbol BTCUSDT --interval 1h --start
2026-01-01 --end 2026-01-02 --database /tmp/lesson.db`. Запишите:

- исходные strings argv;
- тип каждого поля `Namespace`;
- `start_time`, `end_time`, interval milliseconds, expected candle count;
- путь repository call;
- какой condition даёт exit 0/1/2.

Не создавайте свечи; сначала предскажите поведение empty range.

### 5. Breakpoint/pytest

- Breakpoints: `cli.py:101`, `119`, начало `_run_quality`,
  `data_quality.py:84`, перед report return.
- Запуск одного test: `python -m pytest -q -p no:cacheprovider
  tests/unit/test_data_quality.py -k empty`.
- В debugger проверьте, что CLI-level dates уже преобразованы до вызова checker.

### 6. Намеренные ошибки в isolated test

- `end == start`;
- неподдерживаемый interval;
- misaligned start time прямым вызовом checker;
- temp DB без candles;
- один invalid `close_time`.

Для каждой ошибки заранее укажите expected layer и exit/exception.

### 7. Результат занятия

Вы должны уметь объяснить, как console script находит Python function, где
заканчивается parsing, почему checker не печатает сам, как формируется exit code и
почему CLI не содержит data-quality math.

## Занятие 2. Candle как доверенная domain model

### 1. Что открыть

1. `src/cryptolab/market_data/models.py`;
2. `src/cryptolab/market_data/parser.py`;
3. `src/cryptolab/market_data/bybit_parser.py`;
4. `src/cryptolab/market_data/validator.py`;
5. `src/cryptolab/storage/sqlite_repository.py:16-61,207-282,314-348`;
6. `tests/unit/test_parser.py`, `test_bybit_parser.py`, `test_validator.py`.

### 2. Порядок чтения

Начните с полей `Candle`. Затем Binance one-row parser, Bybit envelope/list
parser, `validate_candle`, `validate_candle_timing`, sequence validation. Только
после этого читайте conversion to/from SQLite row.

### 3. Конструкции Python

- `@dataclass(frozen=True)` и value equality;
- `Decimal(str(value))` и почему нельзя промежуточный float;
- `None` для данных, отсутствующих у venue;
- `Sequence`, `enumerate`, exception chaining `raise ... from`;
- dictionary/set comprehensions;
- context manager SQLite transaction и parameterized queries.

### 4. Ручная трассировка

Для одной реальной формы Binance row и одной Bybit row проследите все 11 полей.
Запишите, как reverse Bybit list становится chronological, как inclusive
`close_time` проверяется через `open + interval - 1` и как Decimal превращается в
TEXT и обратно без потери precision.

### 5. Breakpoint/pytest

- Breakpoints в entry parser, на создании Candle и в `validate_candle_timing`.
- Запустить:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/unit/test_parser.py tests/unit/test_bybit_parser.py tests/unit/test_validator.py
```

- Посмотреть `repr` Decimal, типы optional fields и value equality после DB
  round trip.

### 6. Намеренные ошибки

- NaN/Infinity price, zero close, high ниже open;
- taker volume больше total;
- wrong field count/API envelope;
- duplicate/misaligned timestamp;
- incomplete current candle at controlled `as_of_time`.

### 7. Результат

Вы объясняете, почему parser проверяет форму, validator — business invariants,
repository — identity/uniqueness; почему Bybit `None` не заменяется zero и какие
гарантии получатель Candle может считать выполненными только после validation.

## Занятие 3. Feature availability и look-ahead-safe strategy

### 1. Что открыть

1. `src/cryptolab/analytics/returns.py`;
2. `src/cryptolab/analytics/indicators.py`;
3. `src/cryptolab/features/models.py`;
4. `src/cryptolab/features/builder.py`;
5. `src/cryptolab/strategies/base.py`, `models.py`, `runner.py`;
6. одну strategy: `trend_following.py`;
7. `tests/unit/test_features.py`, `test_strategy_runner.py`, relevant strategy tests.

### 2. Порядок чтения

Simple returns → SMA → EMA → RSI/ATR/RVOL warm-up → `FeatureBuilder.build` loop →
`FeatureRow.available_time` → `StrategyRunner.run` → `_HistoryView` → warm-up
branch → strategy evaluate → `_validate_decision`.

### 3. Конструкции Python

- list alignment и `None` warm-up;
- generator/sum with Decimal;
- nested helper function (`rsi_value`);
- `Sequence` implementation with overload and slicing;
- Protocol dispatch и property;
- immutable mapping diagnostics.

### 4. Ручная трассировка

На 5 candles с простыми prices вручную рассчитайте simple returns и 3-period
SMA. Для production periods не пытайтесь считать всё: отметьте first valid
indices. Для row `t` выпишите `open_time`, `close_time`, `available_time`, видимый
history range и earliest legal execution.

### 5. Breakpoint/pytest

- Breakpoint в `FeatureBuilder.build` перед loop и в
  `StrategyRunner._evaluate_strategy`.
- Используйте `pytest -k future` в feature/runner tests.
- В debugger откройте `_HistoryView[-1]`, `[:2]` и попытайтесь обратиться за
  пределами текущего prefix.

### 6. Намеренные ошибки

- missing candle внутри history;
- `available_time != close_time + 1`;
- strategy возвращает wrong symbol/reference price;
- future candle радикально меняется;
- zero prior volume для RVOL.

### 7. Результат

Вы можете доказать, а не просто утверждать, что decision на row `t` видит только
закрытые rows ≤ `t`, объяснить warm-up и отделить value calculation от signal
rule.

## Занятие 4. От BUY signal до FIFO lot и обратно в cash

### 1. Что открыть

1. `src/cryptolab/backtesting/sizing.py`;
2. `src/cryptolab/execution/models.py`;
3. `src/cryptolab/execution/lifecycle.py`;
4. `src/cryptolab/execution/adapter.py:60-290`;
5. `src/cryptolab/backtesting/accounting.py`;
6. `src/cryptolab/backtesting/engine.py:826-1116,1117-1465,1752-2016`;
7. `tests/unit/test_position_sizing.py`, `test_order_lifecycle.py`,
   `test_portfolio_accounting.py`, selected `test_backtest_engine.py`.

### 2. Порядок чтения

SizingContext/proposals → `OrderIntent` validation → reservation/create order →
apply partial/full fill → `PortfolioAccounting.record_buy` → `PositionLot` →
`record_sell` FIFO loop → `LotRealization`/`PositionCycle` → engine report
invariants.

### 3. Конструкции Python

- Protocol и several concrete policies;
- mutable service returning frozen snapshots;
- properties derived from stored fields;
- Decimal proportional allocation;
- identity sets for exactly-once effects;
- `min`, loop with residual quantities, local invariant checks.

### 4. Ручная трассировка

На бумаге проведите:

1. initial 1000 USDT;
2. BUY 2 units at 100, fee 2;
3. BUY 3 units at 110, fee 3;
4. SELL 4 units at 120, exit fee 4.8.

После каждого шага запишите quote, lots, remaining cost basis, fees, realized and
unrealized PnL. Затем сравните с `test_fifo_scale_in_and_partial_scale_out_reconcile`.

### 5. Breakpoint/pytest

- Breakpoints: lifecycle reserve/apply_fill, accounting line before/after cash
  mutation, FIFO loop each consumed piece, engine invariant checks.
- Запуск:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/unit/test_order_lifecycle.py tests/unit/test_portfolio_accounting.py
```

### 6. Намеренные ошибки

- duplicate fill same payload и conflicting payload;
- overfill;
- BUY cost больше cash;
- partial SELL by sizer that disallows it;
- mutation internal realized PnL before `assert_invariants`.

### 7. Результат

Вы объясняете разницу Signal/Intent/Order/Fill/Lot, момент изменения cash,
reservation lifecycle, FIFO fee allocation и каждое равенство PnL. Если balance
не сходится, умеете локализовать layer.

## Занятие 5. Candle event, SQLite COMMIT и recovery после crash

### 1. Что открыть

1. `src/cryptolab/replay/models.py`;
2. `src/cryptolab/replay/runner.py:156-379,548-830`;
3. `src/cryptolab/replay/persistence_models.py`;
4. `src/cryptolab/replay/serialization.py`;
5. `src/cryptolab/replay/persistent.py:101-245,363-610`;
6. `src/cryptolab/storage/paper_state_repository.py:246-452,1137-1310`;
7. `tests/unit/test_deterministic_replay.py`, `test_persistent_replay.py`.

### 2. Порядок чтения

Replay models/clock → session constructor → `process_candle` open phase → pending
execution → close/features/decision → checkpoint → persistent create/process →
repository `BEGIN IMMEDIATE`/writes/COMMIT → recover loop → checkpoint comparison.

### 3. Конструкции Python

- explicit state machine encoded by method order;
- Enum crash points и custom exception hierarchy;
- canonical JSON serialization/deserialization;
- context ownership and fail-closed handle;
- transaction control, rollback and `finally`;
- equality as a recovery oracle.

### 4. Ручная трассировка

Для трёх candles составьте таблицу:

```text
sequence | clock | event type | pending decision | order/fill | in-memory | durable
```

Отдельно остановите timeline в трёх точках: before event journal, after derived
state but before commit, after commit but before caller receives success. Для
каждой точки решите, что будет после restart и retry identical event.

### 5. Breakpoint/pytest

- Breakpoints в `process_candle` open/close, persistent process, repository before
  `BEGIN`, before/after `COMMIT`, recovery compare.
- Запустить один parameterized crash point с `-vv -s`, затем всю matrix:

```bash
python -m pytest -q -p no:cacheprovider tests/unit/test_persistent_replay.py \
  -k crash_injection
```

- После test открыть temp DB read-only и сопоставить journal sequence/checkpoint.

### 6. Намеренные ошибки

- same event ID with changed candle;
- changed strategy/config on recover;
- tampered Decimal or journal open_time;
- injected crash immediately before и after COMMIT;
- second process holding an immediate transaction (только temp DB).

### 7. Результат

Вы способны точно указать durable boundary, объяснить почему uncertain outcome
безопасно retry-ится, как idempotency отличается от exactly-once delivery, почему
recovery replay нужен даже при checkpoint и почему corrupted/incompatible state
останавливается fail-closed.

## После пяти занятий

Не переходите сразу к изменению paper service. Сначала без кода нарисуйте общий
путь:

```text
CLI -> Candle -> FeatureRow -> StrategyDecision -> Sizing -> Risk
    -> OrderIntent -> Order -> Fill -> FIFO -> COMMIT -> Telemetry
```

Для каждой стрелки назовите конкретный файл, метод, timestamp и invariant. Если
хотя бы одна стрелка объясняется фразой «framework делает автоматически»,
вернитесь к соответствующему занятию.
