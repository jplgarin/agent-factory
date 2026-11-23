"""Postgres stores for memory (SPEC 4.4). The connection logs in as one space's role, so
row-level security scopes every statement; the explicit `space` predicates are a second check."""

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, Select, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from core.domain.space import Space
from core.domain.task import RunStatus
from core.memory.models import (
    Episode,
    EpisodeDraft,
    MemoryDraft,
    MemoryItem,
    MemoryMatch,
    MemorySource,
    Trust,
)
from db.models import MemoryEpisodeRow, MemoryItemRow, RunRow

_ITEM = MemoryItemRow
_EPISODE = MemoryEpisodeRow
_ITEM_COLUMNS = (
    _ITEM.id,
    _ITEM.space,
    _ITEM.kind,
    _ITEM.content,
    _ITEM.source_kind,
    _ITEM.source_ref,
    _ITEM.created_by_agent,
    _ITEM.confidence,
    _ITEM.trust,
    _ITEM.created_at,
    _ITEM.expires_at,
    _ITEM.embedding_model,
)


class MemoryConflictError(Exception):
    """The ID of a draft is already used by different content, or is not visible in this space."""


def _content_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def _item(row: Row[Any]) -> MemoryItem:
    return MemoryItem(
        id=row.id,
        space=row.space,
        kind=row.kind,
        content=row.content,
        source=MemorySource(kind=row.source_kind, ref=row.source_ref),
        created_by_agent=row.created_by_agent,
        confidence=row.confidence,
        trust=row.trust,
        created_at=row.created_at,
        expires_at=row.expires_at,
        embedding_model=row.embedding_model,
    )


def _live(now: datetime) -> Any:
    return or_(_ITEM.expires_at.is_(None), _ITEM.expires_at > now)


class SemanticMemoryStore:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def find_duplicate(
        self, draft: MemoryDraft, *, embedding_model: str, now: datetime
    ) -> MemoryItem | None:
        outlives = _ITEM.expires_at.is_(None)
        if draft.expires_at is not None:
            outlives = or_(outlives, _ITEM.expires_at >= draft.expires_at)
        statement = (
            select(*_ITEM_COLUMNS)
            .where(
                _ITEM.space == draft.space,
                _ITEM.content_hash == _content_hash(draft.content),
                _ITEM.embedding_model == embedding_model,
                _live(now),
                outlives,
            )
            .order_by(_ITEM.trust, _ITEM.created_at, _ITEM.id)
            .limit(1)
        )
        if draft.trust is Trust.TRUSTED:
            statement = statement.where(_ITEM.trust == Trust.TRUSTED)
        row = (await self._conn.execute(statement)).one_or_none()
        return None if row is None else _item(row)

    async def add(
        self, draft: MemoryDraft, *, embedding: Sequence[float], embedding_model: str
    ) -> MemoryItem:
        content_hash = _content_hash(draft.content)
        statement = (
            insert(_ITEM)
            .values(
                id=draft.id,
                space=draft.space,
                kind=draft.kind,
                content=draft.content,
                content_hash=content_hash,
                source_kind=draft.source.kind,
                source_ref=draft.source.ref,
                created_by_agent=draft.created_by_agent,
                confidence=draft.confidence,
                trust=draft.trust,
                expires_at=draft.expires_at,
                embedding=list(embedding),
                embedding_model=embedding_model,
                embedding_dim=len(embedding),
            )
            .on_conflict_do_nothing(index_elements=[_ITEM.id])
            .returning(*_ITEM_COLUMNS)
        )
        row = (await self._conn.execute(statement)).one_or_none()
        if row is not None:
            return _item(row)
        # The ID exists: a retried write returns what the first attempt stored.
        existing = (
            await self._conn.execute(
                select(*_ITEM_COLUMNS, _ITEM.content_hash).where(
                    _ITEM.space == draft.space, _ITEM.id == draft.id
                )
            )
        ).one_or_none()
        if existing is None or existing.content_hash != content_hash:
            raise MemoryConflictError(f"memory item {draft.id} already exists with other content")
        return _item(existing)

    async def search(
        self,
        *,
        space: Space,
        embedding: Sequence[float],
        embedding_model: str,
        limit: int,
        now: datetime,
        include_untrusted: bool,
    ) -> list[MemoryMatch]:
        distance = _ITEM.embedding.cosine_distance(list(embedding)).label("distance")
        # Exact search (no approximate index), so the result is the true top `limit` and is
        # reproducible. One row per content: the trusted copy wins, then the oldest.
        matching = select(*_ITEM_COLUMNS, distance).where(
            _ITEM.space == space, _ITEM.embedding_model == embedding_model, _live(now)
        )
        if not include_untrusted:
            matching = matching.where(_ITEM.trust == Trust.TRUSTED)
        candidates = (
            matching.distinct(_ITEM.content_hash)
            .order_by(_ITEM.content_hash, _ITEM.trust, _ITEM.created_at, _ITEM.id)
            .subquery()
        )
        statement = select(candidates).order_by(candidates.c.distance, candidates.c.id).limit(limit)
        rows = (await self._conn.execute(statement)).all()
        return [
            MemoryMatch(item=_item(row), distance=min(max(float(row.distance), 0.0), 2.0))
            for row in rows
        ]


def _episodes() -> Select[Any]:
    return select(
        _EPISODE.id,
        _EPISODE.space,
        _EPISODE.run_id,
        RunRow.task_id,
        RunRow.agent_id,
        RunRow.status.label("outcome"),
        _EPISODE.topic,
        _EPISODE.summary,
        _EPISODE.trust,
        _EPISODE.created_at,
    ).join(RunRow, (RunRow.space == _EPISODE.space) & (RunRow.id == _EPISODE.run_id))


class EpisodicMemoryStore:
    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add(self, draft: EpisodeDraft) -> Episode:
        await self._conn.execute(
            insert(_EPISODE)
            .values(
                id=draft.id,
                space=draft.space,
                run_id=draft.run_id,
                topic=draft.topic,
                summary=draft.summary,
                trust=draft.trust,
            )
            .on_conflict_do_nothing()
        )
        # A run has one episode. If it was already written, the first one stands.
        row = (
            await self._conn.execute(
                _episodes().where(_EPISODE.space == draft.space, _EPISODE.run_id == draft.run_id)
            )
        ).one_or_none()
        if row is None:
            raise MemoryConflictError(f"episode {draft.id} already exists for another run")
        return Episode.model_validate(row._asdict())

    async def recent(
        self, *, space: Space, topic: str, limit: int, include_untrusted: bool
    ) -> list[Episode]:
        statement = (
            _episodes()
            .where(
                _EPISODE.space == space,
                _EPISODE.topic == topic,
                RunRow.status != RunStatus.RUNNING,
            )
            .order_by(_EPISODE.created_at.desc(), _EPISODE.id)
            .limit(limit)
        )
        if not include_untrusted:
            statement = statement.where(_EPISODE.trust == Trust.TRUSTED)
        rows = (await self._conn.execute(statement)).all()
        return [Episode.model_validate(row._asdict()) for row in rows]
