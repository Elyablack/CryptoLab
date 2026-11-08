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

BUY означает предложение увеличить spot-экспозицию, SELL — сократить или
закрыть её. Сигналы не являются ордерами, не открывают short-позиции и не
передаются на биржу.

Подробности:

- [Архитектура Market Data Collector](docs/architecture.md)
- [ADR 0001: начальная архитектура](docs/decisions/0001-initial-architecture.md)
