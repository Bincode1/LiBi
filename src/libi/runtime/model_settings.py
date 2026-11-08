from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from libi.config import LlmConfig
from libi.llm.base import ProviderRequestError
from libi.llm.factory import llm_base_url
from libi.policy.file_lock import try_file_lock


class ModelSettingsBusy(ValueError):
    pass


class WorkbenchModelSettings:
    """Store a user-selected connection separately from CLI and project configuration."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".libi" / "workbench-model.json"

    def load(self, fallback: LlmConfig) -> LlmConfig:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fallback
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("本地模型配置文件无法读取，请检查其 JSON 格式。") from None
        if not isinstance(saved, dict):
            raise ValueError("本地模型配置文件必须包含一个 JSON 对象。")
        url = _base_url(saved.get("base_url"))
        model = _text(saved.get("model"), "模型 ID", 256)
        key = _api_key(saved.get("api_key"))
        provider = _text(saved.get("provider"), "服务类型", 64)
        return _apply_connection(fallback, url, key, model, provider)

    def save(self, body: dict[str, Any], fallback: LlmConfig) -> LlmConfig:
        lock = try_file_lock(self.path.with_suffix(".lock"))
        if lock is None:
            raise ModelSettingsBusy("模型配置正在保存，请稍后重试。")
        with lock:
            candidate = self.candidate(body, self.load(fallback), saving=True)
            fields = {
                "provider": candidate.provider,
                "base_url": candidate.base_url,
                "model": candidate.model,
                "api_key": candidate.api_key,
            }
            fd, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix="workbench-model-")
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(fields, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                temp_path.replace(self.path)
            finally:
                temp_path.unlink(missing_ok=True)
            return candidate

    @staticmethod
    def candidate(body: dict[str, Any], current: LlmConfig, *, saving: bool = False) -> LlmConfig:
        url = _base_url(body.get("base_url"))
        same_url = url == _public_base_url(current)
        raw_key = body.get("api_key", "")
        if isinstance(raw_key, str) and not raw_key.strip() and same_url:
            raw_key = current.api_key
        key = _api_key(raw_key)
        model = _text(body.get("model"), "模型 ID", 256) if saving else current.model
        if saving and contains_credential(model, key):
            raise ValueError("模型 ID 不能包含 API Key。")
        if contains_credential(url, key):
            raise ValueError("请将 API Key 填在专用字段中。")
        provider = current.provider if same_url else "openai-compatible"
        return _apply_connection(current, url, key, model, provider)

    @staticmethod
    def public(config: LlmConfig) -> dict[str, str | bool]:
        return {
            "base_url": _public_base_url(config),
            "model": "" if contains_credential(config.model, config.api_key) else config.model,
            "has_api_key": bool(config.api_key),
        }


def model_discovery_error(error: ProviderRequestError) -> str:
    status = error.details.get("status_code")
    if status in (401, 403):
        return f"模型服务拒绝访问（HTTP {status}）。请检查 API Key 和访问权限。"
    if status in (404, 405):
        return f"模型服务未提供模型列表（HTTP {status}）。请检查 Base URL，或手动填写模型 ID。"
    if status is not None:
        return f"获取模型列表失败（HTTP {status}）。"
    if error.details["kind"] == "timeout":
        return "获取模型列表超时。请检查连接后重试。"
    if error.details["kind"] == "protocol":
        return "模型服务返回的列表格式无效。请检查 Base URL，或手动填写模型 ID。"
    return "无法连接模型服务。请检查 Base URL 和网络。"


def contains_credential(text: str, key: str) -> bool:
    return bool(key) and (key == text or (len(key) >= 12 and key in text))


def _apply_connection(
    config: LlmConfig, url: str, key: str, model: str, provider: str
) -> LlmConfig:
    same_model = (url, model, provider) == (_public_base_url(config), config.model, config.provider)
    return replace(
        config,
        base_url=url,
        api_key=key,
        model=model,
        provider=provider,
        context_window=config.context_window if same_model else None,
        prices=config.prices if same_model else {},
    )


def _public_base_url(config: LlmConfig) -> str:
    try:
        url = _base_url(llm_base_url(config))
    except ValueError:
        return ""
    return "" if contains_credential(url, config.api_key) else url


def _base_url(value: object) -> str:
    raw = _text(value, "Base URL", 2048)
    try:
        url = httpx.URL(raw)
    except (ValueError, httpx.InvalidURL):
        raise ValueError("Base URL 必须是有效的 HTTP 或 HTTPS 地址。") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or "?" in raw
        or "#" in raw
        or any(character.isspace() or ord(character) < 32 for character in raw)
    ):
        raise ValueError("Base URL 必须是 HTTP 或 HTTPS 地址，且不能包含凭据、查询或片段。")
    return str(url).rstrip("/")


def _api_key(value: object) -> str:
    key = _text(value, "API Key", 4096)
    if not all(32 <= ord(character) < 127 for character in key):
        raise ValueError("API Key 包含无效字符。")
    return key


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请填写{field}。")
    result = value.strip()
    if len(result) > limit:
        raise ValueError(f"{field}过长。")
    return result
