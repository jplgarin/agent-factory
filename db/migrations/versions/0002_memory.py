"""Memory: semantic items with embeddings and one episode per run (SPEC 4.4, ADR 0004).

Same isolation as 0001 (ADR 0002): forced row-level security, one policy per space role, no
access for PUBLIC. Space roles may only read and insert, and never set created_at. The pgvector
extension is created by infra/postgres/roles.sql, because that needs a superuser.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 384
SPACE_ROLES = {"personal": "af_personal", "synaption": "af_synaption"}
MEMORY_TABLES = ("memory_items", "memory_episodes")
# Set only by the database, as in 0001: space roles insert every column except these.
DATABASE_SET_COLUMNS = frozenset({"created_at"})
ENUM_TYPES = ("memory_kind", "memory_source_kind", "memory_trust")


def _space() -> sa.Enum:
    return sa.Enum("personal", "synaption", name="space", create_type=False)


def _trust() -> sa.Enum:
    return sa.Enum("trusted", "untrusted", name="memory_trust")


def _security_statements(columns: dict[str, list[str]]) -> list[str]:
    statements: list[str] = []
    for table in MEMORY_TABLES:
        statements += [
            f"REVOKE ALL ON {table} FROM PUBLIC",
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        ]
        insertable = ", ".join(c for c in columns[table] if c not in DATABASE_SET_COLUMNS)
        for space, role in SPACE_ROLES.items():
            own = f"space = '{space}'::space"
            statements += [
                f"CREATE POLICY {table}_{space} ON {table} TO {role}"
                f" USING ({own}) WITH CHECK ({own})",
                f"GRANT SELECT ON {table} TO {role}",
                f"GRANT INSERT ({insertable}) ON {table} TO {role}",
            ]
    return statements


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space", _space(), nullable=False),
        sa.Column("kind", sa.Enum("fact", "note", name="memory_kind"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "source_kind",
            sa.Enum("owner", "agent", "email", "web", "document", name="memory_source_kind"),
            nullable=False,
        ),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("created_by_agent", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("trust", _trust(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "created_by_agent LIKE ('%.' || space::text)",
            name=op.f("ck_memory_items_agent_in_space"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_memory_items_confidence_range")
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 2000", name=op.f("ck_memory_items_content_length")
        ),
        sa.CheckConstraint(
            "embedding_dim = vector_dims(embedding)",
            name=op.f("ck_memory_items_dim_matches_embedding"),
        ),
        sa.CheckConstraint(
            "vector_norm(embedding) > 0", name=op.f("ck_memory_items_embedding_not_zero")
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name=op.f("ck_memory_items_expires_after_creation"),
        ),
        sa.CheckConstraint(
            "source_kind NOT IN ('email', 'web', 'document') OR trust = 'untrusted'",
            name=op.f("ck_memory_items_external_is_untrusted"),
        ),
        sa.CheckConstraint(
            "content_hash = sha256(convert_to(content, 'UTF8'))",
            name=op.f("ck_memory_items_hash_matches_content"),
        ),
        sa.CheckConstraint(
            "char_length(source_ref) BETWEEN 1 AND 500",
            name=op.f("ck_memory_items_source_ref_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(
        op.f("ix_memory_items_space_content_hash"),
        "memory_items",
        ["space", "content_hash"],
        unique=False,
    )
    op.create_table(
        "memory_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space", _space(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("trust", _trust(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 2000",
            name=op.f("ck_memory_episodes_summary_length"),
        ),
        sa.CheckConstraint(
            "topic ~ '^[a-z0-9][a-z0-9_.:/@-]{0,199}$'",
            name=op.f("ck_memory_episodes_topic_format"),
        ),
        sa.ForeignKeyConstraint(
            ["space", "run_id"],
            ["runs.space", "runs.id"],
            name=op.f("fk_memory_episodes_space_run_id_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_episodes")),
        sa.UniqueConstraint("space", "run_id", name=op.f("uq_memory_episodes_space_run_id")),
    )
    op.create_index(
        op.f("ix_memory_episodes_space_topic_created_at"),
        "memory_episodes",
        ["space", "topic", "created_at"],
        unique=False,
    )
    inspector = sa.inspect(op.get_bind())
    columns = {t: [c["name"] for c in inspector.get_columns(t)] for t in MEMORY_TABLES}
    for statement in _security_statements(columns):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("memory_episodes")
    op.drop_table("memory_items")
    for name in ENUM_TYPES:
        op.execute(f"DROP TYPE {name}")
