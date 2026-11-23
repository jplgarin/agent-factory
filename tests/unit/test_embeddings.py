"""ADR 0004: the local embedding provider and the set its candidates are compared on.

The real models are large downloads, so they are not loaded here. The image build loads the
pinned model offline (infra/Dockerfile) and the Embedding benchmark workflow runs all three.
"""

from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from core.memory.embedding import EMBEDDING_DIM, EmbeddingProvider
from integrations.embeddings import fetch
from integrations.embeddings.fastembed_provider import (
    EmbeddingModelError,
    FastembedProvider,
    load_from_directory,
)
from integrations.embeddings.models import CANDIDATES, DEFAULT_MODEL, E5_LARGE, EmbeddingModel
from scripts.embedding_benchmark import load_dataset, ranks, score, summarise, table

PINNED = EmbeddingModel(
    name="example/tiny-model", dim=3, revision="0123456789abcdef" * 2 + "01234567"
)


class StubEngine:
    def __init__(self, vectors: list[list[float]] | None = None) -> None:
        self.vectors = vectors
        self.seen: list[str] = []
        self.batch_size = 0

    def embed(self, documents: Iterable[str], batch_size: int = 0) -> Iterable[Any]:
        texts = list(documents)
        self.seen.extend(texts)
        self.batch_size = batch_size
        if self.vectors is not None:
            return iter(self.vectors)
        return iter([float(len(text)), 1.0, 0.0] for text in texts)


def _provider(model: EmbeddingModel = PINNED, engine: StubEngine | None = None) -> Any:
    engine = engine or StubEngine()
    seen: dict[str, Any] = {}

    def loader(loaded: EmbeddingModel, model_dir: Path, threads: int | None) -> StubEngine:
        seen.update(model=loaded, model_dir=model_dir, threads=threads)
        return engine

    provider = FastembedProvider(model, models_dir=Path("/models"), threads=2, loader=loader)
    return provider, engine, seen


def test_the_provider_identifies_the_exact_model_files() -> None:
    provider, _, seen = _provider()
    checked: EmbeddingProvider = provider
    assert checked.model_id == "example/tiny-model@0123456789ab"
    assert checked.dim == 3
    assert seen == {
        "model": PINNED,
        "model_dir": Path("/models/example--tiny-model"),
        "threads": 2,
    }


def test_documents_and_queries_are_embedded_as_plain_floats() -> None:
    provider, engine, _ = _provider()
    assert provider.embed_documents(["ab", "abcd"]) == [[2.0, 1.0, 0.0], [4.0, 1.0, 0.0]]
    assert provider.embed_query("abc") == [3.0, 1.0, 0.0]
    assert engine.seen == ["ab", "abcd", "abc"]
    assert engine.batch_size == 32
    assert provider.embed_documents([]) == []


def test_models_that_need_prefixes_get_them() -> None:
    model = EmbeddingModel(
        name="example/prefixed",
        dim=3,
        revision="f" * 40,
        query_prefix="query: ",
        document_prefix="passage: ",
    )
    provider, engine, _ = _provider(model)
    provider.embed_documents(["one"])
    provider.embed_query("two")
    assert engine.seen == ["passage: one", "query: two"]
    assert (E5_LARGE.query_prefix, E5_LARGE.document_prefix) == ("query: ", "passage: ")


@pytest.mark.parametrize(
    ("vectors", "message"),
    [([[1.0, 2.0]], "did not return 3 values"), ([], "asked for 1 vectors, got 0")],
)
def test_vectors_that_do_not_match_the_model_are_refused(
    vectors: list[list[float]], message: str
) -> None:
    provider, _, _ = _provider(engine=StubEngine(vectors))
    with pytest.raises(EmbeddingModelError, match=message):
        provider.embed_query("x")


def test_an_unpinned_model_cannot_be_used() -> None:
    unpinned = EmbeddingModel(name="example/unpinned", dim=3)
    with pytest.raises(ValueError, match="not pinned"):
        _provider(unpinned)


def test_loading_never_downloads_and_fails_clearly_without_files(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingModelError, match="no model files"):
        load_from_directory(DEFAULT_MODEL, tmp_path / "missing", None)
    empty = tmp_path / DEFAULT_MODEL.directory
    empty.mkdir()
    with pytest.raises(EmbeddingModelError, match="cannot load"):
        load_from_directory(DEFAULT_MODEL, empty, None)


def test_the_shipped_model_fits_the_schema_and_candidates_are_distinct() -> None:
    assert DEFAULT_MODEL in CANDIDATES
    assert DEFAULT_MODEL.dim == EMBEDDING_DIM
    assert len({model.name for model in CANDIDATES}) == len(CANDIDATES) == 3
    assert DEFAULT_MODEL.directory == DEFAULT_MODEL.name.replace("/", "--")
    assert "/" not in DEFAULT_MODEL.directory


def test_the_offline_check_embeds_both_languages(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = StubEngine()
    monkeypatch.setattr(
        fetch,
        "FastembedProvider",
        lambda model, models_dir: FastembedProvider(
            model, models_dir=models_dir, loader=lambda *_: engine
        ),
    )
    assert fetch.check(PINNED, Path("/models")) == (
        "example/tiny-model@0123456789ab: 3 dimensions, loaded from /models"
    )
    assert len(engine.seen) == 3

    class ZeroEngine(StubEngine):
        def embed(self, documents: Iterable[str], batch_size: int = 0) -> Iterable[Any]:
            return iter([0.0, 0.0, 0.0] for _ in documents)

    engine = ZeroEngine()
    with pytest.raises(SystemExit, match="unusable vector"):
        fetch.check(PINNED, Path("/models"))


# The benchmark set and its scoring


def test_the_recall_set_is_balanced_and_consistent() -> None:
    documents, queries = load_dataset()
    ids = [document["id"] for document in documents]
    language = {document["id"]: document["lang"] for document in documents}
    assert len(ids) == len(set(ids)) == 40
    assert Counter(language.values()) == {"en": 20, "es": 20}
    assert len({document["text"] for document in documents}) == 40
    assert len({query["text"] for query in queries}) == len(queries) == 40
    pairs = Counter((query["lang"], language[query["relevant"]]) for query in queries)
    assert pairs == {("en", "en"): 10, ("es", "es"): 10, ("en", "es"): 10, ("es", "en"): 10}


def test_scoring_counts_ranks_per_language_pair() -> None:
    documents = [
        {"id": "a", "lang": "en", "text": "a"},
        {"id": "b", "lang": "es", "text": "b"},
        {"id": "c", "lang": "en", "text": "c"},
    ]
    queries = [
        {"lang": "en", "relevant": "a", "text": "q1"},
        {"lang": "en", "relevant": "b", "text": "q2"},
        {"lang": "es", "relevant": "c", "text": "q3"},
    ]
    similarity = [[0.9, 0.1, 0.2], [0.5, 0.4, 0.1], [0.3, 0.3, 0.3]]
    assert ranks(similarity, [0, 1, 2]) == [1, 2, 3]
    result = score(documents, queries, similarity)
    assert result["all"] == {"queries": 3, "top1": 1, "top3": 3, "mrr": 0.611}
    assert result["same_language"] == {"queries": 1, "top1": 1, "top3": 1, "mrr": 1.0}
    assert result["cross_language"] == {"queries": 2, "top1": 0, "top3": 2, "mrr": 0.417}
    assert summarise([]) == {"queries": 0, "top1": 0, "top3": 0, "mrr": 0.0}

    row = {
        "model": "example/tiny-model",
        "dim": 3,
        "files_mb": 12,
        "peak_rss_mb": 345,
        "load_s": 1.5,
        "documents_per_s": 80.0,
        "query_ms": 4.2,
        "scores": result,
    }
    lines = table([row]).splitlines()
    assert lines[2] == (
        "| `example/tiny-model` | 3 | 12 MB | 345 MB | 1.5 s | 80.0 | 4.2 ms | "
        "1/3 | 3/3 | 0.611 | 1/1 | 0/2 |"
    )
