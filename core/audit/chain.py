"""Per-space audit hash chain (SPEC 4.7, ADR 0002).

hash = SHA-256(prev_hash || H(space) || H(seq) || H(occurred_at) || H(event_type) || H(actor)
               || H(task_id) || H(run_id) || H(payload))

where H(x) is the SHA-256 of the UTF-8 text of x, `occurred_at` is UTC with microseconds
(`2026-01-02T03:04:05.123456Z`), absent IDs are the empty string and `payload` is the stored
canonical JSON text. The database computes the same function in `audit_event_hash` (see
docs/DATABASE.md); `verify_chain` recomputes it independently.
"""

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from core.domain.space import Space

HASH_SIZE = 32
GENESIS_HASH = bytes(HASH_SIZE)


class AuditEventType(StrEnum):
    TASK_CREATED = "task_created"
    CONTEXT_ASSEMBLED = "context_assembled"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    POLICY_DECISION = "policy_decision"
    APPROVAL = "approval"
    RESULT = "result"
    BUDGET_USAGE = "budget_usage"


def canonical_json(value: dict[str, JsonValue]) -> str:
    """Deterministic JSON text: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _field(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def event_hash(
    *,
    prev_hash: bytes,
    space: Space,
    seq: int,
    occurred_at: datetime,
    event_type: str,
    actor: str,
    task_id: UUID | None,
    run_id: UUID | None,
    payload: str,
) -> bytes:
    if len(prev_hash) != HASH_SIZE:
        raise ValueError("prev_hash must be 32 bytes")
    fields = (
        space.value,
        str(seq),
        format_timestamp(occurred_at),
        event_type,
        actor,
        "" if task_id is None else str(task_id),
        "" if run_id is None else str(run_id),
        payload,
    )
    digest = hashlib.sha256(prev_hash)
    for value in fields:
        digest.update(_field(value))
    return digest.digest()


class AuditEventDraft(BaseModel):
    """What a writer supplies. The database assigns seq, occurred_at, prev_hash and hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: Space
    event_type: AuditEventType
    actor: str = Field(min_length=1, max_length=200)
    task_id: UUID | None = None
    run_id: UUID | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])

    def payload_text(self) -> str:
        return canonical_json(self.payload)


class AuditEvent(BaseModel):
    """A stored, chained audit event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: Space
    seq: int = Field(ge=1)
    occurred_at: AwareDatetime
    event_type: str
    actor: str
    task_id: UUID | None
    run_id: UUID | None
    payload: str
    prev_hash: bytes
    hash: bytes

    def computed_hash(self) -> bytes:
        return event_hash(
            prev_hash=self.prev_hash,
            space=self.space,
            seq=self.seq,
            occurred_at=self.occurred_at,
            event_type=self.event_type,
            actor=self.actor,
            task_id=self.task_id,
            run_id=self.run_id,
            payload=self.payload,
        )


class ChainHead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    space: Space
    seq: int = Field(ge=0)
    hash: bytes


class ChainVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    events_checked: int
    first_invalid_seq: int | None = None
    reason: str | None = None


def _invalid(checked: int, seq: int | None, reason: str) -> ChainVerification:
    return ChainVerification(
        valid=False, events_checked=checked, first_invalid_seq=seq, reason=reason
    )


def verify_chain(
    space: Space, events: Iterable[AuditEvent], head: ChainHead | None = None
) -> ChainVerification:
    """Verify a full chain from seq 1. Detects edited, removed, reordered and truncated events."""
    expected_seq = 1
    prev = GENESIS_HASH
    checked = 0
    for event in events:
        if event.space is not space:
            return _invalid(checked, event.seq, "event from another space")
        if event.seq != expected_seq:
            return _invalid(checked, expected_seq, f"expected seq {expected_seq}, got {event.seq}")
        if event.prev_hash != prev:
            return _invalid(checked, event.seq, "prev_hash does not match the previous event")
        if event.computed_hash() != event.hash:
            return _invalid(checked, event.seq, "hash does not match the event's contents")
        prev = event.hash
        expected_seq += 1
        checked += 1
    if head is not None:
        if head.space is not space:
            return _invalid(checked, None, "chain head from another space")
        if head.seq != checked or head.hash != prev:
            return _invalid(checked, checked + 1, "chain head does not match the last event")
    return ChainVerification(valid=True, events_checked=checked)
