"""Persistent, fail-closed wrapper for deterministic Stage 1 replay."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from cryptolab.backtesting.models import BacktestConfig
from cryptolab.backtesting.sizing import (
    LegacyAllInAllOutSizer,
    PositionSizer,
)
from cryptolab.features.builder import FeatureBuilder
from cryptolab.execution.adapter import SimulatedExecutionAdapter
from cryptolab.market_data.markets import DEFAULT_EXCHANGE, SPOT_MARKET_TYPE
from cryptolab.market_data.models import Candle
from cryptolab.replay.models import (
    DeterministicReplayReport,
    ReplayCandleResult,
    ReplaySessionCheckpoint,
)
from cryptolab.replay.persistence_models import (
    CrashPoint,
    PersistentReplayState,
    ReplayConfigurationError,
    ReplaySessionUnavailableError,
    ReplayStateIntegrityError,
)
from cryptolab.replay.risk import (
    AllowAllReplayRiskPolicy,
    PersistentReplayRiskPolicy,
    ReplayRiskStateValue,
)
from cryptolab.replay.runner import (
    DeterministicReplayRunner,
    DeterministicReplaySession,
)
from cryptolab.replay.serialization import (
    SERIALIZATION_VERSION,
    canonical_json,
    component_configuration,
    decode_mapping,
    deserialize_candle,
    serialize_candle,
)
from cryptolab.storage.paper_state_repository import PaperStateRepository
from cryptolab.strategies.base import Strategy


class PersistentReplayRunner:
    """Create or recover one-symbol persistent replay sessions."""

    def __init__(
        self,
        database_path: str | Path,
        config: BacktestConfig | None = None,
        *,
        position_sizer: PositionSizer | None = None,
        risk_policy: PersistentReplayRiskPolicy | None = None,
        feature_builder: FeatureBuilder | None = None,
    ) -> None:
        self._repository = PaperStateRepository(database_path)
        self._repository.init_schema()
        self._config = config or BacktestConfig()
        self._position_sizer = position_sizer or LegacyAllInAllOutSizer()
        self._risk_policy = risk_policy or AllowAllReplayRiskPolicy()
        self._feature_builder = feature_builder or FeatureBuilder()
        if type(self._feature_builder) is not FeatureBuilder:
            raise ValueError(
                "Persistent replay currently requires the standard "
                "stateless FeatureBuilder"
            )
        if not isinstance(self._risk_policy, PersistentReplayRiskPolicy):
            raise ValueError(
                "Persistent replay risk policy must expose configuration, "
                "snapshot_state(), and restore_state()"
            )

    @property
    def repository(self) -> PaperStateRepository:
        return self._repository

    def create(
        self,
        session_id: str,
        strategy: Strategy,
        symbol: str,
        interval: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> PersistentReplaySession:
        initial_risk_state = self._risk_state_json()
        session = self._new_inner_session(
            strategy,
            symbol,
            interval,
            exchange,
            market_type,
        )
        configuration_json = self._configuration_json(
            strategy,
            symbol,
            interval,
            exchange,
            market_type,
        )
        checkpoint = session.checkpoint()
        self._assert_checkpoint_invariants(checkpoint)
        self._repository.create_session(
            session_id=session_id,
            configuration_json=configuration_json,
            initial_risk_state_json=initial_risk_state,
            checkpoint=checkpoint,
            risk_state_json=initial_risk_state,
        )
        return PersistentReplaySession(
            session_id=session_id,
            inner=session,
            repository=self._repository,
            risk_policy=self._risk_policy,
        )

    def recover(
        self,
        session_id: str,
        strategy: Strategy,
        symbol: str,
        interval: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        market_type: str = SPOT_MARKET_TYPE,
    ) -> PersistentReplaySession:
        stored = self._repository.load_session(session_id)
        expected_configuration = self._configuration_json(
            strategy,
            symbol,
            interval,
            exchange,
            market_type,
        )
        if stored.configuration_json != expected_configuration:
            raise ReplayConfigurationError(
                "Stored replay configuration is incompatible with recovery"
            )
        try:
            initial_state = decode_mapping(stored.initial_risk_state_json)
            self._risk_policy.restore_state(_risk_mapping(initial_state))
        except (TypeError, ValueError) as error:
            raise ReplayStateIntegrityError(
                "Stored initial risk-policy state is invalid"
            ) from error

        session = self._new_inner_session(
            strategy,
            symbol,
            interval,
            exchange,
            market_type,
        )
        try:
            for stored_candle in stored.candles:
                candle = deserialize_candle(stored_candle.payload)
                if candle.open_time != stored_candle.open_time:
                    raise ReplayStateIntegrityError(
                        "Candle payload timestamp differs from journal"
                    )
                session.process_candle(
                    candle,
                    event_id=stored_candle.event_id,
                )
        except ReplayStateIntegrityError:
            raise
        except Exception as error:
            raise ReplayStateIntegrityError(
                "Persisted replay cannot be deterministically reconstructed"
            ) from error

        checkpoint = session.checkpoint()
        self._assert_checkpoint_invariants(checkpoint)
        current_risk_state = self._risk_state_json()
        if current_risk_state != stored.risk_state_json:
            raise ReplayStateIntegrityError(
                "Recovered risk-policy state does not match checkpoint"
            )
        self._repository.verify_checkpoint(
            session_id,
            checkpoint,
            current_risk_state,
        )
        stored_execution = self._repository.load_execution_snapshot(session_id)
        try:
            restored_execution = SimulatedExecutionAdapter.from_snapshot(
                stored_execution
            )
        except ValueError as error:
            raise ReplayStateIntegrityError(
                "Stored order lifecycle cannot be restored safely"
            ) from error
        if restored_execution.snapshot() != stored_execution:
            raise ReplayStateIntegrityError(
                "Restored order lifecycle differs from its checkpoint"
            )
        return PersistentReplaySession(
            session_id=session_id,
            inner=session,
            repository=self._repository,
            risk_policy=self._risk_policy,
        )

    def _new_inner_session(
        self,
        strategy: Strategy,
        symbol: str,
        interval: str,
        exchange: str,
        market_type: str,
    ) -> DeterministicReplaySession:
        return DeterministicReplayRunner(
            self._config,
            position_sizer=self._position_sizer,
            risk_policy=self._risk_policy,
            feature_builder=self._feature_builder,
        ).start(
            strategy,
            symbol,
            interval,
            exchange=exchange,
            market_type=market_type,
        )

    def _configuration_json(
        self,
        strategy: Strategy,
        symbol: str,
        interval: str,
        exchange: str,
        market_type: str,
    ) -> str:
        risk_configuration = {
            "type": (
                f"{type(self._risk_policy).__module__}."
                f"{type(self._risk_policy).__qualname__}"
            ),
            "name": self._risk_policy.name,
            "parameters": dict(
                self._risk_policy.persistence_configuration()
            ),
        }
        return canonical_json({
            "serialization_version": SERIALIZATION_VERSION,
            "exchange": exchange,
            "market_type": market_type,
            "symbol": symbol,
            "interval": interval,
            "backtest_config": self._config,
            "strategy": component_configuration(strategy),
            "position_sizer": component_configuration(self._position_sizer),
            "feature_builder": (
                f"{type(self._feature_builder).__module__}."
                f"{type(self._feature_builder).__qualname__}"
            ),
            "risk_policy": risk_configuration,
        })

    def _risk_state_json(self) -> str:
        return canonical_json(dict(self._risk_policy.snapshot_state()))

    @staticmethod
    def _assert_checkpoint_invariants(
        checkpoint: ReplaySessionCheckpoint,
    ) -> None:
        execution = checkpoint.execution
        state = execution.state
        if state.quote_balance < 0 or state.base_balance < 0:
            raise ReplayStateIntegrityError(
                "Persistent portfolio balances cannot be negative"
            )
        if len(checkpoint.candles) != len(checkpoint.candle_event_ids):
            raise ReplayStateIntegrityError(
                "Candle payloads and event IDs do not reconcile"
            )
        if len(checkpoint.decisions) != len(checkpoint.candles):
            raise ReplayStateIntegrityError(
                "Every committed candle must have one strategy decision"
            )
        event_ids = [event.event_id for event in checkpoint.events]
        if len(event_ids) != len(set(event_ids)):
            raise ReplayStateIntegrityError("Replay event IDs are not unique")
        if [event.sequence for event in checkpoint.events] != list(
            range(1, len(checkpoint.events) + 1)
        ):
            raise ReplayStateIntegrityError(
                "Replay event sequence is not contiguous"
            )
        fill_ids = [fill.fill_id for fill in execution.execution_fills]
        if len(fill_ids) != len(set(fill_ids)):
            raise ReplayStateIntegrityError("Execution fill IDs are not unique")
        quote_reserved = sum(
            (
                item.remaining_amount
                for item in execution.reservations
                if item.asset == "USDT"
            ),
            Decimal("0"),
        )
        if quote_reserved > state.quote_balance:
            raise ReplayStateIntegrityError(
                "Reserved quote balance exceeds available cash"
            )


class PersistentReplaySession:
    """Fail-closed process handle backed by one SQLite checkpoint stream."""

    def __init__(
        self,
        *,
        session_id: str,
        inner: DeterministicReplaySession,
        repository: PaperStateRepository,
        risk_policy: PersistentReplayRiskPolicy,
    ) -> None:
        self._session_id = session_id
        self._inner = inner
        self._repository = repository
        self._risk_policy = risk_policy
        self._failed = False
        self._closed = False
        self._report: DeterministicReplayReport | None = None
        self._last_checkpoint = inner.checkpoint()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def current_state(self) -> PersistentReplayState:
        self._ensure_available()
        checkpoint = self._last_checkpoint
        PersistentReplayRunner._assert_checkpoint_invariants(checkpoint)
        event_id = (
            checkpoint.candle_event_ids[-1]
            if checkpoint.candle_event_ids
            else None
        )
        open_time = checkpoint.candles[-1].open_time if checkpoint.candles else None
        execution = checkpoint.execution
        return PersistentReplayState(
            session_id=self._session_id,
            checkpoint_sequence=len(checkpoint.candle_event_ids),
            checkpoint_event_id=event_id,
            checkpoint_open_time=open_time,
            portfolio=execution.state,
            open_lots=execution.open_lots,
            quote_balance=execution.state.quote_balance,
            base_balance=execution.state.base_balance,
            realized_pnl=execution.state.realized_pnl,
            fees_paid=execution.state.fees_paid,
            replay_event_count=len(checkpoint.events),
            decision_count=len(checkpoint.decisions),
            order_intent_count=len(execution.order_intents),
            order_count=len(execution.orders),
            fill_count=len(execution.execution_fills),
            reservation_count=len(execution.reservations),
            risk_state_json=canonical_json(
                dict(self._risk_policy.snapshot_state())
            ),
        )

    def process_candle(
        self,
        candle: Candle,
        *,
        event_id: str | None = None,
        crash_at: CrashPoint | None = None,
    ) -> ReplayCandleResult:
        self._ensure_available()
        if self._report is not None:
            raise ReplaySessionUnavailableError(
                "Persistent replay session is already finalized"
            )
        identity = self._inner.checkpoint()
        resolved_event_id = event_id if event_id is not None else ":".join((
            "closed-candle",
            identity.exchange,
            identity.market_type,
            identity.symbol,
            identity.interval,
            str(candle.open_time),
        ))
        candle_payload = serialize_candle(candle)
        existing = self._repository.get_candle_event(
            self._session_id,
            resolved_event_id,
        )
        if existing is not None:
            if existing != (candle.open_time, candle_payload):
                self._failed = True
                raise ReplayStateIntegrityError(
                    "Conflicting duplicate persistent candle event"
                )
            return self._inner.process_candle(
                candle,
                event_id=resolved_event_id,
            )
        try:
            _inject(crash_at, CrashPoint.BEFORE_PROCESSING)
            result = self._inner.process_candle(
                candle,
                event_id=resolved_event_id,
            )
            checkpoint = self._inner.checkpoint()
            PersistentReplayRunner._assert_checkpoint_invariants(checkpoint)
            _inject(crash_at, CrashPoint.BEFORE_TRANSACTION)
            self._repository.commit_candle(
                session_id=self._session_id,
                event_id=result.candle_event_id,
                candle_open_time=candle.open_time,
                candle_payload=candle_payload,
                checkpoint=checkpoint,
                risk_state_json=canonical_json(
                    dict(self._risk_policy.snapshot_state())
                ),
                crash_at=crash_at,
            )
            self._last_checkpoint = checkpoint
            return result
        except Exception:
            self._failed = True
            raise

    def finish(self) -> DeterministicReplayReport:
        self._ensure_available()
        if self._report is None:
            self._report = self._inner.finish()
        return self._report

    def close(self) -> None:
        self._closed = True

    def _ensure_available(self) -> None:
        if self._failed:
            raise ReplaySessionUnavailableError(
                "Persistent replay is fail-closed; recover from SQLite"
            )
        if self._closed:
            raise ReplaySessionUnavailableError(
                "Persistent replay handle is closed"
            )


def _risk_mapping(
    value: Mapping[str, object],
) -> Mapping[str, ReplayRiskStateValue]:
    allowed = (str, int, Decimal, bool, type(None))
    if any(not isinstance(item, allowed) for item in value.values()):
        raise ValueError("Risk state values must be supported scalars")
    return value  # type: ignore[return-value]


def _inject(actual: CrashPoint | None, expected: CrashPoint) -> None:
    if actual is expected:
        from cryptolab.replay.persistence_models import InjectedReplayCrash

        raise InjectedReplayCrash(expected)
