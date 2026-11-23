"""The embedding models Agent Factory knows (ADR 0004).

The model that ships is pinned to one commit of its Hugging Face repository, as images and
actions are pinned to digests: a model whose files change produces different vectors, and
vectors of different models cannot be compared. The pinned commit is part of `model_id`, which
is stored with every vector.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingModel:
    name: str  # as fastembed names it
    dim: int
    revision: str | None = None  # commit of the Hugging Face repository, 40 hex digits
    query_prefix: str = ""
    document_prefix: str = ""

    @property
    def model_id(self) -> str:
        """Identifies the exact model files. Stored with every vector."""
        if self.revision is None:
            raise ValueError(f"{self.name} is not pinned to a revision")
        return f"{self.name}@{self.revision[:12]}"

    @property
    def directory(self) -> str:
        """Folder name of the model's files below the models directory."""
        return self.name.replace("/", "--")


MINILM = EmbeddingModel(
    name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    dim=384,
)
MPNET = EmbeddingModel(
    name="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
    dim=768,
)
E5_LARGE = EmbeddingModel(
    name="intfloat/multilingual-e5-large",
    dim=1024,
    # The model card asks for these prefixes; fastembed does not add them.
    query_prefix="query: ",
    document_prefix="passage: ",
)

# Compared in scripts/embedding_benchmark.py. Only DEFAULT_MODEL is installed in the image.
CANDIDATES = (MINILM, MPNET, E5_LARGE)
DEFAULT_MODEL = MINILM
