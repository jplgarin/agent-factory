import hashlib
from datetime import timedelta

import asyncpg
import pytest

from core.domain.space import Space
from tests.db.catalog import sqlstate
from tests.db.conftest import Database

DENIED = "42501"


async def test_content_hash_must_match_the_body(database: Database) -> None:
    conn = await database.connect(Space.PERSONAL.db_role)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO message_content (space, content_hash, content)"
                " VALUES ('personal', $1, 'body')",
                hashlib.sha256(b"other").digest(),
            )
        with pytest.raises(asyncpg.PostgresError) as error:
            await conn.execute("DELETE FROM message_content")
        assert sqlstate(error.value) == DENIED
    finally:
        await conn.close()


async def test_retention_deletes_only_expired_bodies_and_never_reads_them(
    superuser: asyncpg.Connection,
) -> None:
    rows = {
        name: (f"retention {name}".encode(), age)
        for name, age in (("old", timedelta(days=31)), ("fresh", timedelta(days=29)))
    }
    for body, age in rows.values():
        await superuser.execute(
            "INSERT INTO message_content (space, content_hash, content, created_at)"
            " VALUES ('synaption', sha256($1), $1, now() - $2::interval)",
            body,
            age,
        )
    async with superuser.transaction():
        await superuser.execute("SET LOCAL ROLE af_retention")
        with pytest.raises(asyncpg.PostgresError) as error:
            async with superuser.transaction():
                await superuser.execute("SELECT content FROM message_content")
        assert sqlstate(error.value) == DENIED
        await superuser.execute("DELETE FROM message_content")
        assert await superuser.fetchval("SELECT count(*) FROM message_content") == 0
        with pytest.raises(asyncpg.PostgresError) as error:
            async with superuser.transaction():
                await superuser.execute("UPDATE message_content SET created_at = now()")
        assert sqlstate(error.value) == DENIED
    remaining = {
        row["content"]
        for row in await superuser.fetch(
            "SELECT content FROM message_content WHERE content = ANY($1::bytea[])",
            [body for body, _ in rows.values()],
        )
    }
    assert remaining == {rows["fresh"][0]}


@pytest.mark.parametrize("offset", [timedelta(days=-40), timedelta(days=40)])
async def test_space_roles_cannot_set_created_at(database: Database, offset: timedelta) -> None:
    body = f"backdated {offset}".encode()
    conn = await database.connect(Space.PERSONAL.db_role)
    try:
        statements = [
            (
                "INSERT INTO message_content (space, content_hash, content, created_at)"
                " VALUES ('personal', sha256($1::bytea), $1::bytea, now() + $2::interval)",
                (body, offset),
            ),
            (
                "INSERT INTO tasks (id, space, origin, requested_by, agent_id, input, priority,"
                " budget, status, created_at) VALUES (gen_random_uuid(), 'personal', 'api',"
                " 'owner', 'email.personal', '{}', 'normal', '{}', 'pending',"
                " now() + $1::interval)",
                (offset,),
            ),
            ("UPDATE tasks SET created_at = now() + $1::interval", (offset,)),
            ("UPDATE steps SET created_at = now() + $1::interval", (offset,)),
        ]
        for sql, args in statements:
            with pytest.raises(asyncpg.PostgresError) as error:
                await conn.execute(sql, *args)
            assert sqlstate(error.value) == DENIED, sql
    finally:
        await conn.close()
