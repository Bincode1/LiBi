from __future__ import annotations

from collections.abc import Callable

from paicli.config import LlmConfig
from paicli.llm.openai_compatible import OpenAICompatibleClient
from paicli.llm.pricing import resolve_price_profile
from paicli.types import Usage

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
PROVIDER_BASE_URLS = {
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "kimi": "https://api.moonshot.cn/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "step": "https://api.stepfun.com/v1",
}

MODEL_CONTEXT_WINDOWS = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek-coder": 128_000,
    "glm-5.2": 200_000,
    "glm-5.1": 200_000,
    "glm-4.7": 200_000,
}

# Per-provider defaults. ``include_builtin`` controls whether resolve_price_profile
# falls back to the built-in price table (only DeepSeek ships one).
_PROVIDER_PROFILES = {
    "deepseek": {
        "base_url": DEEPSEEK_BASE_URL,
        "default_context_window": 64_000,
        "prompt_cache": True,
        "include_builtin": True,
    },
    "openai": {
        "base_url": OPENAI_BASE_URL,
        "default_context_window": 128_000,
        "prompt_cache": False,
        "include_builtin": False,
    },
    "openai-compatible": {
        "base_url": OPENAI_BASE_URL,
        "default_context_window": 128_000,
        "prompt_cache": False,
        "include_builtin": False,
    },
    "compatible": {
        "base_url": OPENAI_BASE_URL,
        "default_context_window": 128_000,
        "prompt_cache": False,
        "include_builtin": False,
    },
}
for _provider, _url in PROVIDER_BASE_URLS.items():
    _PROVIDER_PROFILES[_provider] = {
        "base_url": _url,
        "default_context_window": 128_000,
        "prompt_cache": False,
        "include_builtin": False,
    }

# Fallback for unknown providers (matches the original default branch).
_FALLBACK_PROFILE = {
    "base_url": DEEPSEEK_BASE_URL,
    "default_context_window": 64_000,
    "prompt_cache": False,
    "include_builtin": False,
}


def _provider_profile(provider: str) -> dict:
    return _PROVIDER_PROFILES.get(provider, _FALLBACK_PROFILE)


def create_llm_client(
    config: LlmConfig, *, on_usage: Callable[[Usage], None] | None = None
) -> OpenAICompatibleClient:
    provider = config.provider.lower()
    profile = _provider_profile(provider)
    context = config.context_window or MODEL_CONTEXT_WINDOWS.get(
        config.model.lower(), profile["default_context_window"]
    )
    return OpenAICompatibleClient(
        provider_name=provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url or profile["base_url"],
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        thinking_enabled=config.thinking_enabled,
        timeout=config.timeout,
        max_context_window=context,
        prompt_cache=profile["prompt_cache"],
        on_usage=on_usage,
        price_profile=resolve_price_profile(
            config.model,
            context_window=context,
            overrides=config.prices,
            include_builtin=profile["include_builtin"],
        ),
    )
