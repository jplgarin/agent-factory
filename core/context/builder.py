"""Context builder (SPEC 4.3): the text an agent sees at the start of a run.

`build_context` is a pure function. It assembles six blocks in a fixed order, each within its
own token budget, and reports what it included as IDs and hashes for the audit log. The same
request always gives the same blocks and the same hash.

Instructions are never cut: a system prompt, profile, preferences, task brief or tool list
over its budget is an error. Memory is the one elastic block: items are added in rank order
while they fit and the rest are reported as dropped.
"""

import hashlib
import json
from datetime import UTC
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, JsonValue, model_validator

from core.audit.chain import AuditEventDraft, AuditEventType, canonical_json
from core.context.documents import FileRef, Preferences, SpaceProfile
from core.context.tokens import TokenCounter, Utf8TokenEstimator
from core.domain.agent import parse_instance_id
from core.domain.space import Space
from core.domain.tool import ToolDefinition
from core.memory.models import Episode, MemoryMatch, Trust
from core.untrusted import NOTICE, wrap_untrusted


class BlockKind(StrEnum):
    """The blocks of a context, in the order they are assembled."""

    SYSTEM_PROMPT = "system_prompt"
    PROFILE = "profile"
    PREFERENCES = "preferences"
    MEMORY = "memory"
    TASK_BRIEF = "task_brief"
    TOOLS = "tools"


class ContextError(Exception):
    """The request cannot be turned into a context."""


class ContextBudgetError(ContextError):
    """A block that must not be cut does not fit its token budget."""

    def __init__(self, kind: BlockKind, tokens: int, budget: int) -> None:
        super().__init__(f"{kind.value} needs {tokens} tokens; its budget is {budget}")
        self.kind = kind
        self.tokens = tokens
        self.budget = budget


class ContextBudget(BaseModel):
    """Token budget of each block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: int = Field(gt=0)
    profile: int = Field(gt=0)
    preferences: int = Field(gt=0)
    memory: int = Field(ge=0)
    task_brief: int = Field(gt=0)
    tools: int = Field(ge=0)

    def limit(self, kind: BlockKind) -> int:
        return int(getattr(self, kind.value))

    @property
    def total(self) -> int:
        return sum(self.limit(kind) for kind in BlockKind)


class SystemPrompt(BaseModel):
    """An agent's system prompt, as read from its versioned file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    source: FileRef

    @model_validator(mode="after")
    def _text_is_the_file(self) -> Self:
        # The audit log names the file by hash; the model must see exactly that file.
        if _sha256(self.text) != self.source.sha256:
            raise ValueError("system prompt text does not match the hash of its file")
        return self


class ContextRequest(BaseModel):
    """Everything a context is built from. The caller has already loaded all of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: Space
    agent_id: str
    agent_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    system_prompt: SystemPrompt
    profile: SpaceProfile
    profile_source: FileRef
    preferences: Preferences
    preferences_source: FileRef
    memories: tuple[MemoryMatch, ...] = ()
    episodes: tuple[Episode, ...] = ()
    # A model instance, never a dict: a dict would validate into an empty model and vanish.
    task_brief: InstanceOf[BaseModel]
    tools: tuple[ToolDefinition, ...] = ()
    budget: ContextBudget

    @model_validator(mode="after")
    def _check(self) -> Self:
        _, agent_space = parse_instance_id(self.agent_id)
        if agent_space is not self.space:
            raise ValueError("agent_id must name an instance in the request's space")
        if not type(self.task_brief).model_fields:
            raise ValueError("task brief has no fields")
        if any(match.item.space is not self.space for match in self.memories):
            raise ValueError("memory item from another space")
        if any(episode.space is not self.space for episode in self.episodes):
            raise ValueError("episode from another space")
        for what, ids in (
            ("memory item", [str(match.item.id) for match in self.memories]),
            ("episode", [str(episode.id) for episode in self.episodes]),
            ("tool", [tool.name for tool in self.tools]),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {what}")
        return self


class ContextBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BlockKind
    text: str
    tokens: int = Field(ge=0)
    sha256: str


class ToolSpec(BaseModel):
    """A tool as it is presented to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    input_schema: dict[str, JsonValue]


class IncludedText(BaseModel):
    """A memory item or episode that went into the context: its ID and the hash of its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    sha256: str
    trust: Trust


class BuiltContext(BaseModel):
    """The assembled context and the record of what went into it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: Space
    agent_id: str
    agent_version: str
    blocks: tuple[ContextBlock, ...]
    tools: tuple[ToolSpec, ...]
    tokens: int
    token_counter: str
    sha256: str
    # True when untrusted text is included: the run that reads this context is tainted (G7).
    contains_untrusted: bool
    sources: dict[str, FileRef]
    memories: tuple[IncludedText, ...]
    episodes: tuple[IncludedText, ...]
    dropped_memories: tuple[UUID, ...]
    dropped_episodes: tuple[UUID, ...]

    def block(self, kind: BlockKind) -> ContextBlock:
        return next(block for block in self.blocks if block.kind is kind)

    def audit_payload(self) -> dict[str, JsonValue]:
        """What was included, as IDs and hashes only (SPEC 4.7). No content."""
        return {
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "context_sha256": self.sha256,
            "tokens": self.tokens,
            "token_counter": self.token_counter,
            "contains_untrusted": self.contains_untrusted,
            "blocks": [
                {"kind": block.kind.value, "sha256": block.sha256, "tokens": block.tokens}
                for block in self.blocks
            ],
            "sources": {
                name: {"path": ref.path, "sha256": ref.sha256}
                for name, ref in sorted(self.sources.items())
            },
            "memories": [_included(entry) for entry in self.memories],
            "episodes": [_included(entry) for entry in self.episodes],
            "dropped_memories": [str(item_id) for item_id in self.dropped_memories],
            "dropped_episodes": [str(episode_id) for episode_id in self.dropped_episodes],
            "tools": [
                {"name": tool.name, "sha256": _sha256(canonical_json(tool.input_schema))}
                for tool in self.tools
            ],
        }

    def audit_draft(self, *, task_id: UUID, run_id: UUID) -> AuditEventDraft:
        return AuditEventDraft(
            space=self.space,
            event_type=AuditEventType.CONTEXT_ASSEMBLED,
            actor=self.agent_id,
            task_id=task_id,
            run_id=run_id,
            payload=self.audit_payload(),
        )


def _included(entry: IncludedText) -> dict[str, JsonValue]:
    return {"id": str(entry.id), "sha256": entry.sha256, "trust": entry.trust.value}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _pretty_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)


def _render_profile(profile: SpaceProfile) -> str:
    lines = ["## Space profile", f"Name: {profile.name}"]
    if profile.role is not None:
        lines.append(f"Role: {profile.role}")
    lines.append(f"Languages: {', '.join(profile.languages)}")
    lines.append(f"Timezone: {profile.timezone}")
    if profile.facts:
        lines.append("Facts:")
        lines.extend(f"- {fact}" for fact in profile.facts)
    return "\n".join(lines)


def _render_preferences(preferences: Preferences) -> str:
    lines = ["## Preferences"]
    lines.extend(f"{key}: {value}" for key, value in sorted(preferences.style.items()))
    if preferences.rules:
        lines.append("Rules:")
        lines.extend(f"- {rule}" for rule in preferences.rules)
    return "\n".join(lines)


def _render_brief(brief: BaseModel) -> str:
    return "## Task\n" + _pretty_json(brief.model_dump(mode="json"))


def _tool_specs(tools: tuple[ToolDefinition, ...]) -> tuple[ToolSpec, ...]:
    return tuple(
        ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_model.model_json_schema(),
        )
        for tool in sorted(tools, key=lambda tool: tool.name)
    )


def _render_tools(specs: tuple[ToolSpec, ...]) -> str:
    return canonical_json({"tools": [spec.model_dump(mode="json") for spec in specs]})


def _memory_line(match: MemoryMatch) -> str:
    item = match.item
    if item.trust is Trust.UNTRUSTED:
        return wrap_untrusted(item.content, source=f"{item.source.kind.value}:{item.source.ref}")
    return f"- ({item.kind.value}, confidence {item.confidence:.2f}) {item.content}"


def _episode_line(episode: Episode) -> str:
    day = episode.created_at.astimezone(UTC).strftime("%Y-%m-%d")
    text = f"{day}, {episode.outcome.value}, {episode.agent_id}: {episode.summary}"
    if episode.trust is Trust.UNTRUSTED:
        return wrap_untrusted(text, source=f"episode:{episode.run_id}")
    return f"- {text}"


def _render_memory(memories: list[MemoryMatch], episodes: list[Episode]) -> str:
    if not memories and not episodes:
        return ""
    lines = ["## Memory"]
    untrusted = [m.item.trust for m in memories] + [e.trust for e in episodes]
    if Trust.UNTRUSTED in untrusted:
        lines.append(NOTICE)
    if memories:
        lines.append("Known facts and notes:")
        lines.extend(_memory_line(match) for match in memories)
    if episodes:
        lines.append("Earlier related tasks:")
        lines.extend(_episode_line(episode) for episode in episodes)
    return "\n".join(lines)


def _fit_memory(
    request: ContextRequest, counter: TokenCounter
) -> tuple[list[MemoryMatch], list[Episode]]:
    """Take memories (closest first) and episodes (newest first) in turn while the block fits.
    Each list stops at its first entry that does not fit, so a kept entry never ranks below a
    dropped one of its kind."""
    ranked = sorted(request.memories, key=lambda match: (match.distance, str(match.item.id)))
    by_id = sorted(request.episodes, key=lambda episode: str(episode.id))
    recent = sorted(by_id, key=lambda episode: episode.created_at, reverse=True)
    budget = request.budget.memory
    kept_memories: list[MemoryMatch] = []
    kept_episodes: list[Episode] = []
    memories_open, episodes_open = True, True
    while (memories_open and len(kept_memories) < len(ranked)) or (
        episodes_open and len(kept_episodes) < len(recent)
    ):
        if memories_open and len(kept_memories) < len(ranked):
            candidate = [*kept_memories, ranked[len(kept_memories)]]
            if counter.count(_render_memory(candidate, kept_episodes)) <= budget:
                kept_memories = candidate
            else:
                memories_open = False
        if episodes_open and len(kept_episodes) < len(recent):
            more = [*kept_episodes, recent[len(kept_episodes)]]
            if counter.count(_render_memory(kept_memories, more)) <= budget:
                kept_episodes = more
            else:
                episodes_open = False
    return kept_memories, kept_episodes


def build_context(request: ContextRequest, counter: TokenCounter | None = None) -> BuiltContext:
    counter = counter or Utf8TokenEstimator()
    kept_memories, kept_episodes = _fit_memory(request, counter)
    specs = _tool_specs(request.tools)
    texts = {
        BlockKind.SYSTEM_PROMPT: request.system_prompt.text,
        BlockKind.PROFILE: _render_profile(request.profile),
        BlockKind.PREFERENCES: _render_preferences(request.preferences),
        BlockKind.MEMORY: _render_memory(kept_memories, kept_episodes),
        BlockKind.TASK_BRIEF: _render_brief(request.task_brief),
        BlockKind.TOOLS: _render_tools(specs) if specs else "",
    }
    blocks: list[ContextBlock] = []
    for kind in BlockKind:
        tokens = counter.count(texts[kind])
        if tokens > request.budget.limit(kind):
            raise ContextBudgetError(kind, tokens, request.budget.limit(kind))
        blocks.append(
            ContextBlock(kind=kind, text=texts[kind], tokens=tokens, sha256=_sha256(texts[kind]))
        )
    kept_memory_ids = {match.item.id for match in kept_memories}
    kept_episode_ids = {episode.id for episode in kept_episodes}
    memories = tuple(
        IncludedText(id=m.item.id, sha256=m.item.content_sha256(), trust=m.item.trust)
        for m in kept_memories
    )
    episodes = tuple(
        IncludedText(id=e.id, sha256=e.summary_sha256(), trust=e.trust) for e in kept_episodes
    )
    return BuiltContext(
        space=request.space,
        agent_id=request.agent_id,
        agent_version=request.agent_version,
        blocks=tuple(blocks),
        tools=specs,
        tokens=sum(block.tokens for block in blocks),
        token_counter=counter.name,
        sha256=_sha256(
            canonical_json({"blocks": [[block.kind.value, block.sha256] for block in blocks]})
        ),
        contains_untrusted=any(entry.trust is Trust.UNTRUSTED for entry in (*memories, *episodes)),
        sources={
            BlockKind.SYSTEM_PROMPT.value: request.system_prompt.source,
            BlockKind.PROFILE.value: request.profile_source,
            BlockKind.PREFERENCES.value: request.preferences_source,
        },
        memories=memories,
        episodes=episodes,
        dropped_memories=tuple(
            sorted(m.item.id for m in request.memories if m.item.id not in kept_memory_ids)
        ),
        dropped_episodes=tuple(
            sorted(e.id for e in request.episodes if e.id not in kept_episode_ids)
        ),
    )
