from enum import StrEnum


class Space(StrEnum):
    """An isolated tenant. Every resource belongs to exactly one space."""

    PERSONAL = "personal"
    SYNAPTION = "synaption"

    @property
    def temporal_namespace(self) -> str:
        return f"af-{self.value}"

    @property
    def task_queue(self) -> str:
        return f"af-{self.value}"
