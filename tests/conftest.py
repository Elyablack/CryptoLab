"""Repository-wide deterministic test safeguards."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any unit/integration test that attempts a real network socket."""

    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError(
            "Tests must not use live network access; use a mocked transport"
        )

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
