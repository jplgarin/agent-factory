import asyncio
import uuid
from collections.abc import Awaitable, Callable

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.audit.chain import AuditEventDraft, AuditEventType, ChainVerification, verify_chain
from core.domain.space import Space
from db.audit import append_event, load_chain
from db.engine import create_engine
from tests.db.catalog import sqlstate
from tests.db.conftest import SUPERUSER, Database, Server

DENIED = "42501"


def _draft(space: Space, n: int = 0, **overrides: object) -> AuditEventDraft:
    data: dict[str, object] = {
        "space": space,
        "event_type": AuditEventType.TOOL_CALL,
        "actor": f"email.{space.value}",
        "task_id": uuid.uuid4(),
        "payload": {"n": n, "tool": "mail.search", "note": "synthetic \u00e9"},
    }
    return AuditEventDraft.model_validate(data | overrides)


async def _verify(engine: AsyncEngine, space: Space) -> ChainVerification:
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="REPEATABLE READ")
        events, head = await load_chain(conn, space)
    return verify_chain(space, events, head)


async def _append(engine: AsyncEngine, draft: AuditEventDraft) -> None:
    async with engine.begin() as conn:
        await append_event(conn, draft)


async def test_database_and_python_compute_the_same_chain(database: Database) -> None:
    engines = {space: create_engine(database.space_url(space)) for space in Space}
    try:
        for n in range(3):
            for space, engine in engines.items():
                async with engine.begin() as conn:
                    event = await append_event(conn, _draft(space, n, run_id=None))
                assert event.computed_hash() == event.hash
        for space, engine in engines.items():
            result = await _verify(engine, space)
            assert result.valid, result
            assert result.events_checked >= 3
    finally:
        for engine in engines.values():
            await engine.dispose()


async def test_concurrent_appends_form_one_gapless_chain(
    fresh_database: Callable[[], Awaitable[Database]],
) -> None:
    database = await fresh_database()
    engine = create_engine(database.space_url(Space.PERSONAL), pool_size=10, max_overflow=10)
    try:
        await asyncio.gather(*(_append(engine, _draft(Space.PERSONAL, n)) for n in range(40)))
        result = await _verify(engine, Space.PERSONAL)
        assert result.valid, result
        assert result.events_checked == 40
    finally:
        await engine.dispose()


async def test_cross_space_append_is_denied_and_leaves_both_chains_intact(
    database: Database,
) -> None:
    engine = create_engine(database.space_url(Space.PERSONAL))
    try:
        with pytest.raises(Exception) as error:  # noqa: PT011
            await _append(engine, _draft(Space.SYNAPTION))
        assert getattr(error.value, "orig", None) is not None
        assert error.value.orig.sqlstate == DENIED  # pyright: ignore[reportAttributeAccessIssue]
        assert (await _verify(engine, Space.PERSONAL)).valid
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET payload = '{}'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "INSERT INTO audit_events (space, seq, event_type, actor, payload, prev_hash, hash)"
        " VALUES ('personal', 999, 'x', 'x', '{}', '\\x00', '\\x00')",
        "UPDATE audit_chain_heads SET seq = 0",
        "INSERT INTO message_content (space, content_hash, content) VALUES"
        " ('synaption', sha256('x'), 'x')",
    ],
)
async def test_space_roles_cannot_rewrite_history(database: Database, sql: str) -> None:
    conn = await database.connect(Space.PERSONAL.db_role)
    try:
        with pytest.raises(asyncpg.PostgresError) as error:
            await conn.execute(sql)
        assert sqlstate(error.value) == DENIED
    finally:
        await conn.close()


async def test_even_the_owner_cannot_truncate_or_change_events(database: Database) -> None:
    conn = await database.connect("af_owner")
    try:
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute("TRUNCATE audit_events")
        # FORCE ROW LEVEL SECURITY: the owner has no policy on audit_events and sees no rows.
        assert await conn.fetchval("SELECT count(*) FROM audit_events") == 0
    finally:
        await conn.close()


async def _tampered(server: Server, database: Database, *statements: str) -> None:
    """Edit rows as the superuser with triggers off, as an attacker with disk access might."""
    conn = await asyncpg.connect(
        f"postgresql://{SUPERUSER}:{server.passwords[SUPERUSER]}"
        f"@{database.host}:{database.port}/{database.name}"
    )
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = replica")
            for statement in statements:
                await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("statements", "first_invalid"),
    [
        (["UPDATE audit_events SET payload = '{\"n\":99}' WHERE seq = 3"], 3),
        (["UPDATE audit_events SET actor = 'someone' WHERE seq = 1"], 1),
        (
            ["UPDATE audit_events SET occurred_at = occurred_at + interval '1 s' WHERE seq = 4"],
            4,
        ),
        (["DELETE FROM audit_events WHERE seq = 2"], 2),
        (["DELETE FROM audit_events WHERE seq = 5"], 5),
        (
            [
                "UPDATE audit_events SET payload = '{\"n\":0}' WHERE seq = 2",
                "UPDATE audit_events SET hash = audit_event_hash(prev_hash, space, seq,"
                " occurred_at, event_type, actor, task_id, run_id, payload) WHERE seq = 2",
            ],
            3,
        ),
    ],
)
async def test_tampering_is_detected(
    server: Server,
    fresh_database: Callable[[], Awaitable[Database]],
    statements: list[str],
    first_invalid: int,
) -> None:
    database = await fresh_database()
    engine = create_engine(database.space_url(Space.PERSONAL))
    try:
        for n in range(5):
            await _append(engine, _draft(Space.PERSONAL, n))
        assert (await _verify(engine, Space.PERSONAL)).valid
        await _tampered(server, database, *(f"{s} AND space = 'personal'" for s in statements))
        result = await _verify(engine, Space.PERSONAL)
        assert not result.valid
        assert result.first_invalid_seq == first_invalid, result
    finally:
        await engine.dispose()
