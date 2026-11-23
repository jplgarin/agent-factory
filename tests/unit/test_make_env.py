import stat
from pathlib import Path

from scripts.make_env import GENERATED, keys, main

EXAMPLE = "# comment\nPOSTGRES_USER=af_dev\nPOSTGRES_PASSWORD=\nAF_OWNER_DB_PASSWORD=\n# X=1\n"


def _values(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if line and line[0] != "#")


def test_creates_env_with_random_passwords_and_mode_0600(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE)
    assert main(tmp_path) == "created .env"
    env = tmp_path / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    values = _values(env.read_text())
    assert values["POSTGRES_USER"] == "af_dev"
    assert len(values["POSTGRES_PASSWORD"]) >= 32
    assert values["POSTGRES_PASSWORD"] != values["AF_OWNER_DB_PASSWORD"]
    assert "# X=1" in env.read_text()


def test_adds_only_missing_keys_and_keeps_existing_values(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE)
    (tmp_path / ".env").write_text("POSTGRES_USER=custom\nPOSTGRES_PASSWORD=kept")
    assert main(tmp_path) == "added to .env: AF_OWNER_DB_PASSWORD"
    values = _values((tmp_path / ".env").read_text())
    assert values["POSTGRES_USER"] == "custom"
    assert values["POSTGRES_PASSWORD"] == "kept"
    assert len(values["AF_OWNER_DB_PASSWORD"]) >= 32
    assert main(tmp_path) == ".env is up to date"


def test_every_generated_key_is_in_the_real_example() -> None:
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert set(GENERATED) <= keys(example)


def test_recognises_export_and_spaced_assignments(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE)
    existing = "export POSTGRES_USER=af_dev\nPOSTGRES_PASSWORD = kept\n  AF_OWNER_DB_PASSWORD=x\n"
    (tmp_path / ".env").write_text(existing)
    assert main(tmp_path) == ".env is up to date"
    assert (tmp_path / ".env").read_text() == existing
