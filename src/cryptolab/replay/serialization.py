"""Canonical, lossless serialization helpers for replay persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from cryptolab.execution.adapter import (
    ExecutionAdapterSnapshot,
    ExecutionEvent,
    ExecutionEventType,
)
from cryptolab.execution.lifecycle import BalanceReservation, LifecycleSnapshot
from cryptolab.execution.models import (
    Fill,
    Instrument,
    IntentSource,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from cryptolab.market_data.models import Candle


SERIALIZATION_VERSION = 1


def canonical_json(value: object) -> str:
    """Serialize supported values deterministically without float conversion."""
    return json.dumps(
        _encode(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def component_configuration(component: object) -> dict[str, object]:
    """Describe immutable component configuration, excluding runtime state."""
    component_type = _qualified_name(component)
    if is_dataclass(component):
        parameters = {
            item.name: getattr(component, item.name)
            for item in fields(component)
        }
    else:
        provider = getattr(component, "persistence_configuration", None)
        if provider is None:
            raise ValueError(
                f"{component_type} must be a dataclass or expose "
                "persistence_configuration()"
            )
        parameters = dict(provider())
    return {
        "type": component_type,
        "name": getattr(component, "name", None),
        "parameters": parameters,
    }


def serialize_candle(candle: Candle) -> str:
    return canonical_json({
        "open_time": candle.open_time,
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
        "close_time": candle.close_time,
        "quote_volume": str(candle.quote_volume),
        "number_of_trades": candle.number_of_trades,
        "taker_buy_base_volume": (
            None
            if candle.taker_buy_base_volume is None
            else str(candle.taker_buy_base_volume)
        ),
        "taker_buy_quote_volume": (
            None
            if candle.taker_buy_quote_volume is None
            else str(candle.taker_buy_quote_volume)
        ),
    })


def deserialize_candle(payload: str) -> Candle:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Persisted candle payload must be an object")
    try:
        return Candle(
            open_time=_required_int(value, "open_time"),
            open=Decimal(_required_str(value, "open")),
            high=Decimal(_required_str(value, "high")),
            low=Decimal(_required_str(value, "low")),
            close=Decimal(_required_str(value, "close")),
            volume=Decimal(_required_str(value, "volume")),
            close_time=_required_int(value, "close_time"),
            quote_volume=Decimal(_required_str(value, "quote_volume")),
            number_of_trades=_optional_int(value, "number_of_trades"),
            taker_buy_base_volume=_optional_decimal(
                value,
                "taker_buy_base_volume",
            ),
            taker_buy_quote_volume=_optional_decimal(
                value,
                "taker_buy_quote_volume",
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid persisted candle payload") from error


def decode_mapping(payload: str) -> dict[str, object]:
    """Decode the restricted scalar mapping used by risk-policy state."""
    raw = json.loads(payload)
    value = _decode(raw)
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError("Persisted state must be a string-keyed mapping")
    return value


def serialize_execution_snapshot(snapshot: ExecutionAdapterSnapshot) -> str:
    """Serialize lifecycle, fills, reservations, and adapter event order."""
    return canonical_json({
        "intents": [_intent_payload(item) for item in snapshot.intents],
        "orders": [_order_payload(item) for item in snapshot.lifecycle.orders],
        "fills": [_fill_payload(item) for item in snapshot.lifecycle.fills],
        "reservations": [
            _reservation_payload(item)
            for item in snapshot.lifecycle.reservations
        ],
        "events": [_execution_event_payload(item) for item in snapshot.events],
        "next_sequence": snapshot.next_sequence,
    })


def deserialize_execution_snapshot(payload: str) -> ExecutionAdapterSnapshot:
    """Restore typed lifecycle state; model constructors validate integrity."""
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise TypeError("execution snapshot must be an object")
        snapshot = ExecutionAdapterSnapshot(
            lifecycle=LifecycleSnapshot(
                orders=tuple(_parse_order(item) for item in raw["orders"]),
                fills=tuple(_parse_fill(item) for item in raw["fills"]),
                reservations=tuple(
                    _parse_reservation(item) for item in raw["reservations"]
                ),
            ),
            intents=tuple(_parse_intent(item) for item in raw["intents"]),
            events=tuple(
                _parse_execution_event(item) for item in raw["events"]
            ),
            next_sequence=_plain_int(raw, "next_sequence"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid persistent execution snapshot") from error
    return snapshot


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Cannot persist a non-finite Decimal")
        return {"$decimal": str(value)}
    if isinstance(value, Enum):
        return {
            "$enum": _qualified_name(value),
            "value": value.value,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": _qualified_name(value),
            **{
                item.name: _encode(getattr(value, item.name))
                for item in fields(value)
            },
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Persistent mappings require string keys")
        return {
            key: _encode(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise TypeError(f"Unsupported persistent value: {type(value).__name__}")


def _decode(value: object) -> object:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$decimal"}:
            raw = value["$decimal"]
            if not isinstance(raw, str):
                raise ValueError("Invalid persisted Decimal")
            decimal_value = Decimal(raw)
            if not decimal_value.is_finite():
                raise ValueError("Persisted Decimal must be finite")
            return decimal_value
        if "$enum" in value or "$type" in value:
            raise ValueError("Typed model payload is not a scalar state mapping")
        return {key: _decode(item) for key, item in value.items()}
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("Unsupported persisted JSON value")


def _qualified_name(value: object) -> str:
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _instrument_payload(instrument: Instrument) -> dict[str, object]:
    return {
        "exchange": instrument.exchange,
        "market_type": instrument.market_type,
        "symbol": instrument.symbol,
        "base_asset": instrument.base_asset,
        "quote_asset": instrument.quote_asset,
    }


def _parse_instrument(value: object) -> Instrument:
    raw = _plain_dict(value)
    return Instrument(
        exchange=_plain_str(raw, "exchange"),
        market_type=_plain_str(raw, "market_type"),
        symbol=_plain_str(raw, "symbol"),
        base_asset=_plain_str(raw, "base_asset"),
        quote_asset=_plain_str(raw, "quote_asset"),
    )


def _intent_payload(intent: OrderIntent) -> dict[str, object]:
    return {
        "intent_id": intent.intent_id,
        "source_event_id": intent.source_event_id,
        "source": intent.source.value,
        "instrument": _instrument_payload(intent.instrument),
        "side": intent.side.value,
        "quantity": str(intent.quantity),
        "created_at": intent.created_at,
        "earliest_execution_at": intent.earliest_execution_at,
        "signal_available_time": intent.signal_available_time,
        "source_candle_open_time": intent.source_candle_open_time,
        "explanation": intent.explanation,
    }


def _parse_intent(value: object) -> OrderIntent:
    raw = _plain_dict(value)
    signal_time = raw["signal_available_time"]
    if signal_time is not None and type(signal_time) is not int:
        raise TypeError("signal_available_time must be an integer or null")
    return OrderIntent(
        intent_id=_plain_str(raw, "intent_id"),
        source_event_id=_plain_str(raw, "source_event_id"),
        source=IntentSource(_plain_str(raw, "source")),
        instrument=_parse_instrument(raw["instrument"]),
        side=OrderSide(_plain_str(raw, "side")),
        quantity=Decimal(_plain_str(raw, "quantity")),
        created_at=_plain_int(raw, "created_at"),
        earliest_execution_at=_plain_int(raw, "earliest_execution_at"),
        signal_available_time=signal_time,
        source_candle_open_time=_plain_int(raw, "source_candle_open_time"),
        explanation=_plain_str(raw, "explanation"),
    )


def _order_payload(order: Order) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "intent_id": order.intent_id,
        "instrument": _instrument_payload(order.instrument),
        "side": order.side.value,
        "requested_quantity": str(order.requested_quantity),
        "filled_quantity": str(order.filled_quantity),
        "status": order.status.value,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "terminal_reason": order.terminal_reason,
    }


def _parse_order(value: object) -> Order:
    raw = _plain_dict(value)
    terminal_reason = raw["terminal_reason"]
    if terminal_reason is not None and not isinstance(terminal_reason, str):
        raise TypeError("terminal_reason must be a string or null")
    return Order(
        order_id=_plain_str(raw, "order_id"),
        intent_id=_plain_str(raw, "intent_id"),
        instrument=_parse_instrument(raw["instrument"]),
        side=OrderSide(_plain_str(raw, "side")),
        requested_quantity=Decimal(_plain_str(raw, "requested_quantity")),
        filled_quantity=Decimal(_plain_str(raw, "filled_quantity")),
        status=OrderStatus(_plain_str(raw, "status")),
        created_at=_plain_int(raw, "created_at"),
        updated_at=_plain_int(raw, "updated_at"),
        terminal_reason=terminal_reason,
    )


def _fill_payload(fill: Fill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "instrument": _instrument_payload(fill.instrument),
        "side": fill.side.value,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
        "quote_notional": str(fill.quote_notional),
        "fee": str(fill.fee),
        "fee_asset": fill.fee_asset,
        "executed_at": fill.executed_at,
        "venue_fill_id": fill.venue_fill_id,
    }


def _parse_fill(value: object) -> Fill:
    raw = _plain_dict(value)
    venue_fill_id = raw["venue_fill_id"]
    if venue_fill_id is not None and not isinstance(venue_fill_id, str):
        raise TypeError("venue_fill_id must be a string or null")
    return Fill(
        fill_id=_plain_str(raw, "fill_id"),
        order_id=_plain_str(raw, "order_id"),
        instrument=_parse_instrument(raw["instrument"]),
        side=OrderSide(_plain_str(raw, "side")),
        quantity=Decimal(_plain_str(raw, "quantity")),
        price=Decimal(_plain_str(raw, "price")),
        quote_notional=Decimal(_plain_str(raw, "quote_notional")),
        fee=Decimal(_plain_str(raw, "fee")),
        fee_asset=_plain_str(raw, "fee_asset"),
        executed_at=_plain_int(raw, "executed_at"),
        venue_fill_id=venue_fill_id,
    )


def _reservation_payload(item: BalanceReservation) -> dict[str, object]:
    return {
        "order_id": item.order_id,
        "instrument": _instrument_payload(item.instrument),
        "asset": item.asset,
        "original_amount": str(item.original_amount),
        "remaining_amount": str(item.remaining_amount),
    }


def _parse_reservation(value: object) -> BalanceReservation:
    raw = _plain_dict(value)
    return BalanceReservation(
        order_id=_plain_str(raw, "order_id"),
        instrument=_parse_instrument(raw["instrument"]),
        asset=_plain_str(raw, "asset"),
        original_amount=Decimal(_plain_str(raw, "original_amount")),
        remaining_amount=Decimal(_plain_str(raw, "remaining_amount")),
    )


def _execution_event_payload(item: ExecutionEvent) -> dict[str, object]:
    return {
        "sequence": item.sequence,
        "event_id": item.event_id,
        "event_type": item.event_type.value,
        "order_id": item.order_id,
        "occurred_at": item.occurred_at,
        "fill_id": item.fill_id,
    }


def _parse_execution_event(value: object) -> ExecutionEvent:
    raw = _plain_dict(value)
    fill_id = raw["fill_id"]
    if fill_id is not None and not isinstance(fill_id, str):
        raise TypeError("fill_id must be a string or null")
    return ExecutionEvent(
        sequence=_plain_int(raw, "sequence"),
        event_id=_plain_str(raw, "event_id"),
        event_type=ExecutionEventType(_plain_str(raw, "event_type")),
        order_id=_plain_str(raw, "order_id"),
        occurred_at=_plain_int(raw, "occurred_at"),
        fill_id=fill_id,
    )


def _plain_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("value must be an object")
    return value


def _plain_str(value: Mapping[str, Any], key: str) -> str:
    result = value[key]
    if not isinstance(result, str):
        raise TypeError(f"{key} must be a string")
    return result


def _plain_int(value: Mapping[str, Any], key: str) -> int:
    result = value[key]
    if type(result) is not int:
        raise TypeError(f"{key} must be an integer")
    return result


def _required_str(value: Mapping[str, Any], key: str) -> str:
    result = value[key]
    if not isinstance(result, str):
        raise TypeError(f"{key} must be a string")
    return result


def _required_int(value: Mapping[str, Any], key: str) -> int:
    result = value[key]
    if type(result) is not int:
        raise TypeError(f"{key} must be an integer")
    return result


def _optional_int(value: Mapping[str, Any], key: str) -> int | None:
    result = value[key]
    if result is not None and type(result) is not int:
        raise TypeError(f"{key} must be an integer or null")
    return result


def _optional_decimal(
    value: Mapping[str, Any],
    key: str,
) -> Decimal | None:
    result = value[key]
    if result is None:
        return None
    if not isinstance(result, str):
        raise TypeError(f"{key} must be a string or null")
    decimal_value = Decimal(result)
    if not decimal_value.is_finite():
        raise ValueError(f"{key} must be finite")
    return decimal_value
