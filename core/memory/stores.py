"""Persistence interfaces for memory. The Postgres implementation is db/memory.py."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from core.domain.space import Space
from core.memory.models import Episode, EpisodeDraft, MemoryDraft, MemoryItem, MemoryMatch


class SemanticStore(Protocol):
    async def find_duplicate(
        self, draft: MemoryDraft, *, embedding_model: str, now: datetime
    ) -> MemoryItem | None:
        """A live item with the same content that already covers `draft`: embedded with the
        same model, at least as trusted and not expiring sooner."""
        ...

    async def add(
        self, draft: MemoryDraft, *, embedding: Sequence[float], embedding_model: str
    ) -> MemoryItem:
        """Insert the item. Writing the same draft ID again returns the stored item."""
        ...

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
        """Nearest live items embedded with `embedding_model`, closest first, one per content."""
        ...


class EpisodicStore(Protocol):
    async def add(self, draft: EpisodeDraft) -> Episode:
        """Insert the episode. A run has one episode: writing it again returns the stored one."""
        ...

    async def recent(
        self, *, space: Space, topic: str, limit: int, include_untrusted: bool
    ) -> list[Episode]:
        """Episodes of finished runs filed under `topic`, newest first."""
        ...
