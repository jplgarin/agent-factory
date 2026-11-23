from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from core.domain.agent import parse_instance_id
from core.domain.budget import Budget
from core.domain.space import Space


class TaskOrigin(StrEnum):
    API = "api"
    SCHEDULE = "schedule"
    AGENT = "agent"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ESCALATED = "escalated"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in _OPEN_TASK_STATUSES


_OPEN_TASK_STATUSES = frozenset(
    {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
)


class Task(BaseModel):
    """A unit of work for one agent instance in one space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    space: Space
    origin: TaskOrigin
    requested_by: str = Field(min_length=1, max_length=200)
    agent_id: str
    input: dict[str, JsonValue]
    priority: TaskPriority = TaskPriority.NORMAL
    budget: Budget
    status: TaskStatus = TaskStatus.PENDING
    parent_task_id: UUID | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        _, agent_space = parse_instance_id(self.agent_id)
        if agent_space is not self.space:
            raise ValueError("agent_id must name an instance in the task's space")
        if (self.origin is TaskOrigin.AGENT) != (self.parent_task_id is not None):
            raise ValueError("a task has a parent if and only if an agent delegated it")
        if self.parent_task_id == self.id:
            raise ValueError("a task cannot be its own parent")
        return self


class RunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    ESCALATED = "escalated"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"


class Run(BaseModel):
    """One execution of a task by the agent runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    space: Space
    task_id: UUID
    agent_id: str
    status: RunStatus = RunStatus.RUNNING
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _, agent_space = parse_instance_id(self.agent_id)
        if agent_space is not self.space:
            raise ValueError("agent_id must name an instance in the run's space")
        if (self.status is RunStatus.RUNNING) != (self.finished_at is None):
            raise ValueError("finished_at is set if and only if the run has finished")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self

    def finish(self, status: RunStatus, at: datetime) -> "Run":
        if self.status is not RunStatus.RUNNING:
            raise ValueError("run already finished")
        if status is RunStatus.RUNNING:
            raise ValueError("finish needs a final status")
        return self.model_validate(self.model_dump() | {"status": status, "finished_at": at})
