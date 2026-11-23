"""ADR 0002: no app container receives the Postgres superuser credentials."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "infra" / "docker-compose.yml"
APP_IMAGE = "agent-factory:dev"
SUPERUSER_VARS = {"POSTGRES_USER", "POSTGRES_PASSWORD"}
SUPERUSER_SERVICES = {"postgres", "db-roles", "temporal-schema", "temporal"}
DB_PASSWORDS = {"AF_OWNER_DB_PASSWORD", "AF_PERSONAL_DB_PASSWORD", "AF_SYNAPTION_DB_PASSWORD"}
# Which role passwords each app container may receive.
ALLOWED_DB_PASSWORDS = {
    "db-migrate": {"AF_OWNER_DB_PASSWORD"},
    "gateway": {"AF_PERSONAL_DB_PASSWORD", "AF_SYNAPTION_DB_PASSWORD"},
    "worker-personal": {"AF_PERSONAL_DB_PASSWORD"},
    "worker-synaption": {"AF_SYNAPTION_DB_PASSWORD"},
}
_VAR = re.compile(r"\$\{([A-Z0-9_]+)")
_URL_USER = re.compile(r"postgresql://([^:@/]+)")


def _services() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [str(value)]


def _variables(service: dict[str, Any]) -> set[str]:
    text = " ".join(_strings({k: service.get(k) for k in ("environment", "command", "entrypoint")}))
    return set(_VAR.findall(text))


def test_app_services_are_known() -> None:
    apps = {name for name, s in _services().items() if s.get("image") == APP_IMAGE}
    assert apps == set(ALLOWED_DB_PASSWORDS)


def test_only_infrastructure_receives_superuser_credentials() -> None:
    holders = {name for name, s in _services().items() if _variables(s) & SUPERUSER_VARS}
    assert holders <= SUPERUSER_SERVICES
    assert "db-roles" in holders


def test_each_app_container_gets_only_its_own_role() -> None:
    for name, service in _services().items():
        granted = _variables(service) & DB_PASSWORDS
        if name == "db-roles":
            assert granted == DB_PASSWORDS
        else:
            assert granted == ALLOWED_DB_PASSWORDS.get(name, set()), name


def test_database_urls_log_in_as_the_matching_role() -> None:
    expected = {
        "db-migrate": {"af_owner"},
        "gateway": {"af_personal", "af_synaption"},
        "worker-personal": {"af_personal"},
        "worker-synaption": {"af_synaption"},
    }
    for name, roles in expected.items():
        urls = _strings(_services()[name].get("environment"))
        assert {user for url in urls for user in _URL_USER.findall(url)} == roles, name
