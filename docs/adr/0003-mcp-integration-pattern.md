# ADR 0003: MCP integration pattern

Status: accepted (2026-11-23)

## Context

SPEC 3 requires every integration to be an MCP server consumed through an MCP client layer. SPEC 2.3 and 4.6 require that the runtime rejects undeclared tools and that every call passes the policy engine. MCP servers can describe their own tools, but a server's self-description must not decide risk or permissions.

## Decision

1. **Tool metadata is ours.** Each tool's `ToolDefinition` (input model, `risk`, timeout, max output chars, idempotent flag) is defined in the repository next to the integration and loaded into the tool registry. At startup the MCP client lists the server's tools and fails if a server exposes a tool that is missing from the registry or whose input schema differs. Annotations from the server are ignored for policy.
2. **Call path**: agent proposes a call, runtime validates it against the input model, policy engine decides (declared by the agent, same space, argument limits such as mailbox id and label prefix, risk, taint), only then the activity calls the MCP server. Outputs are truncated to the tool limit and wrapped as untrusted when the tool returns external content.
3. **Per-space server processes over stdio.** Each space's worker starts its own MCP server processes (mail, documents, tracker) as long-lived stdio subprocesses with only that space's credentials in their environment. No MCP server is reachable over the network, so no MCP transport authentication is needed.
4. **Shared services are shared code, not shared processes.** The documents server is one codebase, instantiated once per space, writing to that space's storage.
5. **Disabled tools are not registered.** `mail.send_draft` exists in code but is registered only when its feature flag is on; even then it is `irreversible`, so the policy engine always returns `require_approval`.

## Alternatives considered

- *Streamable HTTP MCP servers as separate containers*: better for scaling across hosts, but adds network endpoints, transport auth, and per-space secrets in more containers. Can replace stdio later behind the same client interface.
- *Trusting server-declared annotations for risk*: lets a buggy or compromised server downgrade its own risk. Rejected.

## Consequences

- A worker restart restarts its MCP servers; in-flight tool activities retry under ADR 0001's idempotency rules.
- Adding an agent that uses existing tools needs no code; adding a tool needs a `ToolDefinition` plus server code, which SPEC 4.5 allows.

## How it will be verified

- Registry tests: mismatched schema or unregistered tool fails startup.
- Policy tests for each rejection reason.
- Injection tests (SPEC 5.3): an email instructing the agent to call an undeclared or disabled tool results in a policy denial recorded in the audit log, and no MCP call.

Unverified, to check in M2: the exact client API of `mcp` 2.x for long-lived stdio sessions.
