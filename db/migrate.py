from alembic import command
from alembic.config import Config


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", "db:migrations")
    if url is not None:
        config.attributes["url"] = url
    return config


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)
