"""SQLAlchemy models. Row-level security, grants and triggers live in the migrations (ADR 0002)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.domain.approval import ApprovalStatus
from core.domain.space import Space
from core.domain.step import StepKind
from core.domain.task import RunStatus, TaskOrigin, TaskPriority, TaskStatus

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _enum[E: StrEnum](enum: type[E], name: str) -> Enum:
    return Enum(enum, name=name, values_callable=lambda members: [m.value for m in members])


SPACE = _enum(Space, "space")
JSON = JSONB(none_as_null=True)
TIMESTAMP = DateTime(timezone=True)
HASH_CHECK = "octet_length({}) = 32"
TRIGGER_SET = FetchedValue()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("space", "id"),
        ForeignKeyConstraint(["space", "parent_task_id"], ["tasks.space", "tasks.id"]),
        CheckConstraint(
            "(origin = 'agent') = (parent_task_id IS NOT NULL)", name="parent_iff_agent"
        ),
        CheckConstraint("parent_task_id <> id", name="not_own_parent"),
        CheckConstraint("agent_id LIKE ('%.' || space::text)", name="agent_in_space"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    space: Mapped[Space] = mapped_column(SPACE)
    origin: Mapped[TaskOrigin] = mapped_column(_enum(TaskOrigin, "task_origin"))
    requested_by: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    priority: Mapped[TaskPriority] = mapped_column(_enum(TaskPriority, "task_priority"))
    budget: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus, "task_status"))
    parent_task_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("space", "id"),
        ForeignKeyConstraint(["space", "task_id"], ["tasks.space", "tasks.id"]),
        CheckConstraint("agent_id LIKE ('%.' || space::text)", name="agent_in_space"),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="finished_iff_done"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    space: Mapped[Space] = mapped_column(SPACE)
    task_id: Mapped[UUID]
    agent_id: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"))
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class StepRow(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_no"),
        ForeignKeyConstraint(["space", "run_id"], ["runs.space", "runs.id"]),
        CheckConstraint("step_no >= 0", name="step_no_non_negative"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0 AND latency_ms >= 0",
            name="usage_non_negative",
        ),
        CheckConstraint(
            "CASE kind WHEN 'llm' THEN model IS NOT NULL AND prompt_version IS NOT NULL"
            " ELSE model IS NULL AND prompt_version IS NULL END",
            name="llm_fields",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    space: Mapped[Space] = mapped_column(SPACE)
    run_id: Mapped[UUID]
    step_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[StepKind] = mapped_column(_enum(StepKind, "step_kind"))
    name: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(["space", "task_id"], ["tasks.space", "tasks.id"]),
        ForeignKeyConstraint(["space", "run_id"], ["runs.space", "runs.id"]),
        CheckConstraint("expires_at > requested_at", name="expires_after_request"),
        CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)", name="decided_at_iff_resolved"
        ),
        CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_by IS NOT NULL)",
            name="decided_by_iff_human",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    space: Mapped[Space] = mapped_column(SPACE)
    task_id: Mapped[UUID]
    run_id: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[ApprovalStatus] = mapped_column(_enum(ApprovalStatus, "approval_status"))
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class MessageContentRow(Base):
    """Email bodies, kept apart from the audit log so retention can delete them (PLAN G2)."""

    __tablename__ = "message_content"
    __table_args__ = (
        CheckConstraint("content_hash = sha256(content)", name="hash_matches_content"),
    )

    space: Mapped[Space] = mapped_column(SPACE, primary_key=True)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class AuditEventRow(Base):
    """Append-only. A trigger assigns seq, occurred_at, prev_hash and hash (docs/DATABASE.md)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(HASH_CHECK.format("prev_hash"), name="prev_hash_size"),
        CheckConstraint(HASH_CHECK.format("hash"), name="hash_size"),
        CheckConstraint("jsonb_typeof(payload::jsonb) = 'object'", name="payload_is_object"),
    )

    space: Mapped[Space] = mapped_column(SPACE, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, server_default=TRIGGER_SET)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=TRIGGER_SET)
    event_type: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    task_id: Mapped[UUID | None]
    run_id: Mapped[UUID | None]
    payload: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, server_default=TRIGGER_SET)
    hash: Mapped[bytes] = mapped_column(LargeBinary, server_default=TRIGGER_SET)


class AuditChainHeadRow(Base):
    __tablename__ = "audit_chain_heads"
    __table_args__ = (
        CheckConstraint("seq >= 0", name="seq_non_negative"),
        CheckConstraint(HASH_CHECK.format("hash"), name="hash_size"),
    )

    space: Mapped[Space] = mapped_column(SPACE, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    hash: Mapped[bytes] = mapped_column(LargeBinary)
