"""Local embeddings through fastembed and ONNX Runtime (ADR 0004). No text leaves the host."""

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from integrations.embeddings.models import EmbeddingModel

BATCH_SIZE = 32


class EmbeddingModelError(Exception):
    """The model could not be loaded, or returned vectors that are not what it declares."""


class _Engine(Protocol):
    def embed(self, documents: Iterable[str], batch_size: int = ...) -> Iterable[Any]: ...


Loader = Callable[[EmbeddingModel, Path, int | None], _Engine]


def load_from_directory(model: EmbeddingModel, model_dir: Path, threads: int | None) -> _Engine:
    """Load the model's files from `model_dir`. Never downloads: the files are put in the
    image when it is built, so a start cannot fail on a download."""
    from fastembed import TextEmbedding

    if not model_dir.is_dir():
        raise EmbeddingModelError(f"no model files for {model.name} at {model_dir}")
    try:
        return TextEmbedding(
            model_name=model.name,
            specific_model_path=str(model_dir),
            threads=threads,
            local_files_only=True,
        )
    except Exception as error:
        raise EmbeddingModelError(f"cannot load {model.name} from {model_dir}") from error


class FastembedProvider:
    """`core.memory.embedding.EmbeddingProvider` backed by a local model."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        models_dir: Path,
        threads: int | None = None,
        loader: Loader = load_from_directory,
    ) -> None:
        self._model = model
        self._model_id = model.model_id
        self._engine = loader(model, models_dir / model.directory, threads)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._model.dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([self._model.document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._model.query_prefix + text])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors = [
            [float(value) for value in vector]
            for vector in self._engine.embed(texts, batch_size=BATCH_SIZE)
        ]
        if len(vectors) != len(texts):
            raise EmbeddingModelError(f"asked for {len(texts)} vectors, got {len(vectors)}")
        if any(len(vector) != self._model.dim for vector in vectors):
            raise EmbeddingModelError(f"{self._model.name} did not return {self._model.dim} values")
        return vectors
