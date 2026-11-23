"""Memory models (SPEC 4.4). Every item belongs to one space and records where it came from."""

import hashlib
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from core.domain.agent import parse_instance_id
from core.domain.space import Space
from core.domain.task import RunStatus

MAX_MEMORY_CHARS = 2000
MAX_SOURCE_REF_CHARS = 500
TOPIC_PATTERN = r"^[a-z0-9][a-z0-9_.:/@-]{0,199}$"


class Trust(StrEnum):
    """Whether text may be shown to an agent as plain context or only as fenced data."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class MemoryKind(StrEnum):
    FACT = "fact"
    NOTE = "note"


class SourceKind(StrEnum):
    OWNER = "owner"  # stated by the owner
    AGENT = "agent"  # concluded by an agent from trusted input
    EMAIL = "email"
    WEB = "web"
    DOCUMENT = "document"

    @property
    def is_external(self) -> bool:
        """External content is written by other people and is never trusted (SPEC 2.5)."""
        return self in _EXTERNAL_SOURCES


_EXTERNAL_SOURCES = frozenset({SourceKind.EMAIL, SourceKind.WEB, SourceKind.DOCUMENT})


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _storable(text: str, what: str) -> None:
    if not text.strip():
        raise ValueError(f"{what} must not be blank")
    if "\x00" in text:
        raise ValueError(f"{what} must not contain NUL characters")


class MemorySource(BaseModel):
    """Where a memory item came from: the kind of source and a reference to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SourceKind
    ref: str = Field(min_length=1, max_length=MAX_SOURCE_REF_CHARS)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _storable(self.ref, "source ref")
        return self


class MemoryDraft(BaseModel):
    """A fact or note to remember. `MemoryWriter` decides whether it may be stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    space: Space
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=MAX_MEMORY_CHARS)
    source: MemorySource
    created_by_agent: str
    confidence: float = Field(ge=0, le=1)
    trust: Trust
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _storable(self.content, "content")
        _, agent_space = parse_instance_id(self.created_by_agent)
        if agent_space is not self.space:
            raise ValueError("created_by_agent must name an instance in the item's space")
        if self.source.kind.is_external and self.trust is not Trust.UNTRUSTED:
            raise ValueError("content from an external source must be marked untrusted")
        return self


class MemoryItem(BaseModel):
    """A stored memory item. `created_at` is set by the database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    space: Space
    kind: MemoryKind
    content: str
    source: MemorySource
    created_by_agent: str
    confidence: float
    trust: Trust
    created_at: AwareDatetime
    expires_at: AwareDatetime | None
    embedding_model: str

    def content_sha256(self) -> str:
        return text_sha256(self.content)


class MemoryMatch(BaseModel):
    """A search hit. `distance` is the cosine distance to the query, 0 being identical."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: MemoryItem
    distance: float = Field(ge=0, le=2)


class EpisodeDraft(BaseModel):
    """What a finished run leaves behind: a short summary, filed under an optional topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    space: Space
    run_id: UUID
    topic: str | None = Field(default=None, pattern=TOPIC_PATTERN)
    summary: str = Field(min_length=1, max_length=MAX_MEMORY_CHARS)
    trust: Trust

    @model_validator(mode="after")
    def _check(self) -> Self:
        _storable(self.summary, "summary")
        return self


class Episode(BaseModel):
    """A stored episode. Task, agent and outcome are read from the run it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    space: Space
    run_id: UUID
    task_id: UUID
    agent_id: str
    outcome: RunStatus
    topic: str | None
    summary: str
    trust: Trust
    created_at: AwareDatetime

    def summary_sha256(self) -> str:
        return text_sha256(self.summary)
