# ADR 0002: Row-level security and space isolation

Status: accepted (2026-11-23)

## Context

SPEC 2.2 requires that nothing in one space can read or act on the other, enforced in Postgres RLS, the policy engine and tests. The common pattern (one app role plus a session variable such as `app.space`) depends on application code setting the variable correctly on every transaction; one bug or a pooled connection that keeps a stale value silently crosses spaces. A second common failure: when the application connects as the table owner, RLS and REVOKEs do not bind it (owners bypass RLS unless it is forced), so they guard only against mistakes.

## Decision

1. **Database roles**
   - `af_owner`: owns all tables; used only by the migration job; never used by gateway or workers.
   - `af_personal`, `af_synaption`: login roles for each space; `NOBYPASSRLS`; own nothing.
   - `af_retention`: may only DELETE from `message_content` past retention (G2 in PLAN).
2. **Policies keyed on the role, not a session variable.** Every space-scoped table has a `space` column (enum), `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`, and per-role policies such as `CREATE POLICY space_personal ON t TO af_personal USING (space = 'personal') WITH CHECK (space = 'personal')`. A connection authenticated as `af_personal` cannot see or write synaption rows regardless of what the code does.
3. **Audit table grants**: space roles get INSERT and SELECT only on `audit_events`; no UPDATE, DELETE or TRUNCATE.
4. **Process isolation**: one worker process per space, configured with only that space's DB credentials, Temporal namespace (ADR 0001), encryption key (ADR 0005) and MCP servers (ADR 0003). The gateway holds one connection pool per space and selects the pool from the authenticated token's space.
5. **Catalog check**: a test queries `pg_class` and `pg_policies` and fails if any table with a `space` column lacks enabled and forced RLS or a policy for each space role. This prevents a new table from silently missing isolation.

## Alternatives considered

- *Single app role with `SET LOCAL app.space`*: one credential for both spaces; isolation depends on every code path setting the variable. Rejected as the primary control.
- *Separate databases per space*: strongest separation, but duplicates migrations and Temporal setup and is not what SPEC 3 describes. Can be adopted later without changing `core/`.

## Consequences

- Cross-space features are impossible by construction, which is intended.
- Two workers run even when one space is idle (memory cost measured in M0).
- Space-less operations (global pause) are implemented as one action per space.

## How it will be verified

The mandatory space isolation test (SPEC 6), in CI, at three levels:
- **DB**: as `af_personal`, select, insert, update and delete synaption rows on every space-scoped table; every attempt returns no rows or fails. Same in reverse.
- **Policy**: a tool call naming a mailbox, file or memory item from the other space is denied.
- **Runtime**: an agent instance of one space delegating to, or being given a task for, the other space is rejected.
Plus the catalog check above.
