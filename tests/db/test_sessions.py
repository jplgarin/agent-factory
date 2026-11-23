"""Session defaults on the space roles (infra/postgres/roles.sql, docs/DATABASE.md)."""

import time

import asyncpg
import pytest

from core.domain.space import Space
from tests.db.catalog import sqlstate
from tests.db.conftest import Database

DEFAULTS = {
    "lock_timeout": "5s",
    "statement_timeout": "30s",
    "idle_in_transaction_session_timeout": "1min",
}


@pytest.mark.parametrize("space", list(Space))
async def test_space_role_sessions_get_the_timeouts(database: Database, space: Space) -> None:
    conn = await database.connect(space.db_role)
    try:
        for setting, value in DEFAULTS.items():
            assert await conn.fetchval(f"SHOW {setting}") == value
    finally:
        await conn.close()


async def test_a_table_lock_held_by_one_space_stalls_the_other_for_at_most_lock_timeout(
    database: Database,
) -> None:
    holder = await database.connect(Space.SYNAPTION.db_role)
    waiter = await database.connect(Space.PERSONAL.db_role)
    try:
        async with holder.transaction():
            # runs still has a table-level UPDATE grant, which is what LOCK TABLE needs.
            await holder.execute("LOCK TABLE runs IN ACCESS EXCLUSIVE MODE")
            start = time.monotonic()
            with pytest.raises(asyncpg.PostgresError) as error:
                await waiter.fetch("SELECT 1 FROM runs LIMIT 1")
            elapsed = time.monotonic() - start
        assert sqlstate(error.value) == "55P03"  # lock_not_available
        assert 4 < elapsed < 10
    finally:
        await holder.close()
        await waiter.close()


async def test_column_level_grants_leave_no_table_lock(database: Database) -> None:
    """LOCK TABLE beyond ROW EXCLUSIVE needs a table-level UPDATE, DELETE or TRUNCATE grant."""
    conn = await database.connect(Space.SYNAPTION.db_role)
    try:
        for table in ("tasks", "steps", "message_content", "memory_items", "memory_episodes"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(f"LOCK TABLE {table} IN SHARE MODE")
    finally:
        await conn.close()
