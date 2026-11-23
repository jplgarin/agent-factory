import re
from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.domain.budget import Budget
from core.domain.space import Space

AGENT_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
_INSTANCE_ID = re.compile(r"^(?P<agent>[a-z][a-z0-9_]*)\.(?P<space>[a-z]+)$")


def instance_id(agent_id: str, space: Space) -> str:
    """The ID of an agent's instance in one space, for example `email.personal`."""
    return f"{agent_id}.{space.value}"


def parse_instance_id(value: str) -> tuple[str, Space]:
    match = _INSTANCE_ID.match(value)
    if match is None:
        raise ValueError(f"not an agent instance ID: {value!r}")
    return match["agent"], Space(match["space"])


class AgentSpec(BaseModel):
    """Declarative agent definition. One spec may run as one instance per listed space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=AGENT_ID_PATTERN)
    spaces: tuple[Space, ...] = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    model: str = Field(min_length=1)
    system_prompt_path: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    output_schema: str = Field(min_length=1)
    budgets: Budget
    permissions: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    can_delegate_to: tuple[str, ...] = ()
    max_delegation_depth: int = Field(default=2, ge=0, le=5)

    @field_validator("spaces")
    @classmethod
    def _unique_spaces(cls, value: tuple[Space, ...]) -> tuple[Space, ...]:
        if len(set(value)) != len(value):
            raise ValueError("spaces must not repeat")
        return value

    @field_validator("system_prompt_path")
    @classmethod
    def _relative_prompt_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("system_prompt_path must be relative to the config directory")
        return value

    @model_validator(mode="after")
    def _no_self_delegation(self) -> Self:
        if self.id in self.can_delegate_to:
            raise ValueError("an agent cannot delegate to itself")
        return self

    def instance_id(self, space: Space) -> str:
        if space not in self.spaces:
            raise ValueError(f"agent {self.id} does not run in space {space.value}")
        return instance_id(self.id, space)
