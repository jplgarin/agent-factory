"""Memory services (SPEC 4.4): `MemoryWriter` is the only write path, `MemoryReader` the only
read path. Both are bound to one space and refuse anything from another."""

import asyncio
import math
import re
from collections.abc import Sequence
from datetime import datetime

from core.domain.space import Space
from core.memory.embedding import EMBEDDING_DIM, EmbeddingProvider
from core.memory.models import (
    TOPIC_PATTERN,
    Episode,
    EpisodeDraft,
    MemoryDraft,
    MemoryItem,
    MemoryMatch,
    Trust,
)
from core.memory.stores import EpisodicStore, SemanticStore

MAX_RECALL = 50
MAX_QUERY_CHARS = 2000
_TOPIC = re.compile(TOPIC_PATTERN)


class ProvenanceError(Exception):
    """A write claims more trust than its origin allows."""


class SpaceMismatchError(Exception):
    """A memory operation touched a space other than the one it is bound to."""


class EmbeddingError(Exception):
    """The embedding provider returned something that cannot be stored or compared."""


def _check_provider(embedder: EmbeddingProvider) -> None:
    if embedder.dim != EMBEDDING_DIM:
        raise EmbeddingError(
            f"{embedder.model_id} has {embedder.dim} dimensions; the schema stores {EMBEDDING_DIM}"
        )


def _checked(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    if len(values) != EMBEDDING_DIM:
        raise EmbeddingError(f"expected {EMBEDDING_DIM} dimensions, got {len(values)}")
    if not all(math.isfinite(value) for value in values):
        raise EmbeddingError("embedding contains NaN or infinity")
    if not any(values):
        raise EmbeddingError("embedding is the zero vector")
    return values


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


class MemoryWriter:
    """Stores memory after checking where it came from.

    `tainted` says whether the run that produced the text has read untrusted content (PLAN G7).
    Text from a tainted run, or from an external source, is stored only when the draft is marked
    untrusted; a draft that claims to be trusted is refused, never downgraded silently.
    """

    def __init__(
        self,
        space: Space,
        *,
        items: SemanticStore,
        episodes: EpisodicStore,
        embedder: EmbeddingProvider,
    ) -> None:
        _check_provider(embedder)
        self._space = space
        self._items = items
        self._episodes = episodes
        self._embedder = embedder

    async def write_item(self, draft: MemoryDraft, *, tainted: bool, now: datetime) -> MemoryItem:
        self._check_space(draft.space)
        self._check_trust(draft.trust, tainted)
        if draft.expires_at is not None and draft.expires_at <= _aware(now):
            raise ValueError("expires_at must be in the future")
        duplicate = await self._items.find_duplicate(
            draft, embedding_model=self._embedder.model_id, now=now
        )
        if duplicate is not None:
            return self._own(duplicate)
        vectors = await asyncio.to_thread(self._embedder.embed_documents, [draft.content])
        if len(vectors) != 1:
            raise EmbeddingError(f"expected 1 embedding, got {len(vectors)}")
        stored = await self._items.add(
            draft, embedding=_checked(vectors[0]), embedding_model=self._embedder.model_id
        )
        return self._own(stored)

    async def write_episode(self, draft: EpisodeDraft, *, tainted: bool) -> Episode:
        self._check_space(draft.space)
        self._check_trust(draft.trust, tainted)
        stored = await self._episodes.add(draft)
        if stored.space is not self._space:
            raise SpaceMismatchError("store returned an episode from another space")
        return stored

    def _check_space(self, space: Space) -> None:
        if space is not self._space:
            raise SpaceMismatchError(f"writer for {self._space.value} got {space.value}")

    @staticmethod
    def _check_trust(trust: Trust, tainted: bool) -> None:
        if tainted and trust is not Trust.UNTRUSTED:
            raise ProvenanceError(
                "text produced after reading untrusted content must be marked untrusted"
            )

    def _own(self, item: MemoryItem) -> MemoryItem:
        if item.space is not self._space:
            raise SpaceMismatchError("store returned an item from another space")
        return item


class MemoryReader:
    """Retrieves memory for one space. Results that include untrusted text taint the reader's
    run; callers that must stay untainted pass `include_untrusted=False`."""

    def __init__(
        self,
        space: Space,
        *,
        items: SemanticStore,
        episodes: EpisodicStore,
        embedder: EmbeddingProvider,
    ) -> None:
        _check_provider(embedder)
        self._space = space
        self._items = items
        self._episodes = episodes
        self._embedder = embedder

    async def search(
        self, query: str, *, limit: int, now: datetime, include_untrusted: bool
    ) -> list[MemoryMatch]:
        """The `limit` live items closest to `query`, closest first."""
        _check_limit(limit)
        if not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query must be 1 to {MAX_QUERY_CHARS} characters and not blank")
        vector = await asyncio.to_thread(self._embedder.embed_query, query)
        matches = await self._items.search(
            space=self._space,
            embedding=_checked(vector),
            embedding_model=self._embedder.model_id,
            limit=limit,
            now=_aware(now),
            include_untrusted=include_untrusted,
        )
        for match in matches:
            if match.item.space is not self._space:
                raise SpaceMismatchError("store returned an item from another space")
            if match.item.embedding_model != self._embedder.model_id:
                raise EmbeddingError("store compared vectors of different embedding models")
            if match.item.trust is Trust.UNTRUSTED and not include_untrusted:
                raise ProvenanceError("store returned untrusted text to a trusted-only search")
        return matches

    async def recent_episodes(
        self, topic: str, *, limit: int, include_untrusted: bool
    ) -> list[Episode]:
        """The newest `limit` episodes of finished runs filed under `topic`."""
        _check_limit(limit)
        if _TOPIC.fullmatch(topic) is None:
            raise ValueError("not a topic key")
        episodes = await self._episodes.recent(
            space=self._space, topic=topic, limit=limit, include_untrusted=include_untrusted
        )
        for episode in episodes:
            if episode.space is not self._space:
                raise SpaceMismatchError("store returned an episode from another space")
            if episode.trust is Trust.UNTRUSTED and not include_untrusted:
                raise ProvenanceError("store returned untrusted text to a trusted-only search")
        return episodes


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_RECALL:
        raise ValueError(f"limit must be between 1 and {MAX_RECALL}")
