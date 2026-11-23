# ADR 0006: IMAP library and iCloud behaviour

Status: accepted (2026-11-23), including the deviation from the SPEC 3 wording "async-capable".

## Context

SPEC 3 asks for "a maintained async-capable IMAP library" for iCloud, with `UIDVALIDITY`/`UID` sync and `CONDSTORE`/`MODSEQ` if supported, and categories mapped to folders under `AF/`. Measured 2026-11-23: iCloud advertises before login only `XAPPLEPUSHSERVICE IMAP4 IMAP4rev1 SASL-IR AUTH=ATOKEN AUTH=PLAIN AUTH=ATOKEN2 AUTH=XOAUTH2`; post-login capabilities are unverified.

Candidates (PyPI, 2026-11-23): `aioimaplib` 2.0.3 (async, released 2026-10-13), `imapclient` 4.3.0 (synchronous, released 2026-11-17), `imap-tools` 1.16.0 (synchronous, 2026-10-14).

## Decision

Use **`imapclient`**, called from synchronous Temporal activities that run in the worker's thread pool. The worker stays asyncio-based; only the IMAP activity blocks a thread.

Why: `imapclient` parses IMAP responses (FETCH, flags, UIDs, folder listings) into Python types; `aioimaplib` returns raw protocol lines, so we would write and maintain our own response parser for a security-sensitive path. Temporal's Python SDK runs synchronous activities in a thread pool executor, so non-async I/O does not block other work. The deviation from "async-capable" was approved on 2026-11-23; the rejected alternative was `aioimaplib` with a parser we own.

## iCloud behaviour that follows from IMAP semantics

1. **Sync** per folder (INBOX and the sent folder, found by `SPECIAL-USE \Sent` if advertised, else by name from config): store `UIDVALIDITY` and the highest seen UID. If `UIDVALIDITY` changes, resync the folder and re-match messages by `Message-ID` header. Without `CONDSTORE`, flag changes are detected by fetching flags for a recent window each sync.
2. **Classification is stored in our DB.** `apply_labels` sets an IMAP keyword only if post-login `PERMANENTFLAGS` allows custom keywords; otherwise it changes nothing on the server.
3. **Archive moves** the message to `AF/<category>`, using `MOVE` if advertised, else `COPY` plus `\Deleted` plus `UID EXPUNGE` (needs `UIDPLUS`).
4. **Never a plain `EXPUNGE`.** Without `UIDPLUS`, `EXPUNGE` permanently removes every message flagged `\Deleted` in the folder, including messages flagged by your other mail clients. That would be a delete, which SPEC 5.1 forbids. If neither `MOVE` nor `UIDPLUS` is available after login, `archive` on iCloud is recorded only and does nothing on the server.
5. Messages are referenced by `Message-ID` plus folder, because a move changes the UID.

## Consequences

- iCloud gets fewer server-side effects than Gmail; the digest and the tracker read our DB, so behaviour is consistent across providers.
- Post-login capabilities decide items 2 to 4; they are measured in M5a and recorded in the M5a PR.

## How it will be verified

- Adapter tests against an in-process fake IMAP server and recorded fixtures, covering `UIDVALIDITY` change, missing `MOVE`, and missing `UIDPLUS`.
- A test asserting the adapter never sends a bare `EXPUNGE`.
