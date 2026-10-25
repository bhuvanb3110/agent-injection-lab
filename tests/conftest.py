import socket

import pytest


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a network connection (DECISIONS.md D10)."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Network access is disabled in tests; use the fake model.")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
