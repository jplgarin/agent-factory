import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = [ROOT / "compose.yaml", ROOT / "infra" / "docker-compose.yml"]
IPV4_PREFIX = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):")
LOOPBACK = {"127.0.0.1"}


def _host_ip(entry: Any) -> str | None:
    """Return the host IP of a published port, or None if it binds every interface."""
    if isinstance(entry, dict):
        return entry.get("host_ip")
    text = str(entry)
    match = IPV4_PREFIX.match(text)
    return match.group(1) if match else None


def port_violations(compose: dict[str, Any]) -> list[str]:
    bad: list[str] = []
    for name, service in (compose.get("services") or {}).items():
        if service.get("network_mode") == "host":
            bad.append(f"{name}: network_mode host")
        for entry in service.get("ports") or []:
            if _host_ip(entry) not in LOOPBACK:
                bad.append(f"{name}: {entry}")
    return bad


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_every_published_port_binds_loopback(path: Path) -> None:
    compose = yaml.safe_load(path.read_text())
    assert port_violations(compose) == []


def test_main_compose_publishes_ports() -> None:
    compose = yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text())
    published = [s for s in compose["services"].values() if s.get("ports")]
    assert published, "the check above would pass vacuously"


@pytest.mark.parametrize(
    "entry",
    [
        "8080:8080",
        "8080",
        "0.0.0.0:8080:8080",
        "${HOST_IP:-127.0.0.1}:8080:8080",
        {"target": 8080, "published": 8080},
        {"target": 8080, "published": 8080, "host_ip": "0.0.0.0"},
        "[::1]:8080:8080",
        {"target": 8080, "host_ip": "::1"},
    ],
)
def test_checker_rejects_non_loopback(entry: Any) -> None:
    assert port_violations({"services": {"s": {"ports": [entry]}}})


@pytest.mark.parametrize(
    "entry",
    ["127.0.0.1:8080:8080", {"target": 1, "host_ip": "127.0.0.1"}],
)
def test_checker_accepts_loopback(entry: Any) -> None:
    assert port_violations({"services": {"s": {"ports": [entry]}}}) == []


def test_checker_rejects_host_network() -> None:
    assert port_violations({"services": {"s": {"network_mode": "host"}}})
