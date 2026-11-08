import time
from dataclasses import dataclass

from paicli.context.tokens import estimate_text_tokens, fit_text


@dataclass(slots=True)
class ResearchBudget:
    """Web evidence budget shared by all parallel tools in one delegated turn."""

    searches_left: int | None = None
    fetches_left: int | None = None
    chars_left: int | None = None
    tokens_left: int | None = None
    deadline: float | None = None
    request_tokens_left: int | None = None

    @property
    def remaining_seconds(self) -> float | None:
        return max(0.0, self.deadline - time.monotonic()) if self.deadline is not None else None

    def reserve(self, kind: str) -> bool:
        field = "searches_left" if kind == "search" else "fetches_left"
        remaining = getattr(self, field)
        if 0 in (
            remaining,
            self.chars_left,
            self.tokens_left,
            self.request_tokens_left,
            self.remaining_seconds,
        ):
            return False
        if remaining is not None:
            setattr(self, field, remaining - 1)
        return True

    def take_text(self, text: str) -> str:
        evidence = text[: self.chars_left]
        limits = [n for n in (self.tokens_left, self.request_tokens_left) if n is not None]
        if limits:
            evidence = fit_text(evidence, min(limits))
        tokens = estimate_text_tokens(evidence)
        if self.chars_left is not None:
            self.chars_left -= len(evidence)
        if self.tokens_left is not None:
            self.tokens_left -= tokens
        if self.request_tokens_left is not None:
            self.request_tokens_left -= tokens
        return evidence
