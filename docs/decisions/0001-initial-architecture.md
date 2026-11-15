# ADR 0001: Initial CryptoLab Architecture

- Status: Accepted
- Date: 2026-11-03

## Context

CryptoLab is being built as a research platform for studying and analyzing the cryptocurrency market. The first milestone is to reliably collect historical hourly OHLCV data for `BTC/USDT` and `ETH/USDT`.

The system should be easy to understand and verify, and suitable for learning Python step by step. At this stage, there are no requirements for high availability, horizontal scaling, stream processing, or trading operations.

## Decision

### A Single Python Application

The collector will be implemented as a single Python application with a CLI. Within the application, responsibilities are divided among the HTTP client, collection orchestrator, validation, and storage layers. These components are deployed together.

### Binance Spot Public API

The Binance Spot public API is the data source for the first version. The collector will retrieve candles using the public market data endpoint `GET /api/v3/klines`. It will operate without API keys, private endpoints, or order placement endpoints.

### SQLite for Data Storage

Historical candles and collection run metadata are stored in a local SQLite database. A composite primary key ensures candle uniqueness, and UPSERT makes repeated collection idempotent.

### HTTPX for HTTP Requests

The application uses `httpx` to access the public API. It provides explicit timeouts, access to HTTP headers, and a convenient way to substitute the transport in automated tests.

### Decimal for Financial Values

Prices and volumes are parsed with `Decimal` to avoid binary floating-point representation errors when receiving, validating, and storing source data.

### Pytest for Testing

The project uses `pytest` for unit and integration tests. Core HTTP scenarios run against predefined responses without connecting to the exchange.

### No Microservices, Kubernetes, or Automated Trading

The following are out of scope for the first version.

- Microservice architecture
- Kubernetes
- Redis and message brokers
- Automated trading
- Private API keys
- Placing, modifying, or canceling trading orders

## Consequences

### Benefits

- The system can be run and explored locally.
- There are few infrastructure components and points of failure.
- Repeated collection is easy to reproduce.
- Components can be tested independently within the same application.
- The architecture fits the current small data volume.

### Limitations

- SQLite is designed for a single writer at a time.
- The initial implementation is tied to Binance’s data format.
- The application does not provide distributed high availability.
- Scaling and support for other exchanges will require further architectural decisions.

These limitations are acceptable for the first research release. Moving to a more complex architecture should be driven by measured problems with the current design.

## Alternatives Considered

- PostgreSQL was rejected because it would add infrastructure complexity at this stage.
- `requests` was a viable option. `httpx` was chosen for its timeout controls and test transport.
- `float` was rejected for data collection because it can lose decimal precision.
- `ccxt` was deferred. With a single exchange, a direct client makes pagination and error-handling rules easier to follow.
- Microservices and a message queue were rejected because the current use case has no need to scale components independently.
