from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def async_url(url: str) -> str:
    """Accept a plain `postgresql://` DSN and select the asyncpg driver."""
    parsed = make_url(url)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.render_as_string(hide_password=False)


def create_engine(url: str, **kwargs: Any) -> AsyncEngine:
    return create_async_engine(async_url(url), **kwargs)
