import asyncpg

SPACE_TABLES_QUERY = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'space' AND NOT a.attisdropped
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""


async def space_tables(conn: asyncpg.Connection) -> list[str]:
    return [row["relname"] for row in await conn.fetch(SPACE_TABLES_QUERY)]


async def all_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
        " ORDER BY c.relname"
    )
    return [row["relname"] for row in rows]


def sqlstate(error: BaseException) -> str | None:
    """asyncpg sets `sqlstate` on each PostgresError subclass at runtime."""
    return getattr(error, "sqlstate", None)
