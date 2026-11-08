from __future__ import annotations

import asyncio
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.types import Implementation

from libi.mcp.client import McpClientManager
from libi.mcp.config import (
    McpConfigConflict,
    McpConfigFile,
    McpServerSpec,
    lock_mcp_config,
    mcp_config_path,
    missing_mcp_variables,
    project_mcp_trusted,
    spec_from_raw,
    transport_type,
)
from libi.mcp.identity import connection_identity, identity_key, server_icons
from libi.policy.redaction import Redactor

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FIELDS = {
    "type",
    "transport",
    "command",
    "args",
    "cwd",
    "env",
    "url",
    "headers",
    "timeout",
    "startup_timeout",
    "enabled",
}
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TRANSPORT_FIELDS = {"command", "args", "cwd", "env", "url", "headers"}
_PRIVATE_ENV = {"LIBI_API_KEY", "LIBI_RUNTIME_API_KEY", "LIBI_WEB_RUNTIME_KEY"}


def _redactor(raw: dict) -> Redactor:
    return Redactor(
        tuple(
            value
            for field in ("headers", "env")
            if isinstance(values := raw.get(field), dict)
            for value in values.values()
            if isinstance(value, str) and len(value) >= 5 and "${" not in value
        )
    )


def _public_config(raw: dict, sensitive_values: tuple[str, ...] = ()) -> dict:
    redactor = _redactor(raw)
    redactor.add_sensitive_values(sensitive_values)
    public = {"type": transport_type(raw), "enabled": raw.get("enabled", True) is not False}
    for key in ("command", "url", "cwd", "args"):
        if key in raw:
            public[key] = redactor.clean(raw[key])
    for key in ("headers", "env"):
        if isinstance(values := raw.get(key), dict):
            public[key] = dict.fromkeys(values)
    if "timeout" in raw or "startup_timeout" in raw:
        public["timeout"] = raw.get("timeout", raw.get("startup_timeout"))
    return public


def _validate(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("每项 MCP 配置都必须是对象。")
    transport = transport_type(raw)
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("请选择 STDIO、HTTP 或 SSE。")
    if raw.get("command") and raw.get("url"):
        raise ValueError("启动命令和服务地址只能填写一项。")
    for field in ("command", "cwd", "url"):
        if field in raw and not isinstance(raw[field], str):
            raise ValueError(f"{field} 必须是字符串。")
        if field in raw and "\0" in raw[field]:
            raise ValueError(f"{field} 包含无效字符。")
    if transport == "stdio":
        if not raw.get("command", "").strip() or raw.get("url"):
            raise ValueError("STDIO 服务需要填写启动命令。")
    else:
        try:
            url = urlsplit(raw.get("url", ""))
            valid = url.scheme in {"http", "https"} and url.hostname and url.username is None
            valid = valid and not url.password and not url.fragment
            _ = url.port
        except ValueError:
            valid = False
        if not valid or raw.get("command"):
            raise ValueError("请填写有效的 HTTP 或 HTTPS 服务地址，凭据请通过请求头配置。")
    args = raw.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise ValueError("启动参数必须是字符串数组。")
    for field, pattern in (("env", _ENV), ("headers", _HEADER)):
        values = raw.get(field, {})
        if not isinstance(values, dict) or any(
            not pattern.fullmatch(key)
            or not isinstance(value, str)
            or any(char in value for char in "\r\n\0")
            for key, value in values.items()
        ):
            raise ValueError(f"{field} 的名称或值无效。")
    timeout = raw.get("timeout", raw.get("startup_timeout", 30))
    if type(timeout) not in (int, float) or not 0 < timeout <= 300 or not math.isfinite(timeout):
        raise ValueError("超时必须大于 0 秒且不超过 300 秒。")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("enabled 必须为布尔值。")
    if any(f"${{{key}}}" in json.dumps(raw) for key in _PRIVATE_ENV):
        raise ValueError("请为 MCP 服务使用独立的环境变量。")
    return raw


def _new_config(name: str, raw: Any) -> dict:
    if not _NAME.fullmatch(name):
        raise ValueError("MCP 名称仅支持字母、数字、下划线和连字符，最多 64 个字符。")
    _validate(raw)
    if raw.keys() - _FIELDS:
        raise ValueError("配置包含当前版本不支持的字段。")
    return {**raw, "enabled": False, "managed_by_ui": True}


def _edit_config(before: dict, patch: Any, sensitive_values: tuple[str, ...]) -> dict:
    if not isinstance(patch, dict) or patch.keys() - _FIELDS:
        raise ValueError("配置包含无效字段。")
    public = _public_config(before, sensitive_values)
    after = {**before, **patch}
    if transport_type(after) != transport_type(before):
        after = {key: value for key, value in after.items() if key not in _TRANSPORT_FIELDS}
        after.update(patch)
    for key in ("command", "url", "cwd", "args"):
        if key in before and key in patch and patch[key] == public.get(key):
            after[key] = before[key]
    for key in ("env", "headers"):
        if isinstance(values := patch.get(key), dict):
            old_values = before.get(key, {})
            if not isinstance(old_values, dict):
                old_values = {}
            after[key] = {k: old_values.get(k) if v is None else v for k, v in values.items()}
    return _validate(after)


def _test_error(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        messages = [_test_error(item) for item in exc.exceptions]
        return next(
            (message for message in messages if message != _PROTOCOL_ERROR), _PROTOCOL_ERROR
        )
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "连接检测超时。请检查服务是否启动，或确认网络可达。"
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "找不到启动命令或工作目录。请检查 Runtime 的 PATH 和工作目录。"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return f"服务拒绝访问（HTTP {status}）。请检查请求头或服务授权。"
        return f"服务返回 HTTP {status}。请检查 MCP 地址和传输类型。"
    if isinstance(exc, httpx.RequestError):
        return "无法访问服务。请检查网络和 MCP 地址。"
    return _PROTOCOL_ERROR


_PROTOCOL_ERROR = "未完成 MCP 握手或工具发现。请检查服务地址、传输类型或启动参数。"


@dataclass
class _Check:
    config: dict
    result: dict | None = None


class McpManagement:
    def __init__(self, cwd: str, *, sensitive_values: tuple[str, ...] = ()):
        self.cwd = Path(cwd)
        self.user_path = mcp_config_path(Path.home())
        self.project_path = mcp_config_path(cwd)
        self.sensitive_values = sensitive_values
        self._checks: dict[tuple[str, str], _Check] = {}
        self._check_lock = Lock()
        self._closed = False
        self._check_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="libi-mcp")
        self._identities: dict[str, dict] = {}
        self._identity_lock = Lock()

    async def remember_discovered(self, manager: McpClientManager | None) -> None:
        if manager is not None:
            await asyncio.gather(
                *(
                    self._remember_identity(manager.specs[name], info)
                    for name, info in manager.server_info.items()
                )
            )

    async def _remember_identity(
        self, spec: McpServerSpec, info: Implementation, *, refresh: bool = False
    ) -> None:
        key = identity_key(spec)
        if not refresh and key in self._identities:
            return
        redactor = _redactor({"headers": spec.headers, "env": spec.env})
        redactor.add_sensitive_values(self.sensitive_values)
        title = redactor.clean(info.title or "").strip()[:100]
        identity = {"title": title or None, "icons": await server_icons(info, spec.url)}
        with self._identity_lock:
            self._identities[key] = identity
            if len(self._identities) > 64:
                del self._identities[next(iter(self._identities))]

    def _read_configs(self) -> tuple[dict[str, McpConfigFile], list[dict]]:
        files = {}
        warnings = []
        for scope, path in (("user", self.user_path), ("project", self.project_path)):
            if scope == "project" and path == self.user_path:
                continue
            try:
                files[scope] = McpConfigFile.read(path)
            except (OSError, ValueError):
                warnings.append(
                    {
                        "scope": scope,
                        "message": "无法读取 MCP 配置文件。请检查文件格式和权限。",
                    }
                )
        return files, warnings

    def check_at_startup(self) -> None:
        with self._check_lock:
            files, _ = self._read_configs()
            trusted = project_mcp_trusted()
            project = files["project"].servers if trusted and "project" in files else {}
            for scope, config in files.items():
                for name, raw in config.servers.items():
                    if (scope == "user" and name not in project) or (
                        scope == "project" and trusted
                    ):
                        self._schedule_check(scope, name, raw)

    def close(self) -> None:
        with self._check_lock:
            self._closed = True
        self._check_executor.shutdown(wait=True, cancel_futures=True)

    def _schedule_check(self, scope: str, name: str, raw: Any) -> None:
        try:
            _validate(raw)
        except ValueError:
            return
        if self._closed or not raw.get("enabled", True):
            return

        key = (scope, name)
        check = self._checks[key] = _Check(raw)

        def run() -> None:
            with self._check_lock:
                if self._closed or self._checks.get(key) is not check:
                    return
            asyncio.run(self._check(name, check))

        self._check_executor.submit(run)

    def list(self, *, auto_start: bool) -> dict:
        trusted = project_mcp_trusted()
        files, warnings = self._read_configs()
        servers = []
        project = files["project"].servers if "project" in files else {}
        for scope, config in files.items():
            for name, raw in config.servers.items():
                blocked = scope == "project" and not trusted
                overwritten = scope == "user" and trusted and name in project
                item: dict[str, Any] = {
                    "name": name,
                    "scope": scope,
                    "blocked": blocked,
                    "overridden": overwritten,
                    "config": {"type": "", "enabled": False},
                    "problem": None,
                    "check": None,
                    "checking": False,
                    "identity": None,
                }
                if not blocked:
                    try:
                        _validate(raw)
                        item["config"] = _public_config(raw, self.sensitive_values)
                        missing = missing_mcp_variables(raw)
                        spec = spec_from_raw(name, raw, self.cwd)
                        item["identity"] = {
                            **connection_identity(spec),
                            **self._identities.get(identity_key(spec), {}),
                        }
                        if missing:
                            item["problem"] = f"缺少环境变量 {', '.join(missing)}。"
                    except (ValueError, TypeError) as exc:
                        item["problem"] = str(exc)
                    checked = self._checks.get((scope, name))
                    if checked and checked.config == raw:
                        result = checked.result
                        item["check"] = result
                        item["checking"] = result is None
                servers.append(item)
        return {
            "servers": sorted(servers, key=lambda item: item["name"].casefold()),
            "revision": files["user"].revision if "user" in files else None,
            "workspace": str(self.cwd),
            "auto_start": auto_start,
            "warnings": warnings,
        }

    def preview(self, document: Any) -> dict:
        entries = document.get("mcpServers") if isinstance(document, dict) else None
        if not isinstance(entries, dict) or not entries:
            raise ValueError("请粘贴含有 mcpServers 对象的 JSON 配置。")
        config = McpConfigFile.read(self.user_path)
        servers = [
            {
                "name": name,
                "config": _public_config(_new_config(name, raw), self.sensitive_values),
                "conflict": name in config.servers,
            }
            for name, raw in entries.items()
        ]
        return {"servers": servers, "revision": config.revision}

    def mutate(self, action: str, body: dict, *, auto_start: bool = False) -> None:
        with lock_mcp_config(self.user_path), self._check_lock:
            config = McpConfigFile.read(self.user_path)
            if config.revision != body.get("revision"):
                raise McpConfigConflict("配置已在其他位置修改，请刷新列表后重试。")
            if action == "import":
                document = body.get("document")
                entries = document.get("mcpServers") if isinstance(document, dict) else None
                if not isinstance(entries, dict) or not entries:
                    raise ValueError("请粘贴含有 mcpServers 对象的 JSON 配置。")
                additions = {name: _new_config(name, raw) for name, raw in entries.items()}
                if additions.keys() & config.servers.keys():
                    raise McpConfigConflict("存在同名的用户级服务，请修改导入名称。")
                config.servers.update(additions)
            else:
                self._mutate_entry(config.servers, action, body)
            config.write(self.user_path)
            if action in {"edit", "toggle", "remove"}:
                name = body["name"]
                self._checks.pop(("user", name), None)
                if auto_start and action != "remove" and not self._project_overrides(name):
                    self._schedule_check("user", name, config.servers[name])

    def _project_overrides(self, name: str) -> bool:
        if self.project_path == self.user_path or not project_mcp_trusted():
            return False
        try:
            return name in McpConfigFile.read(self.project_path).servers
        except (OSError, ValueError):
            return False

    def _mutate_entry(self, servers: dict, action: str, body: dict) -> None:
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("请填写 MCP 名称。")
        if action == "add":
            if name in servers:
                raise McpConfigConflict("同名用户级服务已存在。")
            servers[name] = _new_config(name, body.get("config"))
            return
        if name not in servers:
            raise McpConfigConflict("该服务已被移除，请刷新列表。")
        if action == "remove":
            del servers[name]
            return
        before = servers[name]
        if not isinstance(before, dict):
            raise ValueError("此配置项无效，请在配置文件中修正或移除。")
        if action == "edit":
            servers[name] = _edit_config(before, body.get("config"), self.sensitive_values)
            return
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("请选择启用或停用。")
        if enabled:
            _validate(before)
        servers[name] = {**before, "enabled": enabled}

    async def test(self, body: dict) -> dict:
        name, scope = body.get("name"), body.get("scope")
        if not isinstance(name, str) or scope not in ("user", "project"):
            raise ValueError("请选择 MCP 服务。")
        if scope == "project" and not project_mcp_trusted():
            raise ValueError("当前项目的 MCP 配置尚未受信任。")
        with self._check_lock:
            file = McpConfigFile.read(self.user_path if scope == "user" else self.project_path)
            raw = _validate(file.servers.get(name))
            check = self._checks[(scope, name)] = _Check(raw)
        return await self._check(name, check)

    async def _check(self, name: str, check: _Check) -> dict:
        raw = check.config
        tools = []
        tool_count = 0
        error = None
        missing = missing_mcp_variables(raw)
        if missing:
            error = f"缺少环境变量 {', '.join(missing)}。"
        else:
            try:
                spec = spec_from_raw(name, raw, self.cwd)
                manager = McpClientManager(self.cwd, load_config=False)
                async with asyncio.timeout(min(spec.timeout, 10)):
                    discovered, _ = await manager.discover(spec)
                if info := manager.server_info.get(spec.name):
                    await self._remember_identity(spec, info, refresh=True)
                redactor = _redactor({"env": spec.env, "headers": spec.headers})
                redactor.add_sensitive_values(self.sensitive_values)
                tools = [
                    {
                        "name": redactor.clean(tool.name),
                        "description": redactor.clean((tool.description or "")[:500]),
                    }
                    for tool in discovered[:100]
                ]
                tool_count = len(discovered)
            except Exception as exc:  # Keep process and server diagnostics out of the response.
                error = _test_error(exc)
        check.result = {
            "checked_at": datetime.now(UTC).isoformat(),
            "error": error,
            "tools": tools,
            "tool_count": tool_count,
        }
        return check.result
