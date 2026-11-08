from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "developer", "user", "assistant", "tool"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cache_write_tokens: int = 0
    cache_context_tokens: int = 0

    def __post_init__(self) -> None:
        self.input_tokens = _non_negative_int(self.input_tokens)
        self.output_tokens = _non_negative_int(self.output_tokens)
        self.cache_hit_tokens = _non_negative_int(self.cache_hit_tokens)
        self.cache_miss_tokens = _non_negative_int(self.cache_miss_tokens)
        self.cache_write_tokens = _non_negative_int(self.cache_write_tokens)
        self.cache_context_tokens = _non_negative_int(self.cache_context_tokens)
        self.reasoning_tokens = _non_negative_int(self.reasoning_tokens)
        self.total_tokens = _non_negative_int(self.total_tokens)
        if not self.input_tokens and (self.cache_hit_tokens or self.cache_miss_tokens):
            self.input_tokens = self.cache_hit_tokens + self.cache_miss_tokens
        if not self.cache_context_tokens and (
            self.cache_hit_tokens or self.cache_miss_tokens or self.cache_write_tokens
        ):
            self.cache_context_tokens = self.input_tokens or (
                self.cache_hit_tokens + self.cache_miss_tokens + self.cache_write_tokens
            )
        if not self.total_tokens:
            self.total_tokens = self.input_tokens + self.output_tokens

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> Usage:
        """Normalize provider token fields while preserving PaiCLI's existing usage fields."""
        value = value or {}
        completion_details = value.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = {}

        input_tokens = _first_int(value, "input_tokens", "prompt_tokens", "inputTokens")
        output_tokens = _first_int(value, "output_tokens", "completion_tokens", "outputTokens")
        cache_hit_raw = _first_present(
            value,
            "prompt_cache_hit_tokens",
            "cache_hit_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cacheReadTokens",
        )
        if cache_hit_raw is None:
            cache_hit_raw = _nested_cache_tokens(value, "hit")
        cache_miss_raw = _first_present(value, "prompt_cache_miss_tokens", "cache_miss_tokens")
        if cache_miss_raw is None:
            cache_miss_raw = _nested_cache_tokens(value, "miss")
        cache_write_raw = _first_present(
            value,
            "prompt_cache_write_tokens",
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
            "cacheWriteTokens",
        )
        if cache_write_raw is None:
            cache_write_raw = _nested_cache_tokens(value, "write")

        cache_hit_tokens = _non_negative_int(cache_hit_raw)
        cache_write_tokens = _non_negative_int(cache_write_raw)
        cache_miss_tokens = _non_negative_int(cache_miss_raw)
        disjoint_buckets = _has_disjoint_cache_buckets(value)
        if disjoint_buckets:
            if cache_miss_raw is None:
                cache_miss_tokens = input_tokens
            cache_context_tokens = input_tokens + cache_hit_tokens + cache_write_tokens
        else:
            if cache_miss_raw is None and cache_hit_raw is not None:
                if input_tokens:
                    cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)
                elif cache_hit_tokens:
                    # A hit count without prompt size cannot produce a rate.
                    cache_hit_tokens = 0
            cache_context_raw = _first_present(value, "cache_context_tokens")
            if cache_context_raw is not None:
                cache_context_tokens = _non_negative_int(cache_context_raw)
            elif (
                cache_hit_raw is not None
                or cache_miss_raw is not None
                or cache_write_raw is not None
            ):
                cache_context_tokens = input_tokens or (
                    cache_hit_tokens + cache_miss_tokens + cache_write_tokens
                )
            else:
                cache_context_tokens = 0

        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            reasoning_tokens=_first_int(
                value,
                "reasoning_tokens",
                fallback=completion_details.get("reasoning_tokens"),
            ),
            total_tokens=_first_int(value, "total_tokens"),
            cache_write_tokens=cache_write_tokens,
            cache_context_tokens=cache_context_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        value = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.cache_write_tokens:
            value["cache_write_tokens"] = self.cache_write_tokens
        if self.cache_context_tokens and self.cache_context_tokens != self.input_tokens:
            value["cache_context_tokens"] = self.cache_context_tokens
        return value

    def __add__(self, other: Usage) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_context_tokens=self.cache_context_tokens + other.cache_context_tokens,
        )


def _has_disjoint_cache_buckets(value: dict[str, Any]) -> bool:
    if "inputTokens" in value:
        return True
    top_level_buckets = any(
        key in value
        for key in (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cacheReadTokens",
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
            "cacheWriteTokens",
        )
    )
    input_details = value.get("input_tokens_details")
    nested_buckets = isinstance(input_details, dict) and any(
        key in input_details
        for key in (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cacheReadTokens",
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
            "cacheWriteTokens",
        )
    )
    return "input_tokens" in value and (top_level_buckets or nested_buckets)


@dataclass(slots=True)
class QueryResult:
    text: str
    total_tokens: int
    turns: int
    usage: Usage = field(default_factory=Usage)
    cost: dict[str, Any] = field(default_factory=dict)
    plan_outcome: dict[str, Any] | None = None


def _first_int(value: dict[str, Any], *keys: str, fallback: Any = 0) -> int:
    for key in keys:
        if key in value and value[key] is not None:
            return _non_negative_int(value[key])
    return _non_negative_int(fallback)


def _nested_cache_tokens(value: dict[str, Any], kind: str) -> Any:
    detail_names = ("prompt_tokens_details", "input_tokens_details")
    candidate_names = (
        (
            "cached_tokens",
            "cache_hit_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cacheReadTokens",
        )
        if kind == "hit"
        else (
            ("cache_miss_tokens", "prompt_cache_miss_tokens")
            if kind == "miss"
            else (
                "cache_write_tokens",
                "cache_write_input_tokens",
                "prompt_cache_write_tokens",
                "cache_creation_input_tokens",
                "cacheWriteTokens",
            )
        )
    )
    for detail_name in detail_names:
        details = value.get(detail_name)
        if not isinstance(details, dict):
            continue
        for candidate in candidate_names:
            if candidate in details and details[candidate] is not None:
                return details[candidate]
    return None


def _first_present(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
