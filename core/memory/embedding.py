"""Embedding interface (ADR 0004). Implementations live under integrations/."""

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

# Dimension of the pgvector column (docs/DATABASE.md). A model with another dimension needs a
# new column and a re-embed, as ADR 0004 describes.
EMBEDDING_DIM = 384

_WORD = re.compile(r"\w+")


class EmbeddingProvider(Protocol):
    """Turns text into vectors. Documents and queries are embedded separately because some
    models expect a different prefix for each."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbeddingProvider:
    """Deterministic bag-of-words vectors for tests: texts that share words are close."""

    def __init__(self, *, model_id: str = "fake-bag-of-words", dim: int = EMBEDDING_DIM) -> None:
        if dim < 1:
            raise ValueError("dim must be positive")
        self._model_id = model_id
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # No words, or words that cancelled out: still return a unit vector.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]
