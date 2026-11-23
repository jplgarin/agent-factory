import asyncio
import json
from collections.abc import Mapping

import structlog

from apps.shared.health import Check, readiness_body, run_checks

log = structlog.get_logger(__name__)

_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}


class HealthServer:
    """Minimal HTTP server for /healthz and /readyz. Not published outside the container."""

    def __init__(self, checks: Mapping[str, Check], check_timeout_s: float) -> None:
        self._checks = checks
        self._timeout_s = check_timeout_s
        self._server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(5):
                request_line = await reader.readline()
                if request_line.strip():
                    while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                        pass
            status, body = await self._route(request_line)
            payload = json.dumps(body).encode()
            head = (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(head.encode() + payload)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _route(self, request_line: bytes) -> tuple[int, dict[str, object]]:
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2:
            return 404, {"status": "not_found"}
        method, path = parts[0], parts[1]
        if path not in ("/healthz", "/readyz"):
            return 404, {"status": "not_found"}
        if method != "GET":
            return 405, {"status": "method_not_allowed"}
        if path == "/healthz":
            return 200, {"status": "ok"}
        return readiness_body(await run_checks(self._checks, self._timeout_s))
