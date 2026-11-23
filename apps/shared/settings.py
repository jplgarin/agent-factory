from pathlib import Path
from typing import Literal

from pydantic import DirectoryPath, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.domain.space import Space

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Settings shared by every process. Read from AF_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="AF_", extra="ignore", frozen=True)

    env: Literal["local", "ci", "production"] = "local"
    log_level: LogLevel = "INFO"
    config_dir: DirectoryPath
    redis_url: SecretStr
    temporal_address: str = "localhost:7233"
    check_timeout_s: float = Field(default=2.0, gt=0, le=30)


class GatewaySettings(Settings):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    # The gateway serves both spaces, so it holds both space roles and never the owner (ADR 0002).
    database_url_personal: SecretStr
    database_url_synaption: SecretStr

    def database_urls(self) -> dict[Space, SecretStr]:
        return {
            Space.PERSONAL: self.database_url_personal,
            Space.SYNAPTION: self.database_url_synaption,
        }


class WorkerSettings(Settings):
    space: Space
    # Must log in as this space's role (Space.db_role); readiness fails otherwise.
    database_url: SecretStr
    health_host: str = "127.0.0.1"
    health_port: int = Field(default=8081, ge=0, le=65535)
    # Where the image keeps the embedding model (ADR 0004). Nothing is downloaded at run time.
    embedding_models_dir: Path = Path("/opt/af/models")
