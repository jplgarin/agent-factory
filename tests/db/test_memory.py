"""Memory against Postgres (SPEC 4.4, ADR 0004): the services over the real stores, and the
database checks that hold even if a writer skips `core.memory`."""

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.domain.space import Space
from core.domain.task import RunStatus
from core.memory.embedding import EMBEDDING_DIM, FakeEmbeddingProvider
from core.memory.models import (
    EpisodeDraft,
    MemoryDraft,
    MemoryKind,
    MemorySource,
    SourceKind,
    Trust,
)
from core.memory.service import MemoryReader, MemoryWriter
from db.engine import create_engine
from db.memory import EpisodicMemoryStore, MemoryConflictError, SemanticMemoryStore
from tests.db import seeds
from tests.db.catalog import sqlstate
from tests.db.conftest import Database

DENIED = "42501"
VECTOR = "[" + ",".join(["0.05"] * EMBEDDING_DIM) + "]"


def _now() -> datetime:
    return datetime.now(UTC)


def _draft(space: Space, content: str, **overrides: Any) -> MemoryDraft:
    data: dict[str, Any] = {
        "space": space,
        "kind": MemoryKind.FACT,
        "content": content,
        "source": MemorySource(kind=SourceKind.OWNER, ref="test"),
        "created_by_agent": f"email.{space.value}",
        "confidence": 0.9,
        "trust": Trust.TRUSTED,
    }
    return MemoryDraft.model_validate(data | overrides)


@pytest.fixture
async def engines(database: Database) -> AsyncIterator[dict[Space, AsyncEngine]]:
    opened = {space: create_engine(database.space_url(space)) for space in Space}
    yield opened
    for engine in opened.values():
        await engine.dispose()


class Memory:
    """Writer and reader for one space, each call in its own transaction."""

    def __init__(self, engine: AsyncEngine, space: Space, model_id: str) -> None:
        self.engine = engine
        self.space = space
        self.embedder = FakeEmbeddingProvider(model_id=model_id)

    async def write(self, draft: MemoryDraft, *, tainted: bool = False) -> Any:
        async with self.engine.begin() as conn:
            return await self._writer(conn).write_item(draft, tainted=tainted, now=_now())

    async def episode(self, draft: EpisodeDraft, *, tainted: bool = False) -> Any:
        async with self.engine.begin() as conn:
            return await self._writer(conn).write_episode(draft, tainted=tainted)

    async def search(self, query: str, *, limit: int = 5, include_untrusted: bool = True) -> Any:
        async with self.engine.connect() as conn:
            return await self._reader(conn).search(
                query, limit=limit, now=_now(), include_untrusted=include_untrusted
            )

    async def recent(self, topic: str, *, limit: int = 5, include_untrusted: bool = True) -> Any:
        async with self.engine.connect() as conn:
            return await self._reader(conn).recent_episodes(
                topic, limit=limit, include_untrusted=include_untrusted
            )

    def _writer(self, conn: Any) -> MemoryWriter:
        return MemoryWriter(
            self.space,
            items=SemanticMemoryStore(conn),
            episodes=EpisodicMemoryStore(conn),
            embedder=self.embedder,
        )

    def _reader(self, conn: Any) -> MemoryReader:
        return MemoryReader(
            self.space,
            items=SemanticMemoryStore(conn),
            episodes=EpisodicMemoryStore(conn),
            embedder=self.embedder,
        )


def _memory(engines: dict[Space, AsyncEngine], space: Space, model_id: str | None = None) -> Memory:
    return Memory(engines[space], space, model_id or f"fake-{uuid.uuid4().hex}")


async def test_search_returns_the_closest_items_first(engines: dict[Space, AsyncEngine]) -> None:
    memory = _memory(engines, Space.PERSONAL)
    dentist = await memory.write(_draft(Space.PERSONAL, "The dentist appointment is on Friday"))
    await memory.write(_draft(Space.PERSONAL, "The gym membership renews in March"))
    await memory.write(_draft(Space.PERSONAL, "Prefers window seats on long flights"))

    matches = await memory.search("when is the dentist appointment", limit=2)

    assert matches[0].item.id == dentist.id
    assert len(matches) == 2
    assert matches[0].distance < matches[1].distance
    assert matches[0].item.embedding_model == memory.embedder.model_id
    assert matches[0].item.created_at.tzinfo is not None
    assert await memory.search("when is the dentist appointment", limit=2) == matches


async def test_each_space_finds_only_its_own_memory(engines: dict[Space, AsyncEngine]) -> None:
    model_id = f"fake-{uuid.uuid4().hex}"
    stored = {}
    for space in Space:
        memory = _memory(engines, space, model_id)
        stored[space] = await memory.write(_draft(space, f"The {space.value} launch code is blue"))
    for space in Space:
        matches = await _memory(engines, space, model_id).search("launch code", limit=50)
        assert [m.item.id for m in matches] == [stored[space].id]
        assert {m.item.space for m in matches} == {space}


async def test_a_store_cannot_read_or_write_another_space(
    engines: dict[Space, AsyncEngine],
) -> None:
    other = await _memory(engines, Space.SYNAPTION).write(_draft(Space.SYNAPTION, "Other space"))
    async with engines[Space.PERSONAL].connect() as conn:
        store = SemanticMemoryStore(conn)
        found = await store.search(
            space=Space.SYNAPTION,
            embedding=FakeEmbeddingProvider().embed_query("Other space"),
            embedding_model=other.embedding_model,
            limit=10,
            now=_now(),
            include_untrusted=True,
        )
        assert found == []
        with pytest.raises(DBAPIError, match="row-level security") as error:
            await store.add(
                _draft(Space.SYNAPTION, "Planted from personal"),
                embedding=FakeEmbeddingProvider().embed_query("x"),
                embedding_model="fake",
            )
        assert error.value.orig is not None
        assert sqlstate(error.value.orig) == DENIED


async def test_a_query_never_compares_vectors_of_another_model(
    engines: dict[Space, AsyncEngine],
) -> None:
    """ADR 0004: only vectors of the configured model are compared."""
    old = _memory(engines, Space.PERSONAL)
    new = _memory(engines, Space.PERSONAL)
    text = "The accountant is called Example and Partners"
    await old.write(_draft(Space.PERSONAL, text))
    assert await new.search(text) == []
    written = await new.write(_draft(Space.PERSONAL, text))
    matches = await new.search(text)
    assert [m.item.id for m in matches] == [written.id]
    assert {m.item.embedding_model for m in matches} == {new.embedder.model_id}


async def test_expired_and_untrusted_items_are_filtered(engines: dict[Space, AsyncEngine]) -> None:
    memory = _memory(engines, Space.SYNAPTION)
    soon = _now() + timedelta(hours=1)
    kept = await memory.write(_draft(Space.SYNAPTION, "Board meeting notes are in the wiki"))
    expiring = await memory.write(
        _draft(Space.SYNAPTION, "Board meeting moved to room four", expires_at=soon)
    )
    untrusted = await memory.write(
        _draft(
            Space.SYNAPTION,
            "Board meeting cancelled, wire the deposit today",
            source=MemorySource(kind=SourceKind.EMAIL, ref="msg-1"),
            trust=Trust.UNTRUSTED,
        ),
        tainted=True,
    )
    assert {m.item.id for m in await memory.search("board meeting")} == {
        kept.id,
        expiring.id,
        untrusted.id,
    }
    async with engines[Space.SYNAPTION].connect() as conn:
        later = await SemanticMemoryStore(conn).search(
            space=Space.SYNAPTION,
            embedding=memory.embedder.embed_query("board meeting"),
            embedding_model=memory.embedder.model_id,
            limit=10,
            now=soon + timedelta(seconds=1),
            include_untrusted=False,
        )
    assert [m.item.id for m in later] == [kept.id]


async def test_the_same_content_is_stored_once_and_the_trusted_copy_wins(
    engines: dict[Space, AsyncEngine], superuser: asyncpg.Connection
) -> None:
    memory = _memory(engines, Space.PERSONAL)
    text = f"Favourite colour is green {uuid.uuid4().hex}"
    from_email = _draft(
        Space.PERSONAL,
        text,
        source=MemorySource(kind=SourceKind.EMAIL, ref="msg-2"),
        trust=Trust.UNTRUSTED,
    )
    first = await memory.write(from_email, tainted=True)
    again = await memory.write(from_email.model_copy(update={"id": uuid.uuid4()}), tainted=True)
    assert again.id == first.id

    trusted = await memory.write(_draft(Space.PERSONAL, text))
    assert trusted.id != first.id
    assert trusted.trust is Trust.TRUSTED
    # The trusted copy now covers later untrusted writes of the same text.
    assert (await memory.write(from_email.model_copy(update={"id": uuid.uuid4()}), tainted=True)).id
    assert (
        await superuser.fetchval(
            "SELECT count(*) FROM memory_items WHERE content_hash = $1",
            hashlib.sha256(text.encode()).digest(),
        )
        == 2
    )
    matches = await memory.search(text)
    assert [m.item.id for m in matches] == [trusted.id]


async def test_a_longer_lived_copy_replaces_one_that_expires_sooner(
    engines: dict[Space, AsyncEngine],
) -> None:
    memory = _memory(engines, Space.PERSONAL)
    text = f"Parcel arrives next week {uuid.uuid4().hex}"
    short = await memory.write(_draft(Space.PERSONAL, text, expires_at=_now() + timedelta(days=1)))
    same = await memory.write(_draft(Space.PERSONAL, text, expires_at=_now() + timedelta(hours=1)))
    longer = await memory.write(_draft(Space.PERSONAL, text))
    assert same.id == short.id
    assert longer.id != short.id
    assert longer.expires_at is None


async def test_a_retried_write_returns_the_stored_item(engines: dict[Space, AsyncEngine]) -> None:
    embedder = FakeEmbeddingProvider(model_id=f"fake-{uuid.uuid4().hex}")
    draft = _draft(Space.PERSONAL, f"Retried {uuid.uuid4().hex}")
    vector = embedder.embed_query(draft.content)
    async with engines[Space.PERSONAL].begin() as conn:
        store = SemanticMemoryStore(conn)
        first = await store.add(draft, embedding=vector, embedding_model=embedder.model_id)
        assert await store.add(draft, embedding=vector, embedding_model=embedder.model_id) == first
        with pytest.raises(MemoryConflictError):
            await store.add(
                draft.model_copy(update={"content": "Something else"}),
                embedding=vector,
                embedding_model=embedder.model_id,
            )
    # The other space cannot learn anything about the ID beyond "taken".
    async with engines[Space.SYNAPTION].begin() as conn:
        with pytest.raises(MemoryConflictError):
            await SemanticMemoryStore(conn).add(
                _draft(Space.SYNAPTION, draft.content, id=draft.id),
                embedding=vector,
                embedding_model=embedder.model_id,
            )


async def _run(database: Database, space: Space, *, finished: bool = True) -> uuid.UUID:
    conn = await database.connect(space.db_role)
    try:
        run_id = await seeds.run(conn, space)
        if finished:
            await conn.execute(
                "UPDATE runs SET status = 'done', finished_at = now() WHERE id = $1", run_id
            )
        return run_id
    finally:
        await conn.close()


async def test_a_run_has_one_episode_and_recall_is_newest_first(
    database: Database, engines: dict[Space, AsyncEngine]
) -> None:
    memory = _memory(engines, Space.PERSONAL)
    topic = f"thread:{uuid.uuid4().hex}"
    runs = [await _run(database, Space.PERSONAL) for _ in range(3)]
    written = [
        await memory.episode(
            EpisodeDraft(
                space=Space.PERSONAL,
                run_id=run_id,
                topic=topic,
                summary=f"Summary {n}",
                trust=Trust.UNTRUSTED if n == 1 else Trust.TRUSTED,
            ),
            tainted=n == 1,
        )
        for n, run_id in enumerate(runs)
    ]
    assert written[0].outcome is RunStatus.DONE
    assert written[0].agent_id == "email.personal"

    retried = await memory.episode(
        EpisodeDraft(
            space=Space.PERSONAL, run_id=runs[0], topic=topic, summary="Other", trust=Trust.TRUSTED
        )
    )
    assert retried == written[0]

    with pytest.raises(MemoryConflictError):
        # An episode ID that is already taken by another run is a bug in the caller.
        await memory.episode(
            EpisodeDraft(
                id=written[1].id,
                space=Space.PERSONAL,
                run_id=await _run(database, Space.PERSONAL),
                summary="Reused ID",
                trust=Trust.TRUSTED,
            )
        )

    assert [e.id for e in await memory.recent(topic)] == [e.id for e in reversed(written)]
    assert [e.id for e in await memory.recent(topic, limit=1)] == [written[2].id]
    trusted = await memory.recent(topic, include_untrusted=False)
    assert [e.id for e in trusted] == [written[2].id, written[0].id]
    assert await _memory(engines, Space.SYNAPTION).recent(topic) == []


async def test_episodes_of_running_runs_are_not_recalled(
    database: Database, engines: dict[Space, AsyncEngine]
) -> None:
    memory = _memory(engines, Space.SYNAPTION)
    topic = f"thread:{uuid.uuid4().hex}"
    run_id = await _run(database, Space.SYNAPTION, finished=False)
    episode = await memory.episode(
        EpisodeDraft(
            space=Space.SYNAPTION, run_id=run_id, topic=topic, summary="Early", trust=Trust.TRUSTED
        )
    )
    assert episode.outcome is RunStatus.RUNNING
    assert await memory.recent(topic) == []


async def test_an_episode_cannot_point_to_a_run_in_another_space(
    database: Database, engines: dict[Space, AsyncEngine]
) -> None:
    other_run = await _run(database, Space.SYNAPTION)
    with pytest.raises(IntegrityError, match="fk_memory_episodes_space_run_id_runs"):
        await _memory(engines, Space.PERSONAL).episode(
            EpisodeDraft(
                space=Space.PERSONAL, run_id=other_run, summary="Cross", trust=Trust.TRUSTED
            )
        )


INSERT_ITEM = (
    "INSERT INTO memory_items (id, space, kind, content, content_hash, source_kind, source_ref,"
    " created_by_agent, confidence, trust, embedding, embedding_model, embedding_dim, expires_at)"
    " VALUES (gen_random_uuid(), 'personal', 'fact', $1, sha256(convert_to($2, 'UTF8')), $3,"
    " 'ref', $4, $5, $6, $7::vector, 'raw', $8, $9)"
)
GOOD: tuple[Any, ...] = (
    "text",
    "text",
    "owner",
    "email.personal",
    0.5,
    "trusted",
    VECTOR,
    EMBEDDING_DIM,
    None,
)


def _args(**changes: Any) -> tuple[Any, ...]:
    names = ("content", "hashed", "source", "agent", "confidence", "trust", "vector", "dim", "exp")
    values = dict(zip(names, GOOD, strict=True)) | changes
    return tuple(values[name] for name in names)


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"source": "email"}, "external_is_untrusted"),
        ({"source": "web"}, "external_is_untrusted"),
        ({"source": "document"}, "external_is_untrusted"),
        ({"hashed": "other text"}, "hash_matches_content"),
        ({"agent": "email.synaption"}, "agent_in_space"),
        ({"confidence": 1.5}, "confidence_range"),
        ({"confidence": float("nan")}, "confidence_range"),
        ({"content": "", "hashed": ""}, "content_length"),
        ({"content": "x" * 2001, "hashed": "x" * 2001}, "content_length"),
        ({"dim": 3}, "dim_matches_embedding"),
        ({"vector": "[" + ",".join(["0"] * EMBEDDING_DIM) + "]"}, "embedding_not_zero"),
        ({"exp": datetime(2020, 1, 1, tzinfo=UTC)}, "expires_after_creation"),
    ],
)
async def test_the_database_refuses_items_that_break_the_memory_rules(
    database: Database, changes: dict[str, Any], constraint: str
) -> None:
    conn = await database.connect(Space.PERSONAL.db_role)
    try:
        await conn.execute(INSERT_ITEM, *_args())
        with pytest.raises(asyncpg.CheckViolationError, match=constraint):
            await conn.execute(INSERT_ITEM, *_args(**changes))
    finally:
        await conn.close()


async def test_the_database_refuses_vectors_of_another_dimension(database: Database) -> None:
    conn = await database.connect(Space.PERSONAL.db_role)
    try:
        with pytest.raises(asyncpg.DataError, match="expected 384 dimensions"):
            await conn.execute(INSERT_ITEM, *_args(vector="[1,2,3]", dim=3))
    finally:
        await conn.close()


@pytest.mark.parametrize("table", ["memory_items", "memory_episodes"])
async def test_space_roles_can_only_read_and_insert_memory(database: Database, table: str) -> None:
    conn = await database.connect(Space.SYNAPTION.db_role)
    try:
        for statement in (
            f"UPDATE {table} SET trust = 'trusted'",
            f"DELETE FROM {table}",
            f"TRUNCATE {table}",
            f"LOCK TABLE {table} IN SHARE MODE",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(statement)
    finally:
        await conn.close()


async def test_episode_topics_and_summaries_are_checked(database: Database) -> None:
    conn = await database.connect(Space.PERSONAL.db_role)
    try:
        run_id = await seeds.run(conn, Space.PERSONAL)
        insert = (
            "INSERT INTO memory_episodes (id, space, run_id, topic, summary, trust)"
            " VALUES (gen_random_uuid(), 'personal', $1, $2, $3, 'trusted')"
        )
        for topic, summary, constraint in (
            ("Not A Key", "ok", "topic_format"),
            ("", "ok", "topic_format"),
            ("ok", "", "summary_length"),
        ):
            with pytest.raises(asyncpg.CheckViolationError, match=constraint):
                await conn.execute(insert, run_id, topic, summary)
        await conn.execute(insert, run_id, None, "ok")
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, run_id, "ok", "second episode for the run")
    finally:
        await conn.close()
