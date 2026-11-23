from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from core.domain.agent import AgentSpec, instance_id, parse_instance_id
from core.domain.approval import APPROVAL_TTL, Approval, ApprovalError, ApprovalStatus
from core.domain.budget import Budget
from core.domain.policy import Decision, PolicyDecision
from core.domain.space import Space
from core.domain.step import Step, StepKind
from core.domain.task import Run, RunStatus, Task, TaskOrigin, TaskStatus
from core.domain.tool import ToolDefinition, ToolRisk

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
BUDGET = Budget(max_steps=8, max_tokens=50_000, max_cost_usd=Decimal("0.5"), timeout_s=300)


def _spec(**overrides: Any) -> AgentSpec:
    data: dict[str, Any] = {
        "id": "email",
        "spaces": [Space.PERSONAL, Space.SYNAPTION],
        "version": "1.0.0",
        "model": "claude-sonnet-5-5",
        "system_prompt_path": "prompts/email.md",
        "output_schema": "EmailTriageResult",
        "budgets": BUDGET,
    }
    return AgentSpec.model_validate(data | overrides)


def _task(**overrides: Any) -> Task:
    data: dict[str, Any] = {
        "space": Space.PERSONAL,
        "origin": TaskOrigin.API,
        "requested_by": "owner",
        "agent_id": "email.personal",
        "input": {"query": "triage"},
        "budget": BUDGET,
        "created_at": NOW,
    }
    return Task.model_validate(data | overrides)


def test_agent_instance_ids_are_per_space() -> None:
    spec = _spec()
    assert spec.instance_id(Space.SYNAPTION) == "email.synaption"
    assert parse_instance_id(instance_id("email", Space.PERSONAL)) == ("email", Space.PERSONAL)
    with pytest.raises(ValueError, match="does not run"):
        _spec(spaces=[Space.PERSONAL]).instance_id(Space.SYNAPTION)
    with pytest.raises(ValueError, match="not an agent instance"):
        parse_instance_id("email")
    with pytest.raises(ValueError, match="other"):
        parse_instance_id("email.other")


@pytest.mark.parametrize(
    "overrides",
    [
        {"spaces": []},
        {"spaces": [Space.PERSONAL, Space.PERSONAL]},
        {"system_prompt_path": "/etc/passwd"},
        {"system_prompt_path": "prompts/../../secret.md"},
        {"can_delegate_to": ["email"]},
        {"max_delegation_depth": 6},
        {"version": "v1"},
        {"id": "Email"},
        {"unknown": 1},
    ],
)
def test_agent_spec_rejects_invalid_definitions(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _spec(**overrides)


def test_task_defaults_and_invariants() -> None:
    task = _task()
    assert task.status is TaskStatus.PENDING
    assert not task.status.is_terminal
    assert TaskStatus.SUCCEEDED.is_terminal
    with pytest.raises(ValidationError, match="instance in the task's space"):
        _task(agent_id="email.synaption")
    with pytest.raises(ValidationError, match="delegated"):
        _task(origin=TaskOrigin.AGENT)
    with pytest.raises(ValidationError, match="delegated"):
        _task(parent_task_id=uuid4())
    task_id = uuid4()
    with pytest.raises(ValidationError, match="own parent"):
        _task(id=task_id, origin=TaskOrigin.AGENT, parent_task_id=task_id)
    with pytest.raises(ValidationError):
        _task(created_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        task.status = TaskStatus.RUNNING  # pyright: ignore[reportAttributeAccessIssue]


def test_run_lifecycle() -> None:
    run = Run(space=Space.PERSONAL, task_id=uuid4(), agent_id="email.personal", started_at=NOW)
    done = run.finish(RunStatus.DONE, NOW + timedelta(seconds=3))
    assert done.status is RunStatus.DONE
    assert done.id == run.id
    with pytest.raises(ValueError, match="already finished"):
        done.finish(RunStatus.FAILED, NOW)
    with pytest.raises(ValueError, match="final status"):
        run.finish(RunStatus.RUNNING, NOW)
    with pytest.raises(ValidationError, match="precedes"):
        run.finish(RunStatus.DONE, NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="space"):
        Run(space=Space.SYNAPTION, task_id=uuid4(), agent_id="email.personal", started_at=NOW)


def test_step_records_model_only_for_llm_calls() -> None:
    common: dict[str, Any] = {
        "space": Space.PERSONAL,
        "run_id": uuid4(),
        "step_no": 0,
        "input": {},
        "created_at": NOW,
    }
    llm = Step(kind=StepKind.LLM, name="plan", model="m", prompt_version="1", **common)
    assert llm.cost_usd == 0
    Step(kind=StepKind.TOOL, name="mail.search", **common)
    with pytest.raises(ValidationError, match="records its model"):
        Step(kind=StepKind.LLM, name="plan", **common)
    with pytest.raises(ValidationError, match="no model"):
        Step(kind=StepKind.TOOL, name="mail.search", model="m", **common)
    with pytest.raises(ValidationError):
        Step.model_validate(common | {"kind": "tool", "name": "mail.search", "step_no": -1})


class _SearchInput(BaseModel):
    query: str


def test_tool_definition_validates_name_and_limits() -> None:
    tool = ToolDefinition(
        name="mail.search",
        description="Search a mailbox.",
        input_model=_SearchInput,
        risk=ToolRisk.READ,
        timeout_s=10,
        max_output_chars=8000,
        idempotent=True,
    )
    assert tool.input_model.model_validate({"query": "x"})
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate(tool.model_dump() | {"name": "search"})
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate(tool.model_dump() | {"timeout_s": 0})


def test_policy_decision_names_its_rule() -> None:
    decision = PolicyDecision(decision=Decision.DENY, rule="no_send", reason="v1 never sends")
    assert decision.decision is Decision.DENY
    with pytest.raises(ValidationError):
        PolicyDecision(decision=Decision.ALLOW, rule="", reason="x")


def _approval() -> Approval:
    return Approval.request(
        space=Space.PERSONAL, task_id=uuid4(), run_id=uuid4(), payload={"action": "x"}, at=NOW
    )


def test_approval_lifecycle() -> None:
    pending = _approval()
    assert pending.expires_at == NOW + APPROVAL_TTL
    approved = pending.decide(approved=True, by="owner", at=NOW + timedelta(hours=1))
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_by == "owner"
    rejected = pending.decide(approved=False, by="owner", at=NOW)
    assert rejected.status is ApprovalStatus.REJECTED
    with pytest.raises(ApprovalError, match="already approved"):
        approved.decide(approved=False, by="owner", at=NOW)
    with pytest.raises(ApprovalError, match="expired"):
        pending.decide(approved=True, by="owner", at=NOW + APPROVAL_TTL)
    with pytest.raises(ApprovalError, match="not expired"):
        pending.expire(NOW)
    expired = pending.expire(NOW + APPROVAL_TTL)
    assert expired.status is ApprovalStatus.EXPIRED
    assert expired.decided_by is None
    with pytest.raises(ApprovalError, match="already expired"):
        expired.expire(NOW + APPROVAL_TTL)


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_at": NOW},
        {"status": ApprovalStatus.APPROVED},
        {"status": ApprovalStatus.APPROVED, "decided_at": NOW},
        {"status": ApprovalStatus.EXPIRED, "decided_at": NOW, "decided_by": "owner"},
        {"decided_at": NOW},
        {"status": ApprovalStatus.REJECTED, "decided_at": NOW - timedelta(1), "decided_by": "o"},
    ],
)
def test_approval_rejects_inconsistent_states(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Approval.model_validate(_approval().model_dump() | overrides)


def test_budget_limits_are_positive() -> None:
    with pytest.raises(ValidationError):
        Budget(max_steps=0, max_tokens=1, max_cost_usd=Decimal(1), timeout_s=1)
