# ADR 0001: Temporal usage pattern

Status: accepted (2026-11-23)

## Context

SPEC 2.7 requires that a restart mid-task loses and duplicates nothing. SPEC 3 and 4.2 make Temporal the orchestrator, with every LLM call and tool call as an activity. SPEC 9.5 keeps `core/` free of framework imports, and Temporal workflow code must be deterministic.

## Decision

1. **Core is a pure state machine.** `core/runtime` exposes a step function: given the run state and the last observation, it returns the next command (`CallLLM`, `CallTool`, `Delegate`, `RequestApproval`, `Finish`, `Escalate`, `Fail`). It does no I/O and imports nothing from Temporal. It is unit-tested without Temporal.
2. **Workflows drive the state machine.** `apps/worker` contains one generic `AgentRunWorkflow`. It loops: call the step function, turn the command into an activity, child workflow or signal wait, feed the result back. Budgets are checked before every command using recorded usage and `workflow.now()`, never the host clock.
3. **Activities do all I/O**: LLM calls, policy-checked tool calls, DB writes, audit appends. Each has a start-to-close timeout, exponential-backoff retries, and non-retryable error types for policy denials and validation errors.
4. **Idempotency.** Each side-effecting activity receives a key `(workflow_id, step_no, tool)`. A `tool_effects` table records intent before the provider call and the result after. On retry, a recorded result is returned without calling the provider again. Operations that are not naturally idempotent carry the key to the provider where possible (for example, an `X-AF-Idempotency-Key` header on drafts, searched for before creating).
5. **Small history.** Email bodies and large tool outputs are stored in Postgres (`message_content`, keyed by hash) and passed through workflow history as references only.
6. **Delegation** is a child workflow in the same namespace, started with a budget no greater than the parent's remaining budget and the child agent's own permissions. Independent briefs run in parallel and are awaited together.
7. **Approvals** are a signal on the waiting workflow, with a 24 hour timer; expiry resolves the approval as `expired` and the action does not run.
8. **Schedules**: per-mailbox sync every 5 minutes with overlap policy "skip" and a fixed workflow id per mailbox, so two syncs of one mailbox never run at once; daily digest at 07:00 Europe/London; daily retention job.
9. **One Temporal namespace and task queue per space**, polled by that space's worker only (see ADR 0002). A workflow in one space cannot start a child in the other because the other namespace's worker does not exist in its process.
10. **Code changes to workflows** use Temporal's patching API so in-flight runs replay safely.

## Alternatives considered

- *Agent loop entirely inside one activity*: simple, but a crash repeats every LLM and tool call in the loop, which breaks SPEC 2.7.
- *Temporal imports in `core/`*: violates SPEC 9.5 and makes the runtime untestable without Temporal.

## Consequences

- Every new tool must declare whether it is idempotent (SPEC 4.1 `ToolDefinition.idempotent`); non-idempotent tools need a dedupe mechanism before they can be registered.
- Workflow determinism is enforced by Temporal's sandbox and replay tests in CI.

## How it will be verified

- Unit tests of the state machine (no Temporal).
- Durability test (SPEC 6): kill the worker after the provider call returns but before the activity completes; restart; assert the run finishes once and the side effect exists once.
- Budget tests: each budget type produces a clean `BudgetExceeded` result.
- Replay test of recorded histories against the current workflow code.

Unverified, to read in M4: current Temporal payload and history size limits.
