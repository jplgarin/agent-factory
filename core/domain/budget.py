from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Budget(BaseModel):
    """Hard limits for one task. Exceeding any of them stops the task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_cost_usd: Decimal = Field(gt=0, max_digits=12, decimal_places=6)
    timeout_s: int = Field(gt=0)
