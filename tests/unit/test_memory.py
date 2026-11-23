"""`core.memory` without a database: provenance, space binding and embedding checks."""

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.domain.space import Space
from core.memory.embedding import EMBEDDING_DIM, FakeEmbeddingProvider
from core.memory.models import (
    EpisodeDraft,
    MemoryDraft,
    MemoryKind,
    MemoryMatch,
    MemorySource,
    SourceKind,
    Trust,
)
from core.memory.service import (
    MAX_RECALL,
    EmbeddingError,
    MemoryReader,
    MemoryWriter,
    ProvenanceError,
    SpaceMismatchError,
)
from tests.unit.memory_fakes import (
    InMemoryEpisodicStore,
    InMemorySemanticStore,
    cosine_distance,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
EMAIL = MemorySource(kind=SourceKind.EMAIL, ref="msg-1")


def _draft(space: Space = Space.PERSONAL, content: str = "Likes tea", **overrides: Any) -> Any:
    data: dict[str, Any] = {
        "space": space,
        "kind": MemoryKind.FACT,
        "content": content,
        "source": MemorySource(kind=SourceKind.OWNER, ref="api"),
        "created_by_agent": f"email.{space.value}",
        "confidence": 0.8,
        "trust": Trust.TRUSTED,
    }
    return MemoryDraft.model_validate(data | overrides)


def _episode(space: Space = Space.PERSONAL, **overrides: Any) -> EpisodeDraft:
    data: dict[str, Any] = {
        "space": space,
        "run_id": uuid4(),
        "topic": "thread:1",
        "summary": "Sorted the inbox",
        "trust": Trust.TRUSTED,
    }
    return EpisodeDraft.model_validate(data | overrides)


class CountingEmbedder(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return super().embed_documents(texts)


class BrokenEmbedder(FakeEmbeddingProvider):
    def __init__(self, vectors: list[list[float]]) -> None:
        super().__init__()
        self.vectors = vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self.vectors

    def embed_query(self, text: str) -> list[float]:
        return self.vectors[0]


def _services(
    space: Space = Space.PERSONAL, embedder: FakeEmbeddingProvider | None = None
) -> tuple[MemoryWriter, MemoryReader, InMemorySemanticStore, InMemoryEpisodicStore]:
    items, episodes = InMemorySemanticStore(), InMemoryEpisodicStore()
    embedder = embedder or FakeEmbeddingProvider()
    writer = MemoryWriter(space, items=items, episodes=episodes, embedder=embedder)
    reader = MemoryReader(space, items=items, episodes=episodes, embedder=embedder)
    return writer, reader, items, episodes


# Drafts


def test_external_sources_cannot_be_drafted_as_trusted() -> None:
    for kind in (SourceKind.EMAIL, SourceKind.WEB, SourceKind.DOCUMENT):
        source = MemorySource(kind=kind, ref="x")
        with pytest.raises(ValidationError, match="must be marked untrusted"):
            _draft(source=source)
        assert _draft(source=source, trust=Trust.UNTRUSTED).trust is Trust.UNTRUSTED
    for kind in (SourceKind.OWNER, SourceKind.AGENT):
        assert not kind.is_external
        assert _draft(source=MemorySource(kind=kind, ref="x")).trust is Trust.TRUSTED


@pytest.mark.parametrize(
    "overrides",
    [
        {"created_by_agent": "email.synaption"},
        {"created_by_agent": "email"},
        {"content": "   "},
        {"content": "nul\x00byte"},
        {"content": "x" * 2001},
        {"confidence": 1.01},
        {"confidence": -0.1},
        {"confidence": math.nan},
        {"expires_at": datetime(2026, 1, 1)},
        {"source": {"kind": "owner", "ref": ""}},
        {"source": {"kind": "telepathy", "ref": "x"}},
        {"unknown": 1},
    ],
)
def test_invalid_drafts_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _draft(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [{"topic": "Has Spaces"}, {"topic": ""}, {"summary": " "}, {"summary": "x" * 2001}],
)
def test_invalid_episode_drafts_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _episode(**overrides)


# Provenance


async def test_text_from_a_tainted_run_is_refused_unless_marked_untrusted() -> None:
    writer, _, items, episodes = _services()
    with pytest.raises(ProvenanceError):
        await writer.write_item(_draft(), tainted=True, now=NOW)
    with pytest.raises(ProvenanceError):
        await writer.write_episode(_episode(), tainted=True)
    assert items.rows == []
    assert episodes.episodes == []

    item = await writer.write_item(_draft(trust=Trust.UNTRUSTED), tainted=True, now=NOW)
    episode = await writer.write_episode(_episode(trust=Trust.UNTRUSTED), tainted=True)
    assert item.trust is Trust.UNTRUSTED
    assert episode.trust is Trust.UNTRUSTED


async def test_an_untainted_run_may_store_trusted_and_untrusted_text() -> None:
    writer, _, items, _ = _services()
    trusted = await writer.write_item(_draft(), tainted=False, now=NOW)
    cautious = await writer.write_item(
        _draft(content="From a newsletter", source=EMAIL, trust=Trust.UNTRUSTED),
        tainted=False,
        now=NOW,
    )
    assert (trusted.trust, cautious.trust) == (Trust.TRUSTED, Trust.UNTRUSTED)
    assert trusted.embedding_model == "fake-bag-of-words"
    assert len(items.rows) == 2


# Space binding


async def test_a_writer_refuses_another_space() -> None:
    writer, _, items, episodes = _services(Space.PERSONAL)
    with pytest.raises(SpaceMismatchError):
        await writer.write_item(_draft(Space.SYNAPTION), tainted=False, now=NOW)
    with pytest.raises(SpaceMismatchError):
        await writer.write_episode(_episode(Space.SYNAPTION), tainted=False)
    assert items.rows == []
    assert episodes.episodes == []


async def test_results_from_another_space_are_refused_even_if_a_store_returns_them() -> None:
    writer, _, items, episodes = _services(Space.SYNAPTION)
    await writer.write_item(_draft(Space.SYNAPTION, "Launch is in May"), tainted=False, now=NOW)
    await writer.write_episode(_episode(Space.SYNAPTION), tainted=False)

    class LeakyItems(InMemorySemanticStore):
        async def search(self, **kwargs: Any) -> list[MemoryMatch]:
            merged: dict[str, Any] = {**kwargs, "space": Space.SYNAPTION}
            return await super().search(**merged)

    class LeakyEpisodes(InMemoryEpisodicStore):
        async def recent(self, **kwargs: Any) -> Any:
            merged: dict[str, Any] = {**kwargs, "space": Space.SYNAPTION}
            return await super().recent(**merged)

    reader = MemoryReader(
        Space.PERSONAL,
        items=LeakyItems(rows=items.rows),
        episodes=LeakyEpisodes(episodes=episodes.episodes),
        embedder=FakeEmbeddingProvider(),
    )
    with pytest.raises(SpaceMismatchError):
        await reader.search("launch", limit=5, now=NOW, include_untrusted=True)
    with pytest.raises(SpaceMismatchError):
        await reader.recent_episodes("thread:1", limit=5, include_untrusted=True)


async def test_a_writer_refuses_what_a_store_wrote_elsewhere() -> None:
    class WritesElsewhere(InMemorySemanticStore):
        async def add(self, draft: MemoryDraft, **kwargs: Any) -> Any:
            return await super().add(_draft(Space.SYNAPTION, draft.content), **kwargs)

    class EpisodesElsewhere(InMemoryEpisodicStore):
        async def add(self, draft: EpisodeDraft) -> Any:
            return await super().add(_episode(Space.SYNAPTION, run_id=draft.run_id))

    writer = MemoryWriter(
        Space.PERSONAL,
        items=WritesElsewhere(),
        episodes=EpisodesElsewhere(),
        embedder=FakeEmbeddingProvider(),
    )
    with pytest.raises(SpaceMismatchError):
        await writer.write_item(_draft(), tainted=False, now=NOW)
    with pytest.raises(SpaceMismatchError):
        await writer.write_episode(_episode(), tainted=False)


# Writing and reading


async def test_search_ranks_by_similarity_and_respects_trust_and_expiry() -> None:
    writer, reader, _, _ = _services()
    tea = await writer.write_item(_draft(content="Drinks green tea"), tainted=False, now=NOW)
    await writer.write_item(_draft(content="Cycles to work"), tainted=False, now=NOW)
    offer = await writer.write_item(
        _draft(content="Tea offer: reply now", source=EMAIL, trust=Trust.UNTRUSTED),
        tainted=True,
        now=NOW,
    )
    short = await writer.write_item(
        _draft(content="Tea tasting on Sunday", expires_at=NOW + timedelta(days=1)),
        tainted=False,
        now=NOW,
    )

    matches = await reader.search("green tea", limit=10, now=NOW, include_untrusted=True)
    assert matches[0].item.id == tea.id
    assert [m.distance for m in matches] == sorted(m.distance for m in matches)
    assert {offer.id, short.id} <= {m.item.id for m in matches}

    trusted = await reader.search("green tea", limit=10, now=NOW, include_untrusted=False)
    assert offer.id not in {m.item.id for m in trusted}
    later = await reader.search(
        "green tea", limit=10, now=NOW + timedelta(days=2), include_untrusted=True
    )
    assert short.id not in {m.item.id for m in later}
    assert len(await reader.search("tea", limit=1, now=NOW, include_untrusted=True)) == 1


async def test_content_already_covered_is_not_embedded_or_stored_again() -> None:
    embedder = CountingEmbedder()
    writer, _, items, _ = _services(embedder=embedder)
    first = await writer.write_item(_draft(), tainted=False, now=NOW)
    second = await writer.write_item(_draft(), tainted=False, now=NOW)
    weaker = await writer.write_item(_draft(trust=Trust.UNTRUSTED), tainted=True, now=NOW)
    assert first.id == second.id == weaker.id
    assert embedder.calls == 1
    assert len(items.rows) == 1


async def test_expiry_must_be_in_the_future_and_now_must_be_aware() -> None:
    writer, reader, _, _ = _services()
    with pytest.raises(ValueError, match="future"):
        await writer.write_item(_draft(expires_at=NOW), tainted=False, now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        await writer.write_item(
            _draft(expires_at=NOW + timedelta(days=1)),
            tainted=False,
            now=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await writer.write_item(_draft(), tainted=False, now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        await reader.search("tea", limit=1, now=NOW.replace(tzinfo=None), include_untrusted=True)


async def test_episodes_are_recalled_by_topic_newest_first() -> None:
    writer, reader, _, episodes = _services()
    first = await writer.write_episode(_episode(summary="First"), tainted=False)
    second = await writer.write_episode(_episode(summary="Second"), tainted=False)
    hidden = await writer.write_episode(_episode(trust=Trust.UNTRUSTED), tainted=True)
    await writer.write_episode(_episode(topic="thread:2"), tainted=False)
    running = uuid4()
    episodes.running.add(running)
    await writer.write_episode(_episode(run_id=running), tainted=False)

    recalled = await reader.recent_episodes("thread:1", limit=10, include_untrusted=True)
    assert [e.id for e in recalled] == [hidden.id, second.id, first.id]
    trusted = await reader.recent_episodes("thread:1", limit=1, include_untrusted=False)
    assert [e.id for e in trusted] == [second.id]
    assert (
        await writer.write_episode(
            _episode(run_id=first.run_id, summary="Rewritten"), tainted=False
        )
        == first
    )


@pytest.mark.parametrize("limit", [0, -1, MAX_RECALL + 1])
async def test_limits_are_bounded(limit: int) -> None:
    _, reader, _, _ = _services()
    with pytest.raises(ValueError, match="limit"):
        await reader.search("tea", limit=limit, now=NOW, include_untrusted=True)
    with pytest.raises(ValueError, match="limit"):
        await reader.recent_episodes("thread:1", limit=limit, include_untrusted=True)


async def test_queries_and_topics_are_validated() -> None:
    _, reader, _, _ = _services()
    for query in ("", "  ", "x" * 2001):
        with pytest.raises(ValueError, match="query"):
            await reader.search(query, limit=1, now=NOW, include_untrusted=True)
    for topic in ("", "Not A Key", "thread:1\n"):
        with pytest.raises(ValueError, match="topic"):
            await reader.recent_episodes(topic, limit=1, include_untrusted=True)


# Stores that misbehave


async def test_a_store_that_ignores_the_filters_is_caught() -> None:
    writer, _, items, episodes = _services()
    await writer.write_item(
        _draft(content="Wire the money", source=EMAIL, trust=Trust.UNTRUSTED),
        tainted=True,
        now=NOW,
    )
    await writer.write_episode(_episode(trust=Trust.UNTRUSTED), tainted=True)

    class IgnoresTrust(InMemorySemanticStore):
        async def search(self, **kwargs: Any) -> list[MemoryMatch]:
            merged: dict[str, Any] = {**kwargs, "include_untrusted": True}
            return await super().search(**merged)

    class IgnoresTrustEpisodes(InMemoryEpisodicStore):
        async def recent(self, **kwargs: Any) -> Any:
            merged: dict[str, Any] = {**kwargs, "include_untrusted": True}
            return await super().recent(**merged)

    class IgnoresModel(InMemorySemanticStore):
        async def search(self, **kwargs: Any) -> list[MemoryMatch]:
            merged: dict[str, Any] = {**kwargs, "embedding_model": "fake-bag-of-words"}
            return await super().search(**merged)

    reader = MemoryReader(
        Space.PERSONAL,
        items=IgnoresTrust(rows=items.rows),
        episodes=IgnoresTrustEpisodes(episodes=episodes.episodes),
        embedder=FakeEmbeddingProvider(),
    )
    with pytest.raises(ProvenanceError):
        await reader.search("money", limit=5, now=NOW, include_untrusted=False)
    with pytest.raises(ProvenanceError):
        await reader.recent_episodes("thread:1", limit=5, include_untrusted=False)

    other_model = MemoryReader(
        Space.PERSONAL,
        items=IgnoresModel(rows=items.rows),
        episodes=episodes,
        embedder=FakeEmbeddingProvider(model_id="another-model"),
    )
    with pytest.raises(EmbeddingError, match="different embedding models"):
        await other_model.search("money", limit=5, now=NOW, include_untrusted=True)


# Embeddings


def test_a_provider_with_another_dimension_is_refused() -> None:
    small = FakeEmbeddingProvider(dim=8)
    items, episodes = InMemorySemanticStore(), InMemoryEpisodicStore()
    with pytest.raises(EmbeddingError, match="8 dimensions"):
        MemoryWriter(Space.PERSONAL, items=items, episodes=episodes, embedder=small)
    with pytest.raises(EmbeddingError, match="8 dimensions"):
        MemoryReader(Space.PERSONAL, items=items, episodes=episodes, embedder=small)
    with pytest.raises(ValueError, match="positive"):
        FakeEmbeddingProvider(dim=0)


@pytest.mark.parametrize(
    ("vectors", "message"),
    [
        ([[1.0] * 3], "expected 384 dimensions"),
        ([[math.nan] * EMBEDDING_DIM], "NaN or infinity"),
        ([[math.inf] + [0.0] * (EMBEDDING_DIM - 1)], "NaN or infinity"),
        ([[0.0] * EMBEDDING_DIM], "zero vector"),
        ([[1.0] * EMBEDDING_DIM, [1.0] * EMBEDDING_DIM], "expected 1 embedding"),
    ],
)
async def test_unusable_embeddings_are_refused(vectors: list[list[float]], message: str) -> None:
    writer, reader, items, _ = _services(embedder=BrokenEmbedder(vectors))
    with pytest.raises(EmbeddingError, match=message):
        await writer.write_item(_draft(), tainted=False, now=NOW)
    assert items.rows == []
    if len(vectors) == 1:
        with pytest.raises(EmbeddingError, match=message):
            await reader.search("tea", limit=1, now=NOW, include_untrusted=True)


def test_the_fake_provider_is_deterministic_and_groups_similar_text() -> None:
    provider = FakeEmbeddingProvider()
    tea, tea_again, bike = provider.embed_documents(
        ["green tea in the morning", "green tea in the morning", "the bike needs new brakes"]
    )
    query = provider.embed_query("morning tea")
    assert tea == tea_again
    assert len(tea) == provider.dim == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(v * v for v in tea)), 1.0)
    assert cosine_distance(query, tea) < cosine_distance(query, bike)
    assert math.isclose(sum(v * v for v in provider.embed_query("")), 1.0)
    assert math.isclose(sum(v * v for v in provider.embed_query("?!")), 1.0)
