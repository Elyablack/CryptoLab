"""Durable, non-transactional Telegram delivery for paper operations."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from hashlib import sha256

import httpx

from cryptolab.replay.serialization import canonical_json
from cryptolab.storage.paper_state_repository import (
    PaperStateRepository,
    StoredNotification,
)


TELEGRAM_TOKEN_ENV = "CRYPTOLAB_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "CRYPTOLAB_TELEGRAM_CHAT_ID"


class NotificationDeliveryError(RuntimeError):
    """Safe error classification that never includes credentials or URLs."""


class TelegramNotifier:
    """Small Telegram Bot API client used only by the outbox dispatcher."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Telegram token must be non-empty")
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ValueError("Telegram chat_id must be non-empty")
        self._token = token
        self._chat_id = chat_id
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
        )

    @classmethod
    def from_environment(
        cls,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> TelegramNotifier | None:
        token = os.environ.get(TELEGRAM_TOKEN_ENV)
        chat_id = os.environ.get(TELEGRAM_CHAT_ENV)
        if not token and not chat_id:
            return None
        if not token or not chat_id:
            raise ValueError(
                "Both Telegram token and chat_id environment variables "
                "must be configured"
            )
        return cls(token, chat_id, transport=transport)

    def send(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Telegram message must be non-empty")
        try:
            response = self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text[:4000]},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise NotificationDeliveryError(
                "Telegram delivery failed: " + type(error).__name__
            ) from error
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise NotificationDeliveryError(
                "Telegram delivery returned an unsuccessful response"
            )

    def close(self) -> None:
        self._client.close()


class NotificationOutbox:
    """Persist, deduplicate, rate-limit, and retry operational messages.

    Financial commits never wait for Telegram. Enqueue occurs after durable
    state transitions and delivery failures remain contained here.
    """

    def __init__(
        self,
        repository: PaperStateRepository,
        notifier: TelegramNotifier | None,
        *,
        now_ms: Callable[[], int] | None = None,
        minimum_interval_ms: int = 1_000,
        max_batch: int = 5,
    ) -> None:
        if minimum_interval_ms < 0:
            raise ValueError("minimum_interval_ms must be non-negative")
        if max_batch <= 0:
            raise ValueError("max_batch must be positive")
        self.repository = repository
        self.notifier = notifier
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.minimum_interval_ms = minimum_interval_ms
        self.max_batch = max_batch

    @property
    def enabled(self) -> bool:
        return self.notifier is not None

    def enqueue(
        self,
        *,
        session_id: str,
        event_type: str,
        dedup_key: str,
        message: str,
    ) -> bool:
        if self.notifier is None:
            return False
        identity = "\0".join((session_id, event_type, dedup_key))
        notification_id = "notification:" + sha256(
            identity.encode("utf-8")
        ).hexdigest()
        return self.repository.enqueue_notification(
            notification_id=notification_id,
            session_id=session_id,
            event_type=event_type,
            dedup_key=dedup_key,
            payload_json=canonical_json({"text": message[:4000]}),
            created_at=self._now_ms(),
        )

    def flush(self) -> tuple[int, int]:
        """Attempt a bounded batch and return ``(sent, failed)``."""
        if self.notifier is None:
            return 0, 0
        now = self._now_ms()
        latest = self.repository.latest_notification_sent_at()
        if latest is not None and now - latest < self.minimum_interval_ms:
            return 0, 0
        pending = self.repository.pending_notifications(
            now_ms=now,
            limit=self.max_batch,
        )
        sent = 0
        failed = 0
        for item in pending:
            if latest is not None and now - latest < self.minimum_interval_ms:
                break
            try:
                self.notifier.send(_notification_text(item))
            except NotificationDeliveryError as error:
                failed += 1
                delay = min(3_600_000, 1_000 * (2 ** min(item.attempts, 10)))
                self.repository.defer_notification(
                    item.notification_id,
                    next_attempt_at=now + delay,
                    error=str(error),
                )
                continue
            self.repository.mark_notification_sent(
                item.notification_id,
                sent_at=now,
            )
            sent += 1
            latest = now
        return sent, failed

    def close(self) -> None:
        if self.notifier is not None:
            self.notifier.close()


def _notification_text(item: StoredNotification) -> str:
    try:
        payload = json.loads(item.payload_json)
    except ValueError as error:
        raise NotificationDeliveryError(
            "Stored notification payload is invalid"
        ) from error
    if not isinstance(payload, dict) or not isinstance(
        payload.get("text"), str
    ):
        raise NotificationDeliveryError(
            "Stored notification text is invalid"
        )
    return payload["text"]
