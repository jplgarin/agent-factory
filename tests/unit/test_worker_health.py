import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from apps.worker.activities import ping
from apps.worker.health_server import HealthServer


async def ok() -> None:
    return None


async def broken() -> None:
    raise RuntimeError("down")


async def _request(port: int, raw: bytes) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


@pytest.fixture
async def server() -> AsyncIterator[tuple[HealthServer, int]]:
    checks = {"database": ok, "worker": ok}
    srv = HealthServer(checks, check_timeout_s=0.5)
    port = await srv.start("127.0.0.1", 0)
    yield srv, port
    await srv.stop()


async def test_healthz_and_readyz(server: tuple[HealthServer, int]) -> None:
    _, port = server
    assert await _request(port, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n") == (
        200,
        {"status": "ok"},
    )
    status, body = await _request(port, b"GET /readyz HTTP/1.1\r\n\r\n")
    assert status == 200
    assert body == {"status": "ready", "checks": {"database": "ok", "worker": "ok"}}


async def test_unknown_path_and_method(server: tuple[HealthServer, int]) -> None:
    _, port = server
    assert (await _request(port, b"GET /other HTTP/1.1\r\n\r\n"))[0] == 404
    assert (await _request(port, b"POST /readyz HTTP/1.1\r\n\r\n"))[0] == 405
    assert (await _request(port, b"\r\n"))[0] == 404


async def test_readyz_503_when_a_check_fails() -> None:
    srv = HealthServer({"worker": broken}, check_timeout_s=0.5)
    port = await srv.start("127.0.0.1", 0)
    try:
        status, body = await _request(port, b"GET /readyz HTTP/1.1\r\n\r\n")
    finally:
        await srv.stop()
    assert status == 503
    assert body == {"status": "not_ready", "checks": {"worker": "fail"}}


async def test_ping_activity() -> None:
    assert await ping() == "pong"
