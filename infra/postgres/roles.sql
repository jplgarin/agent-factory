-- Agent Factory database roles (ADR 0002) and extensions. Idempotent: creates missing roles and
-- resets every attribute and password on each run. Run as the bootstrap superuser against
-- agent_factory, with the passwords in the psql variables owner_password, personal_password and
-- synaption_password.
\set ON_ERROR_STOP on

SELECT format('CREATE ROLE %I', name)
FROM unnest(ARRAY['af_owner', 'af_personal', 'af_synaption', 'af_retention']) AS name
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = name)
\gexec

-- Migration-only owner of every Agent Factory table.
ALTER ROLE af_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  INHERIT CONNECTION LIMIT 5 PASSWORD :'owner_password';
-- One login role per space. They own nothing and are subject to row-level security.
ALTER ROLE af_personal WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  NOINHERIT CONNECTION LIMIT 30 PASSWORD :'personal_password';
ALTER ROLE af_synaption WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  NOINHERIT CONNECTION LIMIT 30 PASSWORD :'synaption_password';
-- Session defaults for the space roles (docs/DATABASE.md). They bound how long a stuck or hostile
-- session can hold or wait for locks; a session can still override them with SET.
SELECT format('ALTER ROLE %I SET %s = %L', role, setting, value)
FROM unnest(ARRAY['af_personal', 'af_synaption']) AS role,
     (VALUES ('lock_timeout', '5s'),
             ('statement_timeout', '30s'),
             ('idle_in_transaction_session_timeout', '60s')) AS defaults (setting, value)
\gexec
-- Deletes expired message bodies. No login until the retention job exists (M7).
ALTER ROLE af_retention WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT PASSWORD NULL;

-- No role may act as another: drop any membership between Agent Factory roles.
SELECT format('REVOKE %I FROM %I', r.rolname, m.rolname)
FROM pg_auth_members am
JOIN pg_roles r ON r.oid = am.roleid
JOIN pg_roles m ON m.oid = am.member
WHERE r.rolname IN ('af_owner', 'af_personal', 'af_synaption', 'af_retention')
   OR m.rolname IN ('af_owner', 'af_personal', 'af_synaption', 'af_retention')
\gexec

-- Only roles granted CONNECT below may open this database; nobody may open the others.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', datname)
FROM pg_database WHERE datallowconn
\gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO af_owner', current_database())
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO af_personal, af_synaption, af_retention',
              current_database())
\gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO af_owner;
GRANT USAGE ON SCHEMA public TO af_personal, af_synaption, af_retention;

-- pgvector, for the embeddings in memory_items (ADR 0004). Creating an extension needs a
-- superuser; the migrations run as af_owner and only use it.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
