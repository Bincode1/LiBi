from dataclasses import dataclass


@dataclass(slots=True)
class ResearchBudget:
    """Web evidence budget shared by all parallel tools in one delegated turn."""

    searches_left: int
    fetches_left: int
    chars_left: int

    def reserve(self, kind: str) -> bool:
        field = "searches_left" if kind == "search" else "fetches_left"
        remaining = getattr(self, field)
        if remaining == 0 or self.chars_left == 0:
            return False
        setattr(self, field, remaining - 1)
        return True

    def take_text(self, text: str) -> str:
        evidence = text[: self.chars_left]
        self.chars_left -= len(evidence)
        if len(evidence) < len(text):
            evidence += "\n[Research evidence budget reached. Summarize the available evidence.]"
        return evidence
