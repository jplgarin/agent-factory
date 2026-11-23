"""Create .env from .env.example, or add keys that a newer .env.example introduced.

Never changes an existing value. Empty password keys get a random value.
"""

import os
import re
import secrets
import sys
from pathlib import Path

GENERATED = (
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "AF_OWNER_DB_PASSWORD",
    "AF_PERSONAL_DB_PASSWORD",
    "AF_SYNAPTION_DB_PASSWORD",
)
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _fill(key: str, value: str) -> str:
    if key in GENERATED and value == "":
        return secrets.token_urlsafe(24)
    return value


def keys(text: str) -> set[str]:
    return {m.group(1) for line in text.splitlines() if (m := _ASSIGNMENT.match(line))}


def new_env(example: str) -> str:
    lines = []
    for line in example.splitlines():
        match = _ASSIGNMENT.match(line)
        lines.append(f"{match[1]}={_fill(match[1], match[2])}" if match else line)
    return "\n".join(lines) + "\n"


def missing_lines(example: str, existing: str) -> list[str]:
    present = keys(existing)
    return [
        f"{m[1]}={_fill(m[1], m[2])}"
        for line in example.splitlines()
        if (m := _ASSIGNMENT.match(line)) and m[1] not in present
    ]


def main(root: Path) -> str:
    example = (root / ".env.example").read_text()
    env = root / ".env"
    if not env.exists():
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(new_env(example))
        return "created .env"
    existing = env.read_text()
    added = missing_lines(example, existing)
    if not added:
        return ".env is up to date"
    with env.open("a") as handle:
        handle.write(("" if existing.endswith("\n") else "\n") + "\n".join(added) + "\n")
    names = ", ".join(line.split("=", 1)[0] for line in added)
    return f"added to .env: {names}"


if __name__ == "__main__":
    print(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()))
