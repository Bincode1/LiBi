from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paicli.policy.file_lock import try_file_lock

_VARIABLE = re.compile(r"\$\{([^}]+)\}")
_TRANSPORTS = {
    "stdio": "stdio",
    "local": "stdio",
    "http": "streamable-http",
    "streamable_http": "streamable-http",
    "streamable-http": "streamable-http",
    "sse": "sse",
}


class McpConfigConflict(ValueError):
    pass


@dataclass
class McpConfigFile:
    document: dict[str, Any]
    servers: dict[str, Any]
    revision: str

    @classmethod
    def read(cls, path: Path) -> McpConfigFile:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = b'{"mcpServers": {}}'
        try:
            document = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("MCP 配置文件不是有效的 JSON。") from exc
        if not isinstance(document, dict) or not isinstance(
            servers := document.get("mcpServers", document), dict
        ):
            raise ValueError("MCP 配置的 mcpServers 必须是对象。")
        return cls(document, servers, hashlib.sha256(content).hexdigest())

    def write(self, path: Path) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temp_path = Path(output.name)
        try:
            temp_path.write_text(
                json.dumps(self.document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def lock_mcp_config(path: Path):
    lock = try_file_lock(path.with_suffix(".lock"))
    if lock is None:
        raise McpConfigConflict("配置正在被其他进程修改，请重试。")
    return lock


def mcp_config_path(root: str | Path) -> Path:
    return Path(root) / ".paicli" / "mcp.json"


def project_mcp_trusted() -> bool:
    return os.environ.get("PAICLI_TRUST_PROJECT_MCP") == "1"


def transport_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or raw.get("transport") or ("http" if raw.get("url") else "stdio"))
    return _TRANSPORTS.get(value, value)


@dataclass(slots=True)
class McpServerSpec:
    name: str
    type: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = 30.0
    managed: bool = False


def load_mcp_server_specs(
    project_root: str | Path, *, include_project: bool = True
) -> dict[str, McpServerSpec]:
    root = Path(project_root).resolve()
    merged: dict[str, Any] = {}
    paths = [mcp_config_path(Path.home())]
    if include_project:
        paths.append(mcp_config_path(root))
    for path in paths:
        try:
            merged.update(McpConfigFile.read(path).servers)
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Could not read MCP configuration at %s", path)
    specs = {}
    for name, raw in merged.items():
        if not isinstance(raw, dict):
            continue
        if raw.get("managed_by_ui") is True and missing_mcp_variables(raw):
            logging.getLogger(__name__).warning("Skipping an MCP service with missing variables")
            continue
        try:
            specs[name] = spec_from_raw(name, raw, root)
        except (TypeError, ValueError, OverflowError):
            logging.getLogger(__name__).warning("Skipping an invalid MCP configuration entry")
    return specs


def write_chrome_devtools_config(
    *,
    scope_root: str | Path | None = None,
    browser_url: str | None = None,
    headless: bool = False,
    slim: bool = False,
    no_usage_statistics: bool = True,
) -> Path:
    root = Path(scope_root).resolve() if scope_root else Path.home()
    path = mcp_config_path(root)
    args = ["-y", "chrome-devtools-mcp@latest"]
    if no_usage_statistics:
        args.append("--no-usage-statistics")
    if slim:
        args.append("--slim")
    if headless:
        args.append("--headless")
    if browser_url:
        args.append(f"--browser-url={browser_url}")
    with lock_mcp_config(path):
        config = McpConfigFile.read(path)
        config.servers["chrome-devtools"] = {"type": "stdio", "command": "npx", "args": args}
        config.write(path)
    return path


def spec_from_raw(name: str, raw: dict[str, Any], project_root: Path) -> McpServerSpec:
    default_type = "streamable_http" if raw.get("url") else "stdio"
    server_type = str(raw.get("type") or raw.get("transport") or default_type)
    env = {
        key: _expand(str(value), project_root) for key, value in dict(raw.get("env") or {}).items()
    }
    args = [_expand(str(arg), project_root) for arg in raw.get("args") or []]
    cwd = raw.get("cwd")
    return McpServerSpec(
        name=name,
        type=server_type,
        command=_expand(str(raw["command"]), project_root) if raw.get("command") else None,
        args=args,
        env=env,
        cwd=_expand(str(cwd), project_root) if cwd else None,
        url=_expand(str(raw["url"]), project_root) if raw.get("url") else None,
        headers={
            key: _expand(str(value), project_root)
            for key, value in dict(raw.get("headers") or {}).items()
        },
        enabled=bool(raw.get("enabled", True)),
        timeout=float(raw.get("timeout", raw.get("startup_timeout", 30.0)) or 30.0),
        managed=raw.get("managed_by_ui") is True,
    )


def _expand(value: str, project_root: Path) -> str:
    replacements = {
        "PROJECT_DIR": str(project_root),
        "HOME": str(Path.home()),
    }

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return replacements.get(name, os.environ.get(name, ""))

    return _VARIABLE.sub(replace, value)


def missing_mcp_variables(raw: dict[str, Any]) -> list[str]:
    names = set(_VARIABLE.findall(json.dumps(raw)))
    return sorted(name for name in names - {"PROJECT_DIR", "HOME"} if not os.environ.get(name))
