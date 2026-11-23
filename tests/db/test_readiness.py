import asyncpg
import pytest

from apps.shared.checks import DatabaseRoleError, database_check
from core.domain.space import Space
from tests.db.conftest import SUPERUSER, Database, Server


async def test_ready_only_as_the_expected_space_role(database: Database, server: Server) -> None:
    for space in Space:
        await database_check(database.space_url(space), space.db_role)()
    with pytest.raises(DatabaseRoleError, match="wrong role"):
        await database_check(database.space_url(Space.PERSONAL), "af_synaption")()
    superuser = (
        f"postgresql://{SUPERUSER}:{server.passwords[SUPERUSER]}"
        f"@{database.host}:{database.port}/{database.name}"
    )
    with pytest.raises(DatabaseRoleError, match="wrong role"):
        await database_check(superuser, "af_personal")()
    with pytest.raises(DatabaseRoleError, match="bypasses"):
        await database_check(superuser, SUPERUSER)()


async def test_other_databases_are_closed_to_space_roles(database: Database) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await asyncpg.connect(database.space_url(Space.PERSONAL).rsplit("/", 1)[0] + "/postgres")
