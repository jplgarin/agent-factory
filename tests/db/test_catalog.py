"""ADR 0002 catalog checks: every space-scoped table is under forced RLS for both space roles."""

import asyncpg

from core.domain.space import Space
from tests.db.catalog import all_tables, space_tables
from tests.db.conftest import Database

APP_ROLES = ("af_owner", "af_personal", "af_synaption", "af_retention")


async def test_every_table_except_alembic_version_is_space_scoped(
    database: Database, superuser: asyncpg.Connection
) -> None:
    scoped = await space_tables(superuser)
    assert scoped
    assert set(await all_tables(superuser)) - set(scoped) == {"alembic_version"}


async def test_rls_is_enabled_and_forced_on_every_space_table(
    superuser: asyncpg.Connection,
) -> None:
    rows = await superuser.fetch(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
        " WHERE relname = ANY($1::text[]) AND relnamespace = 'public'::regnamespace",
        await space_tables(superuser),
    )
    assert rows
    assert [
        r["relname"] for r in rows if not (r["relrowsecurity"] and r["relforcerowsecurity"])
    ] == []


async def test_every_space_table_has_a_policy_for_each_space_role(
    superuser: asyncpg.Connection,
) -> None:
    for table in await space_tables(superuser):
        for space in Space:
            policies = await superuser.fetch(
                "SELECT qual, with_check FROM pg_policies"
                " WHERE schemaname = 'public' AND tablename = $1 AND $2 = ANY(roles)",
                table,
                space.db_role,
            )
            assert policies, f"{table} has no policy for {space.db_role}"
            own = f"'{space.value}'::space"
            for policy in policies:
                for expression in (policy["qual"], policy["with_check"]):
                    assert expression is None or own in expression, (table, expression)
                assert policy["qual"] or policy["with_check"]


async def test_no_policy_applies_to_public(superuser: asyncpg.Connection) -> None:
    rows = await superuser.fetch(
        "SELECT tablename, policyname FROM pg_policies WHERE 'public' = ANY(roles)"
    )
    assert rows == []


async def test_af_owner_owns_every_table_and_function(superuser: asyncpg.Connection) -> None:
    tables = await superuser.fetch(
        "SELECT relname, pg_get_userbyid(relowner) AS owner FROM pg_class"
        " WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p', 'S', 'v', 'm')"
    )
    functions = await superuser.fetch(
        "SELECT proname, pg_get_userbyid(proowner) AS owner FROM pg_proc"
        " WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'audit%'"
    )
    assert tables
    assert len(functions) == 4
    assert {r["owner"] for r in [*tables, *functions]} == {"af_owner"}


async def test_app_roles_are_unprivileged_and_unrelated(superuser: asyncpg.Connection) -> None:
    rows = await superuser.fetch(
        "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication,"
        " rolcanlogin FROM pg_roles WHERE rolname = ANY($1::text[])",
        list(APP_ROLES),
    )
    assert {r["rolname"] for r in rows} == set(APP_ROLES)
    for row in rows:
        assert not any(row[k] for k in ("rolsuper", "rolbypassrls", "rolcreaterole")), row
        assert not row["rolcreatedb"], row
        assert not row["rolreplication"], row
    assert {r["rolname"] for r in rows if r["rolcanlogin"]} == {
        "af_owner",
        "af_personal",
        "af_synaption",
    }
    memberships = await superuser.fetch(
        "SELECT r.rolname, m.rolname FROM pg_auth_members am"
        " JOIN pg_roles r ON r.oid = am.roleid JOIN pg_roles m ON m.oid = am.member"
        " WHERE r.rolname = ANY($1::text[]) OR m.rolname = ANY($1::text[])",
        list(APP_ROLES),
    )
    assert memberships == []


async def test_space_roles_cannot_change_audit_events(superuser: asyncpg.Connection) -> None:
    for space in Space:
        for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not await superuser.fetchval(
                "SELECT has_table_privilege($1, 'audit_events', $2)", space.db_role, privilege
            ), (space, privilege)
        for column in ("seq", "occurred_at", "prev_hash", "hash"):
            assert not await superuser.fetchval(
                "SELECT has_column_privilege($1, 'audit_events', $2, 'INSERT')",
                space.db_role,
                column,
            ), (space, column)


async def test_public_has_no_access_to_the_database(
    database: Database, superuser: asyncpg.Connection
) -> None:
    assert not await superuser.fetchval(
        "SELECT has_database_privilege('public', $1, 'CONNECT')", database.name
    )
    assert not await superuser.fetchval("SELECT has_schema_privilege('public', 'public', 'CREATE')")
    for role in ("af_personal", "af_synaption", "af_retention"):
        assert not await superuser.fetchval(
            "SELECT has_schema_privilege($1, 'public', 'CREATE')", role
        )
