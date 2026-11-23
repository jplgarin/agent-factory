"""Download the pinned embedding model into a directory, or check that it loads from one.

Used when the image is built (infra/Dockerfile):

    python -m integrations.embeddings.fetch download /opt/af/models
    python -m integrations.embeddings.fetch check /opt/af/models

`download` needs the network. `check` must work without it, as the running containers do.
"""

import argparse
import math
from pathlib import Path

from integrations.embeddings.fastembed_provider import FastembedProvider
from integrations.embeddings.models import DEFAULT_MODEL, EmbeddingModel

# The tokenizer and configuration files fastembed reads next to the model file.
SUPPORT_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


def download(model: EmbeddingModel, models_dir: Path) -> Path:  # pragma: no cover
    """Fetch the model's files at its pinned revision into `models_dir`."""
    from fastembed import TextEmbedding
    from huggingface_hub import snapshot_download

    if model.revision is None:
        raise ValueError(f"{model.name} is not pinned to a revision")
    description = next(
        entry for entry in TextEmbedding.list_supported_models() if entry["model"] == model.name
    )
    target = models_dir / model.directory
    snapshot_download(
        repo_id=description["sources"]["hf"],
        revision=model.revision,
        local_dir=target,
        allow_patterns=[
            *SUPPORT_FILES,
            description["model_file"],
            *description["additional_files"],
        ],
    )
    return target


def check(model: EmbeddingModel, models_dir: Path) -> str:
    """Load the model from `models_dir` and embed one sentence in each supported language."""
    provider = FastembedProvider(model, models_dir=models_dir)
    vectors = provider.embed_documents(
        ["The invoice is due on Friday.", "La factura vence el viernes."]
    )
    query = provider.embed_query("When is the invoice due?")
    for vector in (*vectors, query):
        norm = math.sqrt(sum(value * value for value in vector))
        if len(vector) != model.dim or not math.isfinite(norm) or norm == 0:
            raise SystemExit(f"{provider.model_id} returned an unusable vector")
    return f"{provider.model_id}: {model.dim} dimensions, loaded from {models_dir}"


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Download or check the pinned embedding model.")
    parser.add_argument("action", choices=("download", "check"))
    parser.add_argument("models_dir", type=Path)
    arguments = parser.parse_args()
    if arguments.action == "download":
        print(download(DEFAULT_MODEL, arguments.models_dir))
    else:
        print(check(DEFAULT_MODEL, arguments.models_dir))


if __name__ == "__main__":  # pragma: no cover
    main()
