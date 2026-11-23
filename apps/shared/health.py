import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

import structlog

Check = Callable[[], Awaitable[None]]
"""A readiness check. Returns on success, raises on failure."""

CheckStatus = Literal["ok", "fail", "timeout"]

log = structlog.get_logger(__name__)


async def _run_one(name: str, check: Check, timeout_s: float) -> CheckStatus:
    try:
        async with asyncio.timeout(timeout_s):
            await check()
    except TimeoutError:
        log.warning("readiness_check_timeout", check=name, timeout_s=timeout_s)
        return "timeout"
    except Exception as exc:
        log.warning("readiness_check_failed", check=name, error_type=type(exc).__name__)
        return "fail"
    return "ok"


async def run_checks(checks: Mapping[str, Check], timeout_s: float) -> dict[str, CheckStatus]:
    """Run all checks concurrently. Never raises and never exposes error details."""
    names = list(checks)
    results = await asyncio.gather(*(_run_one(n, checks[n], timeout_s) for n in names))
    return dict(zip(names, results, strict=True))


def readiness_body(results: Mapping[str, CheckStatus]) -> tuple[int, dict[str, object]]:
    ready = all(status == "ok" for status in results.values())
    body: dict[str, object] = {"status": "ready" if ready else "not_ready", "checks": dict(results)}
    return (200 if ready else 503), body
