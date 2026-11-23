import asyncpg
from temporalio.client import Client

from apps.shared.health import Check


def database_check(dsn: str) -> Check:
    async def check() -> None:
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    return check


def temporal_check(client: Client) -> Check:
    async def check() -> None:
        if not await client.service_client.check_health():
            raise RuntimeError("temporal reported not serving")

    return check
