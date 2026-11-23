# Database

PostgreSQL 16 with pgvector. Schema changes are Alembic migrations in `db/migrations/`, applied by
the one-shot `db-migrate` container (`make migrate` or `make up`). This page describes the M1
schema; the design decisions are in ADR 0002 (roles, RLS) and ADR 0005 (secrets, retention).

## Roles

| Role | Login | Used by | Can do |
| --- | --- | --- | --- |
| `af_owner` | yes | `db-migrate` only | Owns every table and function. Migrations refuse any other role. |
| `af_personal` | yes | gateway, `worker-personal` | Reads and writes `personal` rows only. |
| `af_synaption` | yes | gateway, `worker-synaption` | Reads and writes `synaption` rows only. |
| `af_retention` | no (until M7) | retention job | Reads body metadata and deletes bodies older than 30 days. |

`infra/postgres/roles.sql` creates the roles and resets their passwords from `.env` on every run.
All four are `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB` and members of no other role. `PUBLIC`
loses `CONNECT` on every database and `CREATE` on schema `public`.

Only `postgres`, `db-roles` and the Temporal containers receive the superuser password. Each worker
receives only its own space's password; the gateway receives both, as two separate connection URLs.
Readiness fails if a process is connected as any role other than the one it expects, or as a role
that can bypass RLS.

## Tables

Every table has a `space` column, RLS enabled and forced, and one policy per space role restricted
to that role's own space. References between tables include `space` in the foreign key, so a row
cannot point to a row in another space even when the writer could see both.

| Table | Space roles may | Notes |
| --- | --- | --- |
| `tasks` | select, insert, update | `agent_id` must belong to the row's space. |
| `runs` | select, insert, update | |
| `steps` | select, insert | |
| `approvals` | select, insert, update | |
| `message_content` | select, insert | Email bodies. `content_hash` must equal `sha256(content)`. |
| `audit_events` | select, insert | Append-only for every role, including `af_owner`. |
| `audit_chain_heads` | select | Moved only by the audit trigger. |

`tests/db` discovers tables with a `space` column from the system catalog and fails if one lacks
forced RLS, a policy for either space role, or an isolation seed.

## Audit chain

Each space has its own hash chain. A space role inserts only `space`, `event_type`, `actor`,
`task_id`, `run_id` and `payload`. A `BEFORE INSERT` trigger, running as `af_owner`, locks the
space's row in `audit_chain_heads`, then sets `seq`, `occurred_at`, `prev_hash` and `hash` and moves
the head. Concurrent appends therefore form one gapless sequence. Updates, deletes and truncates
raise an error.

```text
hash = SHA-256(prev_hash || H(space) || H(seq) || H(occurred_at) || H(event_type)
               || H(actor) || H(task_id) || H(run_id) || H(payload))
```

`H(x)` is SHA-256 of the UTF-8 text. `occurred_at` is UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Absent
IDs hash as the empty string. `payload` is canonical JSON (sorted keys, no whitespace). The first
event's `prev_hash` is 32 zero bytes.

`core.audit.chain.verify_chain` recomputes every hash in Python and compares the last event with the
chain head, so it detects edited, removed, reordered and appended-then-truncated events. It cannot
detect a rewrite by someone who also rewrites the head; anchoring heads outside the database (M7)
closes that gap.
