import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
RESERVED = re.compile(r"(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(test|example|invalid))$")
SCANNED = ["config.example", "README.md", ".env.example", "infra", "tests"]


def _files() -> list[Path]:
    out: list[Path] = []
    for name in SCANNED:
        path = ROOT / name
        out.extend(p for p in ([path] if path.is_file() else path.rglob("*")) if p.is_file())
    return out


def test_only_reserved_example_domains_in_addresses() -> None:
    offenders = {
        f"{path.relative_to(ROOT)}: {match.group(0)}"
        for path in _files()
        for match in EMAIL.finditer(path.read_text(errors="ignore"))
        if not RESERVED.search(match.group(1).lower())
    }
    assert offenders == set()


def test_config_example_has_both_spaces() -> None:
    spaces = ROOT / "config.example" / "spaces"
    assert {p.name for p in spaces.iterdir() if p.is_dir()} == {"personal", "synaption"}
