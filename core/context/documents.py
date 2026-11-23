"""Profile and preferences of a space (SPEC 4.3). They are read from the private configuration
directory when a context is built and are never copied into the database (PLAN G15)."""

import re
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _one_line(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("must be a single line without control characters")
    if value != value.strip():
        raise ValueError("must not start or end with whitespace")
    return value


Line = Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_one_line)]
ShortLine = Annotated[
    str, StringConstraints(min_length=1, max_length=200), AfterValidator(_one_line)
]
Language = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
StyleKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,49}$")]


class FileRef(BaseModel):
    """A configuration file a context block came from: its path and the hash of its bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SpaceProfile(BaseModel):
    """Stable facts about the owner that are relevant in one space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ShortLine
    role: ShortLine | None = None
    languages: tuple[Language, ...] = Field(min_length=1, max_length=10)
    timezone: str
    facts: tuple[Line, ...] = Field(default=(), max_length=50)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("not an IANA time zone") from error
        return value

    @model_validator(mode="after")
    def _unique_languages(self) -> Self:
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("languages must not repeat")
        return self


class Preferences(BaseModel):
    """How the owner wants work done in one space: named style settings and standing rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    style: dict[StyleKey, ShortLine] = Field(default_factory=dict[str, str], max_length=30)
    rules: tuple[Line, ...] = Field(default=(), max_length=50)
