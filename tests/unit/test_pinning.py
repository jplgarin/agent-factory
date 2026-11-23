import re
from pathlib import Path

from integrations.embeddings.models import CANDIDATES, DEFAULT_MODEL

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40} # v\d")


def _images() -> list[str]:
    refs: list[str] = []
    for path in (ROOT / "infra").rglob("*"):
        if not path.is_file():
            continue
        stages: set[str] = set()
        for line in path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("image:"):
                refs.append(stripped.removeprefix("image:").strip())
            elif path.name.startswith("Dockerfile") and stripped.upper().startswith("FROM "):
                words = stripped.split()
                # `FROM deps AS build` builds on an earlier stage of the same file, not an image.
                if words[1] not in stages:
                    refs.append(words[1])
                if len(words) == 4 and words[2].upper() == "AS":
                    stages.add(words[3])
    return refs


def test_every_image_is_pinned_to_a_digest() -> None:
    images = [i for i in _images() if i != "agent-factory:dev"]
    assert images
    assert [i for i in images if not DIGEST.search(i)] == []


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    uses = [
        line.strip().removeprefix("- ").removeprefix("uses:").strip()
        for wf in (ROOT / ".github" / "workflows").glob("*.y*ml")
        for line in wf.read_text().splitlines()
        if line.strip().removeprefix("- ").startswith("uses:")
    ]
    assert uses
    assert [u for u in uses if not ACTION_SHA.match(u)] == []


def test_every_embedding_model_is_pinned_to_a_commit() -> None:
    """A model whose files change gives different vectors, so it is pinned like an image."""
    assert DEFAULT_MODEL in CANDIDATES
    for model in CANDIDATES:
        assert model.revision is not None
        assert re.fullmatch(r"[0-9a-f]{40}", model.revision), model.name
        assert model.model_id == f"{model.name}@{model.revision[:12]}"
    dockerfile = (ROOT / "infra" / "Dockerfile").read_text()
    assert "integrations.embeddings.fetch download /opt/af/models" in dockerfile
    assert "integrations.embeddings.fetch check /opt/af/models" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert dockerfile.index("USER 10001") < dockerfile.index("fetch check")
