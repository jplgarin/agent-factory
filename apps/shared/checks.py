import asyncpg
from temporalio.client import Client

from apps.shared.health import Check

_ROLE_QUERY = """
SELECT current_user AS role, r.rolsuper, r.rolbypassrls
FROM pg_roles r WHERE r.rolname = current_user
"""


class DatabaseRoleError(RuntimeError):
    pass


def database_check(dsn: str, expected_role: str) -> Check:
    """Ready only when logged in as `expected_role`, which must be subject to RLS (ADR 0002)."""

    async def check() -> None:
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            row = await conn.fetchrow(_ROLE_QUERY)
        except BaseException:
            conn.terminate()
            raise
        await conn.close(timeout=2)
        if row is None or row["role"] != expected_role:
            raise DatabaseRoleError(f"connected as the wrong role, expected {expected_role}")
        if row["rolsuper"] or row["rolbypassrls"]:
            raise DatabaseRoleError(f"role {expected_role} bypasses row-level security")

    return check


def temporal_check(client: Client) -> Check:
    async def check() -> None:
        if not await client.service_client.check_health():
            raise RuntimeError("temporal reported not serving")

    return check
