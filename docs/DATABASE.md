# Database

PostgreSQL 16 with pgvector. Schema changes are Alembic migrations in `db/migrations/`, applied by
the one-shot `db-migrate` container (`make migrate` or `make up`). This page describes the schema
after M3; the design decisions are in ADR 0002 (roles, RLS), ADR 0004 (embeddings) and ADR 0005
(secrets, retention). How memory is written and searched is in [MEMORY.md](MEMORY.md).

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

The same script creates the `vector` extension (pgvector), because creating an extension needs a
superuser and migrations run as `af_owner`. It runs before every migration, in the stack and in
each test database.

`af_personal` and `af_synaption` get session defaults, so a stuck or hostile session cannot hold
or wait on locks indefinitely:

| Setting | Value | Why |
| --- | --- | --- |
| `lock_timeout` | 5 s | A query waiting on a lock fails instead of stalling the worker. Any table lock held by the other space's role (RLS does not cover table locks) stalls this space for at most 5 s per query. |
| `statement_timeout` | 30 s | M1 queries are single-row reads and writes; anything slower is a bug. |
| `idle_in_transaction_session_timeout` | 60 s | Ends a session left inside an open transaction, releasing its locks. |

These are defaults, not limits: a session can change them with `SET`, so a compromised worker can
still hold a table lock for longer. Removing the table-level `UPDATE` grants that allow it (writes
through `SECURITY DEFINER` functions instead) is decided in M4.

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
| `memory_items` | select, insert | Facts and notes with a 384-dimension embedding. See below. |
| `memory_episodes` | select, insert | One summary per run. The run must be in the same space. |
| `audit_events` | select, insert | Append-only for every role, including `af_owner`. |
| `audit_chain_heads` | select | Moved only by the audit trigger. |

`created_at` is set only by the database: on tables that have it, space roles get `INSERT` and
`UPDATE` on every other column. This matters most on `message_content`, where `created_at` decides
when retention deletes the body. Every `SECURITY DEFINER` function sets
`search_path = pg_catalog, public, pg_temp`.

`tests/db` discovers tables with a `space` column from the system catalog and fails if one lacks
forced RLS, a policy for either space role, or an isolation seed.

## Memory tables

`memory_items` and `memory_episodes` are insert-only for the space roles: stored memory cannot be
changed or deleted by a worker. The checks below repeat the rules of `core.memory`, so they hold
even if a writer skips that code.

| Check on `memory_items` | Why |
| --- | --- |
| `source_kind` of `email`, `web` or `document` requires `trust = 'untrusted'` | External text is never stored as trusted (SPEC 2.5). |
| `content_hash = sha256(content)` | The hash used to find duplicates is the hash of the text. |
| `created_by_agent` ends with the row's space | An item is written by an agent of its own space. |
| `confidence` between 0 and 1; content of 1 to 2000 characters | |
| `embedding_dim = vector_dims(embedding)`; the embedding is not the zero vector | Every stored vector can be compared (ADR 0004). |
| `expires_at` is after `created_at` | |

The `embedding` column is `vector(384)`, the dimension of the configured model, and each row stores
`embedding_model`. Searches compare only rows of one model. A model with another dimension needs a
new column and a re-embed (ADR 0004).

`memory_episodes` references `runs` by `(space, run_id)` and allows one episode per run. Its topic
is a lowercase key of at most 200 characters.

Neither table has an index on the embedding: search is exact (measurements in
[MEMORY.md](MEMORY.md#search)).

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
