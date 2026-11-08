# CryptoLab

CryptoLab — исследовательская платформа для сбора и анализа исторических
данных криптовалютного рынка.

Проект получает публичные Binance Spot и Bybit V5 Spot OHLCV-данные,
сохраняет их в SQLite, проверяет полноту, рассчитывает исторические признаки
и формирует воспроизводимые исследовательские сигналы.

Основные принципы проекта:

- простая и проверяемая архитектура;
- воспроизводимый и идемпотентный сбор данных;
- явный контроль качества и пропусков;
- отсутствие приватных API-ключей и торговых операций;
- отсутствие look-ahead bias в исторических признаках и стратегиях;
- постепенная реализация небольшими тестируемыми итерациями.

## Возможности

- `cryptolab collect` — загрузка исторических свечей;
- `cryptolab quality` — проверка полноты диапазона;
- `cryptolab analyze` — статистика и технические индикаторы;
- `cryptolab signals` — исследовательские BUY/SELL/HOLD-сигналы.
- `cryptolab backtest` — независимая симуляция long-only Spot-стратегий.
- `cryptolab research` — baseline-стратегии, benchmarks и диагностика условий.

Пример:

```bash
cryptolab signals \
  --exchange binance \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2024-01-01 \
  --end 2024-02-01 \
  --database data/market_data.db \
  --strategy breakout
```

`--exchange` принимает `binance` или `bybit`; Binance используется по
умолчанию для обратной совместимости. Пример сбора и проверки Bybit BTCUSDT:

```bash
cryptolab collect \
  --exchange bybit \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2024-01-01 \
  --end 2024-02-01 \
  --database data/market_data.db

cryptolab quality \
  --exchange bybit \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2024-01-01 \
  --end 2024-02-01 \
  --database data/market_data.db
```

При первом открытии существующей базы прежние записи автоматически и без
перезаписи помечаются как `exchange=binance`, `market_type=spot`. Данные бирж
хранятся раздельно. Bybit V5 не возвращает число сделок и taker-buy volumes в
Kline-ответе, поэтому эти поля сохраняются как `NULL`, а не вычисляются.

## Backtesting

Минимальный Backtesting Engine запускает каждую стратегию с отдельным
виртуальным портфелем в 1000 USDT. В режиме `all` сигналы стратегий не
объединяются и не голосуют друг за друга.

```bash
cryptolab backtest \
  --exchange bybit \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2024-01-01 \
  --end 2024-02-01 \
  --database data/market_data.db \
  --strategy all \
  --fee-rate 0.001 \
  --show-trades
```

Исполнение моделируется только на `open` следующей свечи после сигнала.
BUY использует весь доступный USDT только когда портфель пуст; SELL продаёт
весь базовый актив только при открытой long-позиции. На каждый fill применяется
неблагоприятное проскальзывание 0.05% и комиссия, заданная `--fee-rate`
(`0.001` означает 0.1%). Открытая в конце позиция оценивается по последнему
`close` без синтетического SELL.

`--show-trades` добавляет времена формирования и исполнения сигналов, цены,
количество базового актива, комиссии и доходность каждой завершённой сделки.
Если позиция осталась открытой, она выводится отдельно вместе с ценой последней
оценки и нереализованным PnL. HOLD-решения в детальный отчёт не выводятся.

Ограничения модели исполнения и риска:

- исполнение на `open` следующей свечи — идеализированное предположение;
- фиксированное неблагоприятное проскальзывание не моделирует книгу заявок;
- открытая позиция оценивается по последнему `close` без гипотетической
  комиссии и проскальзывания выхода;
- максимальная просадка рассчитывается по equity на закрытиях свечей, а не по
  внутрисвечным минимумам;
- не учитываются lot size, tick size, minimum notional и частичное исполнение;
- комиссия и проскальзывание — параметры модели, а не подтверждённые тарифы
  конкретного аккаунта Bybit.

BUY означает предложение увеличить spot-экспозицию, SELL — сократить или
закрыть её. Сигналы не являются ордерами, не открывают short-позиции и не
передаются на биржу.

## Strategy research

Воспроизводимый baseline-отчёт сравнивает неизменённые Trend Following, Mean
Reversion и Breakout с buy-and-hold и cash-only. Он также показывает exposure,
пропущенные BUY/SELL, diagnostics каждой Trend Following сделки и частоты
условий Mean Reversion.

```bash
cryptolab research \
  --exchange bybit \
  --symbol BTCUSDT \
  --interval 1h \
  --start 2026-10-01 \
  --end 2026-11-01 \
  --database data/market_data.db \
  --fee-rate 0.001
```

Команда работает только с локальными проверенными свечами и не выполняет
HTTP-запросы. Текущий исследовательский snapshot и ограничения описаны в
[Strategy Research Phase 1](docs/research-phase-1.md).

Подробности:

- [Архитектура Market Data Collector](docs/architecture.md)
- [ADR 0001: начальная архитектура](docs/decisions/0001-initial-architecture.md)
- [Strategy Research Phase 1](docs/research-phase-1.md)
