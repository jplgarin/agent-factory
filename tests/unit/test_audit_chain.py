import hashlib
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from core.audit.chain import (
    GENESIS_HASH,
    AuditEvent,
    AuditEventDraft,
    AuditEventType,
    ChainHead,
    canonical_json,
    event_hash,
    format_timestamp,
    verify_chain,
)
from core.domain.space import Space

T0 = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
TASK = UUID("00000000-0000-4000-8000-000000000001")


def _chain(space: Space, n: int) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    prev = GENESIS_HASH
    for seq in range(1, n + 1):
        fields = {
            "space": space,
            "seq": seq,
            "occurred_at": T0 + timedelta(seconds=seq),
            "event_type": AuditEventType.TOOL_CALL.value,
            "actor": "email.personal",
            "task_id": TASK if seq % 2 else None,
            "run_id": None,
            "payload": canonical_json({"n": seq}),
            "prev_hash": prev,
        }
        digest = event_hash(**fields)
        events.append(AuditEvent(**fields, hash=digest))
        prev = digest
    return events


def _head(events: list[AuditEvent]) -> ChainHead:
    return ChainHead(space=events[0].space, seq=events[-1].seq, hash=events[-1].hash)


def test_hash_matches_the_documented_construction() -> None:
    """Pins the format in docs/DATABASE.md; the database tests check SQL computes the same."""

    def h(text: str) -> bytes:
        return hashlib.sha256(text.encode()).digest()

    payload = canonical_json({"b": 1, "a": "\u00e9"})
    assert payload == '{"a":"\u00e9","b":1}'
    expected = hashlib.sha256(
        GENESIS_HASH
        + h("personal")
        + h("1")
        + h("2026-01-02T03:04:05.000006Z")
        + h("task_created")
        + h("api")
        + h(str(TASK))
        + h("")
        + h(payload)
    ).digest()
    actual = event_hash(
        prev_hash=GENESIS_HASH,
        space=Space.PERSONAL,
        seq=1,
        occurred_at=T0,
        event_type="task_created",
        actor="api",
        task_id=TASK,
        run_id=None,
        payload=payload,
    )
    assert actual == expected


def test_timestamps_are_normalized_to_utc() -> None:
    plus_two = T0.astimezone(timezone(timedelta(hours=2)))
    assert format_timestamp(plus_two) == "2026-01-02T03:04:05.000006Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        format_timestamp(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="32 bytes"):
        _chain(Space.PERSONAL, 1)[0].model_copy(update={"prev_hash": b"x"}).computed_hash()


def test_valid_chain_and_empty_chain_verify() -> None:
    events = _chain(Space.PERSONAL, 5)
    result = verify_chain(Space.PERSONAL, events, _head(events))
    assert result.valid
    assert result.events_checked == 5
    empty = verify_chain(
        Space.PERSONAL, [], ChainHead(space=Space.PERSONAL, seq=0, hash=GENESIS_HASH)
    )
    assert empty.valid


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload", canonical_json({"n": 99})),
        ("actor", "someone.else"),
        ("event_type", "result"),
        ("occurred_at", T0),
        ("task_id", None),
        ("run_id", TASK),
    ],
)
def test_edited_event_is_detected(field: str, value: object) -> None:
    events = _chain(Space.PERSONAL, 5)
    events[2] = events[2].model_copy(update={field: value})
    result = verify_chain(Space.PERSONAL, events, _head(events))
    assert not result.valid
    assert result.first_invalid_seq == 3
    assert result.events_checked == 2


def test_rehashed_event_breaks_the_next_link() -> None:
    events = _chain(Space.PERSONAL, 5)
    forged = events[2].model_copy(update={"payload": canonical_json({"n": 0})})
    events[2] = forged.model_copy(update={"hash": forged.computed_hash()})
    result = verify_chain(Space.PERSONAL, events)
    assert result.first_invalid_seq == 4
    assert result.reason is not None
    assert "prev_hash" in result.reason


def test_removed_reordered_and_truncated_events_are_detected() -> None:
    events = _chain(Space.PERSONAL, 5)
    head = _head(events)
    removed = verify_chain(Space.PERSONAL, [*events[:2], *events[3:]], head)
    assert (removed.valid, removed.first_invalid_seq) == (False, 3)
    swapped = verify_chain(Space.PERSONAL, [events[1], events[0], *events[2:]], head)
    assert (swapped.valid, swapped.first_invalid_seq) == (False, 1)
    truncated = verify_chain(Space.PERSONAL, events[:4], head)
    assert (truncated.valid, truncated.first_invalid_seq) == (False, 5)
    assert truncated.reason == "chain head does not match the last event"


def test_events_and_heads_from_another_space_are_rejected() -> None:
    events = _chain(Space.SYNAPTION, 2)
    assert not verify_chain(Space.PERSONAL, events).valid
    other_head = ChainHead(space=Space.SYNAPTION, seq=0, hash=GENESIS_HASH)
    assert not verify_chain(Space.PERSONAL, [], other_head).valid


def test_draft_serializes_payload_canonically() -> None:
    draft = AuditEventDraft(
        space=Space.PERSONAL,
        event_type=AuditEventType.POLICY_DECISION,
        actor="policy",
        payload={"z": [1, 2], "a": {"y": None}},
    )
    assert draft.payload_text() == '{"a":{"y":null},"z":[1,2]}'
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"x": float("nan")})
