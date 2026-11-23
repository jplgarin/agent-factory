from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from core.domain.space import Space


class StepKind(StrEnum):
    LLM = "llm"
    TOOL = "tool"


class Step(BaseModel):
    """One LLM call or tool call inside a run. Outputs are stored redacted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    space: Space
    run_id: UUID
    step_no: int = Field(ge=0)
    kind: StepKind
    name: str = Field(min_length=1, max_length=200)
    model: str | None = None
    prompt_version: str | None = None
    input: dict[str, JsonValue]
    output: dict[str, JsonValue] | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=6)
    latency_ms: int = Field(default=0, ge=0)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind is StepKind.LLM:
            if self.model is None or self.prompt_version is None:
                raise ValueError("an LLM step records its model and prompt version")
        elif self.model is not None or self.prompt_version is not None:
            raise ValueError("a tool step has no model or prompt version")
        return self
