import asyncio
import os

from alembic import context
from sqlalchemy import Connection, pool, text

from db.engine import create_engine
from db.models import Base

MIGRATION_ROLE = "af_owner"
config = context.config


def _url() -> str:
    url = config.attributes.get("url") or os.environ.get("AF_MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("set AF_MIGRATION_DATABASE_URL")
    return url


def _migrate(connection: Connection) -> None:
    role = connection.execute(text("SELECT current_user")).scalar_one()
    if role != MIGRATION_ROLE:
        # Tables must be owned by af_owner, never by a superuser or an app role (ADR 0002).
        raise RuntimeError(f"migrations must run as {MIGRATION_ROLE}, not {role}")
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        transaction_per_migration=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
asyncio.run(_run())
