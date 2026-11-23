# ADR 0005: Secret encryption

Status: accepted (2026-11-23)

## Context

SPEC 2.9 forbids secrets in plain text in code, logs, prompts or the database. SPEC 3 asks for OAuth refresh tokens encrypted at rest with per-space keys (envelope encryption, key from the secret manager). SPEC 5.3 keeps the iCloud app-specific password in the secret manager only. SPEC 5.1 stores generated files encrypted in space-scoped storage.

## Decision

1. **Key encryption keys (KEK)**: one 256-bit key per space, `AF_KEK_PERSONAL` and `AF_KEK_SYNAPTION`, held in Doppler in deployed environments and in a git-ignored `.env` locally. Each space's worker receives only its own KEK (ADR 0002); the gateway receives neither. The OAuth code exchange runs inside the space's worker, which encrypts and stores the refresh token, so the token never passes through the gateway (ADR 0007).
2. **Data keys (DEK)**: a fresh random 256-bit key per secret record and per file.
3. **Algorithm**: AES-256-GCM via the `cryptography` library (`AESGCM`), 96-bit random nonces. The DEK is wrapped with the KEK, also with AES-256-GCM.
4. **Associated data** binds each ciphertext to its context: `space | table | record_id | key_version`. A ciphertext copied to another row or space fails to decrypt.
5. **Rotation**: KEKs carry a version. A new version is added alongside the old; a re-wrap job rewraps DEKs (data is not re-encrypted); the old version is removed after the job reports zero rows on it. Procedure goes into `docs/RUNBOOK.md`.
6. **iCloud app-specific password** is never stored in the database; it is read from the secret manager by the personal mail MCP server only. Revocation and rotation steps (appleid.apple.com) go into the runbook.
7. **Logging**: structlog processors drop known secret fields and redact values matching token patterns; a test asserts that a log line containing a fake token is redacted.

## Alternatives considered

- *pgcrypto in the database*: the key would pass through SQL and could appear in logs or `pg_stat_statements`. Rejected.
- *A cloud KMS*: stronger key custody, but adds a cloud dependency the spec does not include. The KEK source sits behind a small interface so a KMS can replace Doppler later.

## Consequences

- Known limit, stated in SECURITY.md: on a single VPS, root on the host can read both workers' environments, so per-space keys protect against application-level mistakes and database or backup leaks, not against host compromise.
- Losing a KEK makes that space's tokens unrecoverable; recovery is re-authorising the mailboxes, which the runbook covers.

## How it will be verified

- Unit tests: round trip; tampered ciphertext, wrong AAD, wrong space key and wrong version all fail.
- Rotation test: rewrap then decrypt with the new version only.
- Test that no column in the schema stores a token without the encrypted envelope (catalog check on known secret columns).
