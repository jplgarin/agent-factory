"""Token counting for context budgets (SPEC 4.3)."""

from typing import Protocol


class TokenCounter(Protocol):
    """Counts tokens deterministically: the same text always gives the same number."""

    @property
    def name(self) -> str: ...

    def count(self, text: str) -> int: ...


class Utf8TokenEstimator:
    """One token per three UTF-8 bytes, rounded up.

    An estimate that needs no network call and no vendor tokenizer, so the context builder
    stays deterministic. It runs high for English and Spanish prose (about four characters per
    token) and close for JSON. Context budgets guard against runaway context; the hard token
    budget of a task (SPEC 2.8) is charged from the usage the model provider reports.
    """

    name = "utf8-bytes/3"

    def count(self, text: str) -> int:
        return -(-len(text.encode()) // 3)
