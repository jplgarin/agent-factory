import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40} # v\d")


def _images() -> list[str]:
    refs: list[str] = []
    for path in (ROOT / "infra").rglob("*"):
        if not path.is_file():
            continue
        for line in path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("image:"):
                refs.append(stripped.removeprefix("image:").strip())
            elif path.name.startswith("Dockerfile") and stripped.upper().startswith("FROM "):
                refs.append(stripped.split()[1])
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
