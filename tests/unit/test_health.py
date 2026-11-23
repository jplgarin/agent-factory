import asyncio

import httpx

from apps.gateway.app import create_app
from apps.shared.health import Check, run_checks


async def ok() -> None:
    return None


async def broken() -> None:
    raise ConnectionError("postgresql://u:secret@db failed")


async def slow() -> None:
    await asyncio.sleep(5)


async def _get(checks: dict[str, Check], path: str) -> httpx.Response:
    app = create_app(checks, check_timeout_s=0.05)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


async def test_healthz_does_not_depend_on_checks() -> None:
    response = await _get({"database": broken}, "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok_when_all_checks_pass() -> None:
    response = await _get({"database": ok, "temporal": ok}, "/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "temporal": "ok"}}


async def test_readyz_503_without_leaking_error_details() -> None:
    response = await _get({"database": broken, "temporal": slow}, "/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "fail", "temporal": "timeout"},
    }
    assert "secret" not in response.text


async def test_docs_endpoints_are_disabled() -> None:
    assert (await _get({}, "/docs")).status_code == 404
    assert (await _get({}, "/openapi.json")).status_code == 404


async def test_checks_run_concurrently() -> None:
    async def wait() -> None:
        await asyncio.sleep(0.2)

    loop = asyncio.get_running_loop()
    start = loop.time()
    results = await run_checks({"a": wait, "b": wait, "c": wait}, timeout_s=1)
    assert results == {"a": "ok", "b": "ok", "c": "ok"}
    assert loop.time() - start < 0.5
