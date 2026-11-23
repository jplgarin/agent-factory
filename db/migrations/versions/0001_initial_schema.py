"""Initial schema: tasks, runs, steps, approvals, audit chain, message content.

Row-level security, grants and the audit chain triggers follow ADR 0002 and docs/DATABASE.md.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SPACE_ROLES = {"personal": "af_personal", "synaption": "af_synaption"}
# Tables where each space role reads and writes only its own rows.
SPACE_TABLES = {
    "tasks": ("SELECT", "INSERT", "UPDATE"),
    "runs": ("SELECT", "INSERT", "UPDATE"),
    "steps": ("SELECT", "INSERT"),
    "approvals": ("SELECT", "INSERT", "UPDATE"),
    "message_content": ("SELECT", "INSERT"),
}
# Set only by the database. created_at on message_content decides retention, so space roles get
# INSERT and UPDATE on every column except these.
DATABASE_SET_COLUMNS = frozenset({"created_at"})
ENUM_TYPES = (
    "space",
    "task_origin",
    "task_priority",
    "task_status",
    "run_status",
    "step_kind",
    "approval_status",
)

# One statement per item: asyncpg prepares each statement and rejects multi-statement strings.
AUDIT_FUNCTIONS = (
    r"""
CREATE FUNCTION audit_event_hash(
    prev_hash bytea, space space, seq bigint, occurred_at timestamptz, event_type text,
    actor text, task_id uuid, run_id uuid, payload text
) RETURNS bytea
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog
BEGIN ATOMIC
    SELECT sha256(
        prev_hash
        || sha256(convert_to(space::text, 'UTF8'))
        || sha256(convert_to(seq::text, 'UTF8'))
        || sha256(convert_to(
               to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8'))
        || sha256(convert_to(event_type, 'UTF8'))
        || sha256(convert_to(actor, 'UTF8'))
        || sha256(convert_to(coalesce(task_id::text, ''), 'UTF8'))
        || sha256(convert_to(coalesce(run_id::text, ''), 'UTF8'))
        || sha256(convert_to(payload, 'UTF8')));
END
""",
    r"""
-- Runs as the inserting role, so RLS on audit_chain_heads hides the other space's head and a
-- cross-space insert fails before it can take that space's lock.
CREATE FUNCTION audit_events_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM 1 FROM public.audit_chain_heads WHERE space = NEW.space;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no audit chain for space % is visible to %', NEW.space, current_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$
""",
    r"""
-- Runs as af_owner. Locks the space's head row, so concurrent appends are serialized and the
-- chain has no gaps or forks. The writer cannot choose seq, occurred_at, prev_hash or hash.
CREATE FUNCTION audit_events_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    head public.audit_chain_heads%ROWTYPE;
BEGIN
    SELECT * INTO STRICT head FROM public.audit_chain_heads WHERE space = NEW.space FOR UPDATE;
    NEW.seq := head.seq + 1;
    NEW.prev_hash := head.hash;
    NEW.occurred_at := date_trunc('microseconds', clock_timestamp());
    NEW.hash := public.audit_event_hash(
        NEW.prev_hash, NEW.space, NEW.seq, NEW.occurred_at, NEW.event_type, NEW.actor,
        NEW.task_id, NEW.run_id, NEW.payload);
    UPDATE public.audit_chain_heads SET seq = NEW.seq, hash = NEW.hash WHERE space = NEW.space;
    RETURN NEW;
END;
$$
""",
    r"""
CREATE FUNCTION audit_events_immutable() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
""",
    r"""
REVOKE ALL ON FUNCTION audit_events_guard() FROM PUBLIC
""",
    r"""
REVOKE ALL ON FUNCTION audit_events_chain() FROM PUBLIC
""",
    r"""
REVOKE ALL ON FUNCTION audit_events_immutable() FROM PUBLIC
""",
    r"""
-- BEFORE triggers fire in name order: the guard runs first.
CREATE TRIGGER a_guard BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_guard()
""",
    r"""
CREATE TRIGGER b_chain BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_chain()
""",
    r"""
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
""",
    r"""
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_immutable()
""",
    r"""
INSERT INTO audit_chain_heads (space, seq, hash)
VALUES ('personal', 0, decode(repeat('00', 32), 'hex')),
       ('synaption', 0, decode(repeat('00', 32), 'hex'))
""",
)


def _grants(table: str, role: str, columns: list[str]) -> list[str]:
    privileges = SPACE_TABLES[table]
    if DATABASE_SET_COLUMNS.isdisjoint(columns):
        return [f"GRANT {', '.join(privileges)} ON {table} TO {role}"]
    writable = ", ".join(c for c in columns if c not in DATABASE_SET_COLUMNS)
    return [
        f"GRANT {privilege} ({writable}) ON {table} TO {role}"
        if privilege in ("INSERT", "UPDATE")
        else f"GRANT {privilege} ON {table} TO {role}"
        for privilege in privileges
    ]


def _security_statements(columns: dict[str, list[str]]) -> list[str]:
    statements: list[str] = list(AUDIT_FUNCTIONS)
    tables = [*SPACE_TABLES, "audit_events", "audit_chain_heads"]
    for table in tables:
        statements.append(f"REVOKE ALL ON {table} FROM PUBLIC")
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for space, role in SPACE_ROLES.items():
        own = f"space = '{space}'::space"
        for table in SPACE_TABLES:
            statements.append(
                f"CREATE POLICY {table}_{space} ON {table} TO {role}"
                f" USING ({own}) WITH CHECK ({own})"
            )
            statements += _grants(table, role, columns[table])
        statements += [
            f"CREATE POLICY audit_events_{space}_select ON audit_events FOR SELECT TO {role}"
            f" USING ({own})",
            f"CREATE POLICY audit_events_{space}_insert ON audit_events FOR INSERT TO {role}"
            f" WITH CHECK ({own})",
            f"GRANT SELECT ON audit_events TO {role}",
            f"GRANT INSERT (space, event_type, actor, task_id, run_id, payload) ON audit_events"
            f" TO {role}",
            f"CREATE POLICY audit_chain_heads_{space} ON audit_chain_heads FOR SELECT TO {role}"
            f" USING ({own})",
            f"GRANT SELECT ON audit_chain_heads TO {role}",
        ]
    statements += [
        # The chain trigger runs as af_owner and must see and update both heads.
        "CREATE POLICY audit_chain_heads_chain ON audit_chain_heads TO af_owner"
        " USING (true) WITH CHECK (true)",
        # Retention sees only metadata and deletes only bodies past the 30-day window (ADR 0005).
        "CREATE POLICY message_content_retention_select ON message_content FOR SELECT"
        " TO af_retention USING (created_at < now() - interval '30 days')",
        "CREATE POLICY message_content_retention_delete ON message_content FOR DELETE"
        " TO af_retention USING (created_at < now() - interval '30 days')",
        "GRANT SELECT (space, content_hash, created_at) ON message_content TO af_retention",
        "GRANT DELETE ON message_content TO af_retention",
    ]
    return statements


DROP_FUNCTIONS_AND_TYPES = [
    "DROP FUNCTION audit_events_guard()",
    "DROP FUNCTION audit_events_chain()",
    "DROP FUNCTION audit_events_immutable()",
    "DROP FUNCTION audit_event_hash"
    "(bytea, space, bigint, timestamptz, text, text, uuid, uuid, text)",
    *(f"DROP TYPE {name}" for name in ENUM_TYPES),
]


def upgrade() -> None:
    op.create_table(
        "audit_chain_heads",
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("octet_length(hash) = 32", name=op.f("ck_audit_chain_heads_hash_size")),
        sa.CheckConstraint("seq >= 0", name=op.f("ck_audit_chain_heads_seq_non_negative")),
        sa.PrimaryKeyConstraint("space", name=op.f("pk_audit_chain_heads")),
    )
    op.create_table(
        "audit_events",
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(payload::jsonb) = 'object'",
            name=op.f("ck_audit_events_payload_is_object"),
        ),
        sa.CheckConstraint("octet_length(hash) = 32", name=op.f("ck_audit_events_hash_size")),
        sa.CheckConstraint(
            "octet_length(prev_hash) = 32", name=op.f("ck_audit_events_prev_hash_size")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_audit_events_seq_positive")),
        sa.PrimaryKeyConstraint("space", "seq", name=op.f("pk_audit_events")),
    )
    op.create_table(
        "message_content",
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_hash = sha256(content)", name=op.f("ck_message_content_hash_matches_content")
        ),
        sa.PrimaryKeyConstraint("space", "content_hash", name=op.f("pk_message_content")),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column(
            "origin", sa.Enum("api", "schedule", "agent", name="task_origin"), nullable=False
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column(
            "input", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "priority", sa.Enum("low", "normal", "high", name="task_priority"), nullable=False
        ),
        sa.Column(
            "budget", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "waiting_approval",
                "succeeded",
                "failed",
                "escalated",
                "budget_exceeded",
                "cancelled",
                name="task_status",
            ),
            nullable=False,
        ),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(origin = 'agent') = (parent_task_id IS NOT NULL)",
            name=op.f("ck_tasks_parent_iff_agent"),
        ),
        sa.CheckConstraint(
            "agent_id LIKE ('%.' || space::text)", name=op.f("ck_tasks_agent_in_space")
        ),
        sa.CheckConstraint("parent_task_id <> id", name=op.f("ck_tasks_not_own_parent")),
        sa.ForeignKeyConstraint(
            ["space", "parent_task_id"],
            ["tasks.space", "tasks.id"],
            name=op.f("fk_tasks_space_parent_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("space", "id", name=op.f("uq_tasks_space_id")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("running", "done", "escalated", "failed", "budget_exceeded", name="run_status"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name=op.f("ck_runs_finished_iff_done")
        ),
        sa.CheckConstraint(
            "agent_id LIKE ('%.' || space::text)", name=op.f("ck_runs_agent_in_space")
        ),
        sa.ForeignKeyConstraint(
            ["space", "task_id"],
            ["tasks.space", "tasks.id"],
            name=op.f("fk_runs_space_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("space", "id", name=op.f("uq_runs_space_id")),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", "expired", name="approval_status"),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name=op.f("ck_approvals_decided_at_iff_resolved"),
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_by IS NOT NULL)",
            name=op.f("ck_approvals_decided_by_iff_human"),
        ),
        sa.CheckConstraint(
            "expires_at > requested_at", name=op.f("ck_approvals_expires_after_request")
        ),
        sa.ForeignKeyConstraint(
            ["space", "run_id"],
            ["runs.space", "runs.id"],
            name=op.f("fk_approvals_space_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["space", "task_id"],
            ["tasks.space", "tasks.id"],
            name=op.f("fk_approvals_space_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_table(
        "steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space", sa.Enum("personal", "synaption", name="space"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("llm", "tool", name="step_kind"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column(
            "input", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "output", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "CASE kind WHEN 'llm' THEN model IS NOT NULL AND prompt_version IS NOT NULL"
            " ELSE model IS NULL AND prompt_version IS NULL END",
            name=op.f("ck_steps_llm_fields"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0 AND latency_ms >= 0",
            name=op.f("ck_steps_usage_non_negative"),
        ),
        sa.CheckConstraint("step_no >= 0", name=op.f("ck_steps_step_no_non_negative")),
        sa.ForeignKeyConstraint(
            ["space", "run_id"], ["runs.space", "runs.id"], name=op.f("fk_steps_space_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_steps")),
        sa.UniqueConstraint("run_id", "step_no", name=op.f("uq_steps_run_id_step_no")),
    )
    inspector = sa.inspect(op.get_bind())
    columns = {t: [c["name"] for c in inspector.get_columns(t)] for t in SPACE_TABLES}
    for statement in _security_statements(columns):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("steps")
    op.drop_table("approvals")
    op.drop_table("runs")
    op.drop_table("tasks")
    op.drop_table("message_content")
    op.drop_table("audit_events")
    op.drop_table("audit_chain_heads")
    for statement in DROP_FUNCTIONS_AND_TYPES:
        op.execute(statement)
