from __future__ import annotations

import ipaddress
import json
import os
import socket

import httpx
import pytest

from libi.config import LiBiConfig
from libi.session import SessionStore
from libi.tools import ToolRegistry, get_builtin_tools
from libi.tools.base import ToolContext


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    # Keep the home beside workspaces: snapshots must never copy their own storage.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in tuple(os.environ):
        if name.startswith("LIBI_") or name.endswith(("_API_KEY", "_BASE_URL", "_MODEL")):
            monkeypatch.delenv(name)
    return home


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo
    attempts = []

    def check_address(address):
        if not isinstance(address, tuple):
            return
        host = str(address[0])
        if host == "localhost":
            return
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            attempts.append(host)
            raise AssertionError("Tests must mock external network connections")

    def connect(sock, address):
        check_address(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check_address(address)
        return original_connect_ex(sock, address)

    def resolve(host, port, *args, **kwargs):
        check_address((host, port))
        return original_resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    yield attempts
    assert not attempts, "External network was attempted, even if application code caught the error"


@pytest.fixture
def config(tmp_path) -> LiBiConfig:
    config = LiBiConfig()
    config.session_title.enabled = False
    config.features.mcp = False
    config.memory.long_term_db_path = str(tmp_path / "memory.db")
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


@pytest.fixture
def session_store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "runtime.db")


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    return registry


@pytest.fixture
def tool_context(tmp_path, config) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), config=config)


@pytest.fixture
def provider_stream(monkeypatch):
    """Script provider SSE responses while exercising the real HTTP client and agent."""
    requests = []
    batches = []

    async def respond(_transport, request):
        requests.append(json.loads(request.content))
        if not batches:
            raise AssertionError("Provider received an unexpected extra request")
        chunks = batches.pop(0)
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    return batches, requests
