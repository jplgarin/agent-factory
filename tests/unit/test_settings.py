from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.shared.settings import GatewaySettings, WorkerSettings
from core.domain.space import Space


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AF_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AF_DATABASE_URL", "postgresql://u:hunter2@db:5432/x")
    monkeypatch.setenv("AF_REDIS_URL", "redis://:hunter2@redis:6379/0")
    return tmp_path


def test_gateway_defaults_bind_loopback(base_env: Path) -> None:
    settings = GatewaySettings()  # pyright: ignore[reportCallIssue]
    assert settings.host == "127.0.0.1"
    assert settings.config_dir == base_env


def test_secrets_never_appear_in_repr(base_env: Path) -> None:
    settings = GatewaySettings()  # pyright: ignore[reportCallIssue]
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())


def test_missing_config_dir_is_rejected(base_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_CONFIG_DIR", str(base_env / "missing"))
    with pytest.raises(ValidationError):
        GatewaySettings()  # pyright: ignore[reportCallIssue]


def test_worker_requires_a_known_space(base_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings()  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("AF_SPACE", "other")
    with pytest.raises(ValidationError):
        WorkerSettings()  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("AF_SPACE", "synaption")
    settings = WorkerSettings()  # pyright: ignore[reportCallIssue]
    assert settings.space is Space.SYNAPTION


def test_space_maps_to_its_own_namespace_and_queue() -> None:
    assert Space.PERSONAL.temporal_namespace == "af-personal"
    assert Space.SYNAPTION.task_queue == "af-synaption"
