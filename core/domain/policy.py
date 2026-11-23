from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(BaseModel):
    """The outcome of the policy engine for one proposed action, with the rule that decided it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Decision
    rule: str = Field(min_length=1)
    reason: str = Field(min_length=1)
