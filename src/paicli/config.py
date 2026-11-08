from __future__ import annotations

import json
import math
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _home() -> Path:
    return Path.home()


@dataclass(slots=True)
class LlmConfig:
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    api_key: str = ""
    base_url: str | None = None
    context_window: int | None = None
    # Optional per-million-token overrides, keyed by currency then
    # input_cache_hit/input_cache_miss/output. Provider prices can change.
    prices: dict[str, dict[str, float]] = field(default_factory=dict)
    max_tokens: int = 8192
    temperature: float = 0.7
    thinking_enabled: bool = False
    timeout: float = 120.0


@dataclass(slots=True)
class ToolsConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    timeout: float = 60.0
    batch_timeout: float = 90.0
    max_concurrent_read: int = 4


@dataclass(slots=True)
class SubagentConfig:
    """Runtime limits for ReAct delegated agents."""

    max_active: int = 4
    max_resident: int = 32
    max_depth: int = 2
    default_deadline: float | None = None
    drain_timeout: float = 15.0
    observation_queue_size: int = 512
    research_max_searches: int | None = None
    research_max_fetches: int | None = None
    research_max_chars: int | None = None
    research_timeout: float = 240.0

    def __post_init__(self) -> None:
        for name in ("research_max_searches", "research_max_fetches", "research_max_chars"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"subagents.{name} must be a positive integer or null")
        if (
            type(self.research_timeout) not in (int, float)
            or not math.isfinite(self.research_timeout)
            or self.research_timeout <= 0
        ):
            raise ValueError("subagents.research_timeout must be a finite positive number")


@dataclass(slots=True)
class McpConfig:
    servers: list[dict[str, Any]] = field(default_factory=list)
    auto_start: bool = True


@dataclass(slots=True)
class MemoryConfig:
    long_term_enabled: bool = True
    long_term_db_path: str = "~/.paicli/memory.db"
    max_long_term_entries: int = 1_000
    max_memory_chars: int = 8_000
    recall_limit: int = 6
    recall_min_score: float = 0.05
    token_budget_mode: str = "balanced"
    compression_threshold: float = 0.8
    compression_target: float = 0.55
    compression_reserve_tokens: int = 1_024
    min_recent_messages: int = 6
    compression_mode: str = "llm"


@dataclass(slots=True)
class CompactionConfig:
    provider: str = ""
    model: str = ""
    max_output_tokens: int = 4_096
    temperature: float = 0.0
    max_tool_result_chars_in_input: int = 2_000


@dataclass(slots=True)
class PolicyConfig:
    hitl_mode: str = "auto"
    path_guard_enabled: bool = True
    command_guard_enabled: bool = True
    subagent_tool_allowlist: list[str] | None = None
    command_blacklist: list[str] = field(
        default_factory=lambda: [
            "sudo",
            "rm -rf /",
            "rm -rf ~",
            "mkfs",
            "dd if=/dev/zero",
            ":(){:|:&};:",
            "chmod -R 777 /",
            "curl | sh",
            "curl|sh",
            "shutdown",
            "reboot",
        ]
    )
    audit_log_path: str = "~/.paicli/audit.jsonl"


@dataclass(slots=True)
class PromptConfig:
    personality: str = "default"
    agent_mode: str = "react"
    custom_prompt_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FeatureConfig:
    mcp: bool = True
    skill: bool = True
    memory: bool = True
    audit_log: bool = True
    code_index: bool = True


@dataclass(slots=True)
class RuntimeConfig:
    port: int = 24158

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("runtime.port must be an integer from 1 to 65535")


@dataclass(slots=True)
class SessionTitleConfig:
    model: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or type(self.enabled) is not bool:
            raise ValueError("session_title requires a string model and a boolean enabled value")
        self.model = self.model.strip()


@dataclass(slots=True)
class PaiCliConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    render_mode: str = "inline"
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    subagents: SubagentConfig = field(default_factory=SubagentConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    session_title: SessionTitleConfig = field(default_factory=SessionTitleConfig)


def load_config(
    project_root: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str | None] | None = None,
) -> PaiCliConfig:
    env_map = env if env is not None else os.environ
    data = _config_to_dict(PaiCliConfig())

    user_config = _read_json(_home() / ".paicli" / "config.json")
    if user_config:
        data = _deep_merge(data, user_config)

    root = Path(project_root).resolve() if project_root else None
    if root:
        project_config = _read_json(root / ".paicli" / "config.json")
        if project_config:
            data = _deep_merge(data, project_config)
        project_env = _read_env(root / ".env")
        if project_env:
            data = _apply_env(data, project_env)

    if overrides:
        data = _deep_merge(data, overrides)

    data = _apply_env(data, env_map)
    memory = data.get("memory", {})
    if memory.get("compression_mode") not in {"llm", "disabled"}:
        raise ValueError(
            "memory.compression_mode must be llm or disabled; extractive compression was removed"
        )
    removed = {"max_conversation_history", "summary_max_chars", "mode_source"} & memory.keys()
    if removed:
        raise ValueError(f"removed memory settings: {', '.join(sorted(removed))}")
    if "context_compression" in data.get("features", {}):
        raise ValueError(
            "features.context_compression was removed; use memory.compression_mode=llm or disabled"
        )
    config = _dict_to_config(data)
    if config.prompt.agent_mode not in {"react", "plan"}:
        raise ValueError("prompt.agent_mode must be react or plan; Team mode has been removed")
    config.memory.long_term_db_path = _expand_home(config.memory.long_term_db_path)
    config.policy.audit_log_path = _expand_home(config.policy.audit_log_path)
    if config.compaction.max_output_tokens <= 0:
        raise ValueError("compaction.max_output_tokens must be positive")
    if config.compaction.temperature < 0:
        raise ValueError("compaction.temperature cannot be negative")
    return config


def get_config_paths(project_root: str | Path | None = None) -> list[Path]:
    paths = [_home() / ".paicli" / "config.json"]
    if project_root:
        paths.append(Path(project_root).resolve() / ".paicli" / "config.json")
    return paths


def config_to_public_dict(config: PaiCliConfig) -> dict[str, Any]:
    data = _config_to_dict(config)
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON configuration in {path} at line {exc.lineno}, column {exc.colno}"
        ) from None
    if not isinstance(raw, dict):
        raise ValueError(f"Configuration must be a JSON object: {path}")
    return raw


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


def _apply_env(data: dict[str, Any], env: dict[str, str | None]) -> dict[str, Any]:
    result = deepcopy(data)
    llm = result.setdefault("llm", {})
    features = result.setdefault("features", {})
    policy = result.setdefault("policy", {})

    mappings: list[tuple[str, str, Any]] = [
        ("PAICLI_API_KEY", "api_key", str),
        ("PAICLI_PROVIDER", "provider", str),
        ("PAICLI_MODEL", "model", str),
        ("PAICLI_BASE_URL", "base_url", str),
        ("PAICLI_CONTEXT_WINDOW", "context_window", int),
        ("PAICLI_MAX_TOKENS", "max_tokens", int),
        ("PAICLI_TEMPERATURE", "temperature", float),
    ]
    for env_key, config_key, caster in mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                llm[config_key] = caster(raw)

    thinking = env.get("PAICLI_THINKING")
    if thinking is not None:
        normalized_thinking = thinking.strip().lower()
        if normalized_thinking in {"1", "true", "yes", "on", "enabled"}:
            llm["thinking_enabled"] = True
        elif normalized_thinking in {"0", "false", "no", "off", "disabled"}:
            llm["thinking_enabled"] = False

    provider = str(llm.get("provider") or "").lower()
    if not llm.get("api_key"):
        provider_key_map = {
            "deepseek": ("DEEPSEEK_API_KEY",),
            "glm": ("ZAI_API_KEY", "GLM_API_KEY"),
            "zhipu": ("ZAI_API_KEY", "GLM_API_KEY"),
            "step": ("STEP_API_KEY",),
            "kimi": ("KIMI_API_KEY",),
            "moonshot": ("KIMI_API_KEY",),
            "freellmapi": ("FREELLMAPI_API_KEY",),
            "xfyun": ("XFYUN_API_KEY",),
            "agnes": ("AGNES_API_KEY",),
        }
        for provider_key in provider_key_map.get(provider, ()):
            if env.get(provider_key):
                llm["api_key"] = env[provider_key]
                break

    provider_model_key = f"{provider.upper()}_MODEL" if provider else ""
    provider_base_url_key = f"{provider.upper()}_BASE_URL" if provider else ""
    if provider_model_key and env.get(provider_model_key):
        llm["model"] = env[provider_model_key]
    if provider_base_url_key and env.get(provider_base_url_key):
        llm["base_url"] = env[provider_base_url_key]

    render_mode = env.get("PAICLI_RENDER_MODE") or env.get("PAICLI_RENDERER")
    if render_mode in {"plain", "inline"}:
        result["render_mode"] = render_mode

    if env.get("PAICLI_TUI") == "true":
        result["render_mode"] = "inline"

    for env_key, feature_key in [
        ("PAICLI_MCP", "mcp"),
        ("PAICLI_SKILL", "skill"),
        ("PAICLI_MEMORY", "memory"),
    ]:
        raw = env.get(env_key)
        if raw == "false":
            features[feature_key] = False
        elif raw == "true":
            features[feature_key] = True

    hitl = env.get("PAICLI_HITL")
    if hitl in {"always", "auto", "never"}:
        policy["hitl_mode"] = hitl

    return result


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(target)
    for key, value in source.items():
        if value is None:
            continue
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _deep_merge(old, value)
        else:
            result[key] = deepcopy(value)
    return result


def _config_to_dict(config: PaiCliConfig) -> dict[str, Any]:
    return asdict(config)


def _dict_to_config(data: dict[str, Any]) -> PaiCliConfig:
    return PaiCliConfig(
        llm=LlmConfig(**data.get("llm", {})),
        render_mode=data.get("render_mode", "inline"),
        tools=ToolsConfig(**data.get("tools", {})),
        subagents=SubagentConfig(**data.get("subagents", {})),
        mcp=McpConfig(**data.get("mcp", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        compaction=CompactionConfig(**data.get("compaction", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        features=FeatureConfig(**data.get("features", {})),
        runtime=RuntimeConfig(**data.get("runtime", {})),
        session_title=SessionTitleConfig(**data.get("session_title", {})),
    )


def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())
