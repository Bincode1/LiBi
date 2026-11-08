from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import asdict
from io import BytesIO
from pathlib import PureWindowsPath
from urllib.parse import urlsplit

import httpx
from mcp.types import Icon, Implementation
from PIL import Image

from libi.mcp.config import McpServerSpec

_AMAP_PACKAGE = "@amap/amap-maps-mcp-server"
_NPM_PACKAGE = re.compile(r"((?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+)(?:@[a-zA-Z0-9_.^~-]+)?")
_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/webp": "WEBP",
}
_MAX_ICON_BYTES = 128 * 1024


def connection_identity(spec: McpServerSpec) -> dict:
    """Derive a display source from connection configuration without using secrets."""
    if spec.type in {"stdio", "local"}:
        command = PureWindowsPath(spec.command or "").name
        if command.lower() in {"npx", "npx.cmd", "npx.exe"}:
            package = next((arg for arg in spec.args if arg not in {"-y", "--yes", "--"}), "")
            if match := _NPM_PACKAGE.fullmatch(package):
                name = match[1]
                return {"source": name, "brand": "amap" if name == _AMAP_PACKAGE else None}
        return {"source": command or None, "brand": None}
    try:
        url = urlsplit(spec.url or "")
        host, port = url.hostname, url.port
    except ValueError:
        return {"source": None, "brand": None}
    is_amap = (
        url.scheme == "https"
        and host == "mcp.amap.com"
        and port in {None, 443}
        and url.username is None
        and url.path.rstrip("/") == "/mcp"
    )
    source = url.netloc.rsplit("@", 1)[-1] if host else None
    return {"source": source, "brand": "amap" if is_amap else None}


def identity_key(spec: McpServerSpec) -> str:
    connection = asdict(spec)
    connection.pop("enabled")
    return hashlib.sha256(json.dumps(connection, sort_keys=True).encode()).hexdigest()


async def server_icons(info: Implementation, server_url: str | None) -> dict[str, str]:
    result = {}
    # Decoration must not delay the normal MCP startup beyond this shared deadline.
    try:
        async with asyncio.timeout(1.5):
            for icon in (info.icons or [])[:8]:
                theme = getattr(icon, "theme", None)
                if theme not in ("light", "dark"):
                    theme = "default"
                if theme not in result and (source := await _safe_icon(icon, server_url)):
                    result[theme] = source
    except TimeoutError:
        pass
    return result


async def _safe_icon(icon: Icon, server_url: str | None) -> str | None:
    try:
        payload, content_type = await _icon_bytes(icon.src, server_url)
        if not payload or len(payload) > _MAX_ICON_BYTES:
            return None
        expected = _FORMATS.get(content_type) or _FORMATS.get(icon.mimeType or "")
        with Image.open(BytesIO(payload), formats=list(set(_FORMATS.values()))) as image:
            if expected and image.format != expected:
                return None
            if max(image.size) > 512 or getattr(image, "n_frames", 1) != 1:
                return None
            clean = image.convert("RGBA")
            clean.thumbnail((64, 64))
            clean.info.clear()
            output = BytesIO()
            clean.save(output, format="PNG")
    except (
        OSError,
        ValueError,
        SyntaxError,
        httpx.HTTPError,
        httpx.InvalidURL,
        Image.DecompressionBombError,
    ):
        return None
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


async def _icon_bytes(source: str, server_url: str | None) -> tuple[bytes, str]:
    if source.startswith("data:"):
        header, _, encoded = source[5:].partition(",")
        mime, _, encoding = header.partition(";")
        if mime not in _FORMATS or encoding != "base64" or len(encoded) > _MAX_ICON_BYTES * 2:
            return b"", ""
        return base64.b64decode(encoded, validate=True), mime
    if len(source) > 2048 or not _same_https_origin(source, server_url):
        return b"", ""
    async with (
        httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
        client.stream(
            "GET",
            source,
            headers={"accept": "image/png,image/jpeg,image/webp", "accept-encoding": "identity"},
        ) as response,
    ):
        if response.status_code != 200:
            return b"", ""
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            return b"", ""
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime not in _FORMATS and mime not in {"", "application/octet-stream"}:
            return b"", ""
        chunks = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            chunks.extend(chunk)
            if len(chunks) > _MAX_ICON_BYTES:
                return b"", ""
        return bytes(chunks), mime


def _same_https_origin(source: str, server_url: str | None) -> bool:
    icon, server = urlsplit(source), urlsplit(server_url or "")
    return (
        icon.scheme == server.scheme == "https"
        and icon.hostname is not None
        and icon.hostname == server.hostname
        and (icon.port if icon.port is not None else 443)
        == (server.port if server.port is not None else 443)
        and icon.username is None
    )
