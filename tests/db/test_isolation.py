"""SPEC 6 / ADR 0002: any cross-space read or write by a space role fails the build.

Tables are discovered from the catalog, so a new space-scoped table is covered automatically.
"""

from collections.abc import AsyncIterator

import asyncpg
import pytest

from core.domain.space import Space
from tests.db import seeds
from tests.db.catalog import space_tables, sqlstate
from tests.db.conftest import Database

PAIRS = [(Space.PERSONAL, Space.SYNAPTION), (Space.SYNAPTION, Space.PERSONAL)]
DENIED = "42501"  # insufficient_privilege, raised for missing grants and RLS violations alike


@pytest.fixture
async def conns(database: Database) -> AsyncIterator[dict[Space, asyncpg.Connection]]:
    opened = {space: await database.connect(space.db_role) for space in Space}
    for space, conn in opened.items():
        assert await conn.fetchval("SELECT current_user") == space.db_role
        for seed in seeds.SEEDS.values():
            await seed(conn, space)
    yield opened
    for conn in opened.values():
        await conn.close()


async def _denied(conn: asyncpg.Connection, sql: str, *args: object) -> str:
    """Run `sql` in a savepoint. Returns its status, or 'denied' if Postgres refused it."""
    try:
        async with conn.transaction():
            return await conn.execute(sql, *args)
    except asyncpg.PostgresError as error:
        if sqlstate(error) != DENIED:
            raise
        return "denied"


async def test_every_space_table_has_a_seed(superuser: asyncpg.Connection) -> None:
    assert set(await space_tables(superuser)) == set(seeds.SEEDS)


@pytest.mark.parametrize(("own", "other"), PAIRS)
async def test_reads_see_only_own_space(
    own: Space, other: Space, conns: dict[Space, asyncpg.Connection], superuser: asyncpg.Connection
) -> None:
    conn = conns[own]
    for table in await space_tables(superuser):
        assert await superuser.fetchval(
            f"SELECT count(*) FROM {table} WHERE space = $1", other.value
        )
        assert await conn.fetchval(f"SELECT count(*) FROM {table}"), table
        status = await _denied(conn, f"SELECT 1 FROM {table} WHERE space <> $1", own.value)
        assert status in ("SELECT 0", "denied"), table


@pytest.mark.parametrize(("own", "other"), PAIRS)
async def test_updates_and_deletes_cannot_touch_the_other_space(
    own: Space, other: Space, conns: dict[Space, asyncpg.Connection], superuser: asyncpg.Connection
) -> None:
    conn = conns[own]
    for table in await space_tables(superuser):
        before = await superuser.fetchval(
            f"SELECT count(*) FROM {table} WHERE space = $1", other.value
        )
        update = await _denied(
            conn, f"UPDATE {table} SET space = space WHERE space = $1", other.value
        )
        delete = await _denied(conn, f"DELETE FROM {table} WHERE space = $1", other.value)
        assert update in ("UPDATE 0", "denied"), table
        assert delete in ("DELETE 0", "denied"), table
        after = await superuser.fetchval(
            f"SELECT count(*) FROM {table} WHERE space = $1", other.value
        )
        assert after == before, table


@pytest.mark.parametrize(("own", "other"), PAIRS)
async def test_rows_cannot_be_moved_into_the_other_space(
    own: Space, other: Space, conns: dict[Space, asyncpg.Connection], superuser: asyncpg.Connection
) -> None:
    conn = conns[own]
    for table in await space_tables(superuser):
        status = await _denied(conn, f"UPDATE {table} SET space = $1", other.value)
        # A role either may not update the table at all, or its policy rejects the new row.
        assert status == "denied", table


@pytest.mark.parametrize(("own", "other"), PAIRS)
@pytest.mark.parametrize("table", sorted(seeds.SEEDS))
async def test_inserts_into_the_other_space_are_denied(
    own: Space, other: Space, table: str, conns: dict[Space, asyncpg.Connection]
) -> None:
    conn = conns[own]
    with pytest.raises(asyncpg.PostgresError) as error:
        async with conn.transaction():
            await seeds.SEEDS[table](conn, other)
    assert sqlstate(error.value) == DENIED


@pytest.mark.parametrize(("own", "other"), PAIRS)
async def test_references_cannot_cross_spaces(
    own: Space, other: Space, database: Database, superuser: asyncpg.Connection
) -> None:
    """Even a writer that sees both spaces (here the superuser) cannot link rows across them."""
    other_task = await seeds.task(superuser, other)
    other_run = await seeds.run(superuser, other, other_task)
    own_task = await seeds.task(superuser, own)
    own_run = await seeds.run(superuser, own, own_task)
    attempts = [
        lambda: seeds.task(superuser, own, parent=other_task),
        lambda: seeds.run(superuser, own, other_task),
        lambda: seeds.step(superuser, own, other_run),
        lambda: seeds.approval(superuser, own, other_task, own_run),
        lambda: seeds.approval(superuser, own, own_task, other_run),
        lambda: seeds.memory_episode(superuser, own, other_run),
    ]
    for attempt in attempts:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with superuser.transaction():
                await attempt()


async def test_space_roles_cannot_assume_each_other(conns: dict[Space, asyncpg.Connection]) -> None:
    for own, other in PAIRS:
        assert await _denied(conns[own], f"SET ROLE {other.db_role}") == "denied"
        assert await _denied(conns[own], "SET ROLE af_owner") == "denied"
        # With row_security off, a query that RLS would filter raises instead of bypassing it.
        async with conns[own].transaction():
            await conns[own].execute("SET LOCAL row_security = off")
            assert await _denied(conns[own], "SELECT * FROM tasks") == "denied"


async def test_agent_ids_must_belong_to_the_row_space(superuser: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await superuser.execute(
            "INSERT INTO tasks (id, space, origin, requested_by, agent_id, input, priority,"
            " budget, status) VALUES (gen_random_uuid(), 'personal', 'api', 'owner',"
            " 'email.synaption', '{}', 'normal', '{}', 'pending')"
        )
