"""In-memory stores with the semantics of db/memory.py, for testing `core.memory` alone."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from core.domain.space import Space
from core.domain.task import RunStatus
from core.memory.models import (
    Episode,
    EpisodeDraft,
    MemoryDraft,
    MemoryItem,
    MemoryMatch,
    Trust,
)

START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return min(max(1.0 - dot / norm, 0.0), 2.0)


@dataclass
class InMemorySemanticStore:
    rows: list[tuple[MemoryItem, list[float]]] = field(default_factory=list)

    def _created_at(self) -> datetime:
        return START + timedelta(seconds=len(self.rows))

    async def find_duplicate(
        self, draft: MemoryDraft, *, embedding_model: str, now: datetime
    ) -> MemoryItem | None:
        for item, _ in self.rows:
            covers_trust = draft.trust is Trust.UNTRUSTED or item.trust is Trust.TRUSTED
            outlives = item.expires_at is None or (
                draft.expires_at is not None and item.expires_at >= draft.expires_at
            )
            if (
                item.space is draft.space
                and item.content == draft.content
                and item.embedding_model == embedding_model
                and (item.expires_at is None or item.expires_at > now)
                and covers_trust
                and outlives
            ):
                return item
        return None

    async def add(
        self, draft: MemoryDraft, *, embedding: Sequence[float], embedding_model: str
    ) -> MemoryItem:
        for item, _ in self.rows:
            if item.id == draft.id:
                return item
        item = MemoryItem(
            **draft.model_dump(),
            created_at=self._created_at(),
            embedding_model=embedding_model,
        )
        self.rows.append((item, list(embedding)))
        return item

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
        matches = [
            MemoryMatch(item=item, distance=cosine_distance(embedding, vector))
            for item, vector in self.rows
            if item.space is space
            and item.embedding_model == embedding_model
            and (item.expires_at is None or item.expires_at > now)
            and (include_untrusted or item.trust is Trust.TRUSTED)
        ]
        return sorted(matches, key=lambda m: (m.distance, str(m.item.id)))[:limit]


@dataclass
class InMemoryEpisodicStore:
    episodes: list[Episode] = field(default_factory=list)
    running: set[UUID] = field(default_factory=set)

    async def add(self, draft: EpisodeDraft) -> Episode:
        for episode in self.episodes:
            if episode.space is draft.space and episode.run_id == draft.run_id:
                return episode
        episode = Episode(
            **draft.model_dump(),
            task_id=uuid4(),
            agent_id=f"email.{draft.space.value}",
            outcome=RunStatus.RUNNING if draft.run_id in self.running else RunStatus.DONE,
            created_at=START + timedelta(seconds=len(self.episodes)),
        )
        self.episodes.append(episode)
        return episode

    async def recent(
        self, *, space: Space, topic: str, limit: int, include_untrusted: bool
    ) -> list[Episode]:
        found = [
            episode
            for episode in self.episodes
            if episode.space is space
            and episode.topic == topic
            and episode.outcome is not RunStatus.RUNNING
            and (include_untrusted or episode.trust is Trust.TRUSTED)
        ]
        return sorted(found, key=lambda e: e.created_at, reverse=True)[:limit]
