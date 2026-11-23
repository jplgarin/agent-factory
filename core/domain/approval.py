from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from core.domain.space import Space

APPROVAL_TTL = timedelta(hours=24)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalError(Exception):
    pass


class Approval(BaseModel):
    """A human decision on one proposed action. `payload` is exactly what the human was shown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    space: Space
    task_id: UUID
    run_id: UUID
    payload: dict[str, JsonValue]
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: AwareDatetime
    expires_at: AwareDatetime
    decided_by: str | None = Field(default=None, min_length=1, max_length=200)
    decided_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must follow requested_at")
        if (self.status is ApprovalStatus.PENDING) != (self.decided_at is None):
            raise ValueError("decided_at is set if and only if the approval is resolved")
        human = self.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)
        if human != (self.decided_by is not None):
            raise ValueError("decided_by is set if and only if a human decided")
        if self.decided_at is not None and self.decided_at < self.requested_at:
            raise ValueError("decided_at precedes requested_at")
        return self

    @classmethod
    def request(
        cls,
        *,
        space: Space,
        task_id: UUID,
        run_id: UUID,
        payload: dict[str, JsonValue],
        at: datetime,
    ) -> "Approval":
        return cls(
            space=space,
            task_id=task_id,
            run_id=run_id,
            payload=payload,
            requested_at=at,
            expires_at=at + APPROVAL_TTL,
        )

    def is_expired(self, at: datetime) -> bool:
        return at >= self.expires_at

    def decide(self, *, approved: bool, by: str, at: datetime) -> "Approval":
        if self.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"approval already {self.status.value}")
        if self.is_expired(at):
            raise ApprovalError("approval expired")
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        return self._resolved(status, by, at)

    def expire(self, at: datetime) -> "Approval":
        if self.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"approval already {self.status.value}")
        if not self.is_expired(at):
            raise ApprovalError("approval has not expired yet")
        return self._resolved(ApprovalStatus.EXPIRED, None, at)

    def _resolved(self, status: ApprovalStatus, by: str | None, at: datetime) -> "Approval":
        data = self.model_dump() | {"status": status, "decided_by": by, "decided_at": at}
        return self.model_validate(data)
