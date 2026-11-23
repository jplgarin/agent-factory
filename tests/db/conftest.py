"""A throwaway Postgres per test session (SPEC 6), set up exactly as the stack does it:
infra/postgres/roles.sql as the superuser, then the Alembic migrations as af_owner."""

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest
import yaml

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.community.postgres import PostgresContainer

from core.domain.space import Space
from db.migrate import upgrade

ROOT = Path(__file__).resolve().parents[2]
SUPERUSER = "af_test_super"
ROLES = ("af_owner", "af_personal", "af_synaption")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.db)


def _postgres_image() -> str:
    compose = yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text())
    return compose["services"]["postgres"]["image"]


@dataclass(frozen=True)
class Database:
    host: str
    port: int
    name: str
    passwords: dict[str, str]

    def url(self, role: str) -> str:
        return f"postgresql://{role}:{self.passwords[role]}@{self.host}:{self.port}/{self.name}"

    def space_url(self, space: Space) -> str:
        return self.url(space.db_role)

    async def connect(self, role: str) -> asyncpg.Connection:
        return await asyncpg.connect(self.url(role))


@dataclass(frozen=True)
class Server:
    container: PostgresContainer
    host: str
    port: int
    passwords: dict[str, str]

    def bootstrap(self, name: str) -> Database:
        """Create database `name` if needed, run roles.sql in it, then migrate to head."""
        psql = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", SUPERUSER]
        wrapped = self.container.get_wrapped_container()
        if name != "agent_factory":
            code, out = wrapped.exec_run([*psql, "-d", "postgres", "-c", f"CREATE DATABASE {name}"])
            assert code == 0, str(out)
        variables = [
            "-v",
            f"owner_password={self.passwords['af_owner']}",
            "-v",
            f"personal_password={self.passwords['af_personal']}",
            "-v",
            f"synaption_password={self.passwords['af_synaption']}",
        ]
        code, out = wrapped.exec_run([*psql, "-d", name, *variables, "-f", "/af/roles.sql"])
        assert code == 0, str(out)
        database = Database(self.host, self.port, name, self.passwords)
        upgrade(database.url("af_owner"))
        return database


@pytest.fixture(scope="session")
def server() -> Iterator[Server]:
    passwords = {role: secrets.token_urlsafe(18) for role in (*ROLES, SUPERUSER)}
    container = PostgresContainer(
        _postgres_image(),
        username=SUPERUSER,
        password=passwords[SUPERUSER],
        dbname="agent_factory",
        driver=None,
    ).with_volume_mapping(str(ROOT / "infra" / "postgres"), "/af", "ro")
    # Publish on loopback only, on a random port (docker-py accepts an (ip, port) tuple).
    container.ports = {port: ("127.0.0.1", None) for port in container.ports}  # pyright: ignore[reportAttributeAccessIssue]
    with container:
        host = "127.0.0.1"
        port = int(container.get_exposed_port(5432))
        yield Server(container, host, port, passwords)


@pytest.fixture(scope="session")
def database(server: Server) -> Database:
    """Shared, migrated database. Tests must not break its audit chains."""
    return server.bootstrap("agent_factory")


@pytest.fixture
def fresh_database(server: Server) -> Callable[[], Awaitable[Database]]:
    """A new migrated database, for tests that tamper with data or run migrations.

    Alembic runs its own event loop, so the bootstrap runs in a thread."""

    async def make() -> Database:
        return await asyncio.to_thread(server.bootstrap, f"af_{secrets.token_hex(4)}")

    return make


@pytest.fixture
async def superuser(database: Database, server: Server) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(
        f"postgresql://{SUPERUSER}:{server.passwords[SUPERUSER]}"
        f"@{database.host}:{database.port}/{database.name}"
    )
    yield conn
    await conn.close()
