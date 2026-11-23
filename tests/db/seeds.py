"""One insert per space-scoped table, run as a space role. test_isolation checks this covers every
table the catalog reports, so a new table needs a seed here before its isolation is tested."""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable

import asyncpg

from core.domain.space import Space

Seed = Callable[[asyncpg.Connection, Space], Awaitable[None]]
BUDGET = json.dumps({"max_steps": 4, "max_tokens": 1000, "max_cost_usd": "0.1", "timeout_s": 60})


async def task(
    conn: asyncpg.Connection, space: Space, parent: uuid.UUID | None = None
) -> uuid.UUID:
    task_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO tasks (id, space, origin, requested_by, agent_id, input, priority, budget,"
        " status, parent_task_id) VALUES ($1, $2, $3, 'owner', $4, '{}', 'normal', $5,"
        " 'pending', $6)",
        task_id,
        space.value,
        "agent" if parent else "api",
        f"email.{space.value}",
        BUDGET,
        parent,
    )
    return task_id


async def run(
    conn: asyncpg.Connection, space: Space, task_id: uuid.UUID | None = None
) -> uuid.UUID:
    task_id = task_id or await task(conn, space)
    run_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO runs (id, space, task_id, agent_id, status, started_at)"
        " VALUES ($1, $2, $3, $4, 'running', now())",
        run_id,
        space.value,
        task_id,
        f"email.{space.value}",
    )
    return run_id


async def step(conn: asyncpg.Connection, space: Space, run_id: uuid.UUID | None = None) -> None:
    run_id = run_id or await run(conn, space)
    await conn.execute(
        "INSERT INTO steps (id, space, run_id, step_no, kind, name, input, input_tokens,"
        " output_tokens, cost_usd, latency_ms) VALUES ($1, $2, $3, 0, 'tool', 'mail.search',"
        " '{}', 0, 0, 0, 5)",
        uuid.uuid4(),
        space.value,
        run_id,
    )


async def approval(
    conn: asyncpg.Connection,
    space: Space,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> None:
    task_id = task_id or await task(conn, space)
    run_id = run_id or await run(conn, space, task_id)
    await conn.execute(
        "INSERT INTO approvals (id, space, task_id, run_id, payload, status, requested_at,"
        " expires_at) VALUES ($1, $2, $3, $4, '{}', 'pending', now(), now() + interval '1 day')",
        uuid.uuid4(),
        space.value,
        task_id,
        run_id,
    )


async def message_content(conn: asyncpg.Connection, space: Space) -> None:
    body = f"synthetic body {uuid.uuid4()}".encode()
    await conn.execute(
        "INSERT INTO message_content (space, content_hash, content) VALUES ($1, $2, $3)",
        space.value,
        hashlib.sha256(body).digest(),
        body,
    )


async def audit_event(conn: asyncpg.Connection, space: Space) -> None:
    await conn.execute(
        "INSERT INTO audit_events (space, event_type, actor, payload)"
        " VALUES ($1, 'task_created', 'test', '{}')",
        space.value,
    )


async def _task(conn: asyncpg.Connection, space: Space) -> None:
    await task(conn, space)


async def _run(conn: asyncpg.Connection, space: Space) -> None:
    await run(conn, space)


async def _approval(conn: asyncpg.Connection, space: Space) -> None:
    await approval(conn, space)


async def _chain_head(conn: asyncpg.Connection, space: Space) -> None:
    # Heads are created by the migration; nobody inserts them. Appending moves the head.
    await audit_event(conn, space)


SEEDS: dict[str, Seed] = {
    "tasks": _task,
    "runs": _run,
    "steps": step,
    "approvals": _approval,
    "message_content": message_content,
    "audit_events": audit_event,
    "audit_chain_heads": _chain_head,
}
