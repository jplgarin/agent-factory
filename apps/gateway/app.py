from collections.abc import Mapping

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from apps.shared.health import Check, readiness_body, run_checks


def create_app(checks: Mapping[str, Check], check_timeout_s: float) -> FastAPI:
    app = FastAPI(title="Agent Factory gateway", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        status_code, body = readiness_body(await run_checks(checks, check_timeout_s))
        return JSONResponse(body, status_code=status_code)

    return app
