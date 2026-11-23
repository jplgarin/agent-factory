from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"


class ToolDefinition(BaseModel):
    """A tool the runtime can execute. The input model validates every call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    description: str = Field(min_length=1)
    input_model: type[BaseModel]
    risk: ToolRisk
    timeout_s: float = Field(gt=0, le=600)
    max_output_chars: int = Field(gt=0, le=200_000)
    idempotent: bool
