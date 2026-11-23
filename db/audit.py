from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from core.audit.chain import AuditEvent, AuditEventDraft, ChainHead
from core.domain.space import Space
from db.models import AuditChainHeadRow, AuditEventRow

_EVENT_COLUMNS = (
    AuditEventRow.space,
    AuditEventRow.seq,
    AuditEventRow.occurred_at,
    AuditEventRow.event_type,
    AuditEventRow.actor,
    AuditEventRow.task_id,
    AuditEventRow.run_id,
    AuditEventRow.payload,
    AuditEventRow.prev_hash,
    AuditEventRow.hash,
)


async def append_event(conn: AsyncConnection, draft: AuditEventDraft) -> AuditEvent:
    """Append one event. The database chains it under the space's head lock."""
    statement = (
        insert(AuditEventRow)
        .values(
            space=draft.space,
            event_type=draft.event_type.value,
            actor=draft.actor,
            task_id=draft.task_id,
            run_id=draft.run_id,
            payload=draft.payload_text(),
        )
        .returning(*_EVENT_COLUMNS)
    )
    row = (await conn.execute(statement)).one()
    return AuditEvent.model_validate(row._asdict())


async def load_chain(conn: AsyncConnection, space: Space) -> tuple[list[AuditEvent], ChainHead]:
    """Read a space's events and head. Call inside a REPEATABLE READ transaction so both
    come from one snapshot; otherwise a concurrent append makes them disagree."""
    head_row = (
        await conn.execute(
            select(AuditChainHeadRow.space, AuditChainHeadRow.seq, AuditChainHeadRow.hash).where(
                AuditChainHeadRow.space == space
            )
        )
    ).one()
    rows = await conn.execute(
        select(*_EVENT_COLUMNS).where(AuditEventRow.space == space).order_by(AuditEventRow.seq)
    )
    events = [AuditEvent.model_validate(row._asdict()) for row in rows]
    return events, ChainHead.model_validate(head_row._asdict())
