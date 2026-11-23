import socket
from typing import Any, cast

import pytest
from temporalio.client import Client

from apps.shared.checks import database_check, temporal_check


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_database_check_raises_when_unreachable() -> None:
    check = database_check(f"postgresql://u:p@127.0.0.1:{_closed_port()}/db", "af_personal")
    with pytest.raises(ConnectionRefusedError):
        await check()


class _ServiceClient:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    async def check_health(self) -> bool:
        return self.healthy


class _Client:
    def __init__(self, healthy: bool) -> None:
        self.service_client = _ServiceClient(healthy)


def _client(healthy: bool) -> Client:
    return cast(Any, _Client(healthy))


async def test_temporal_check() -> None:
    await temporal_check(_client(True))()
    with pytest.raises(RuntimeError):
        await temporal_check(_client(False))()
