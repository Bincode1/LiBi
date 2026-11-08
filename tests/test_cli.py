from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from libi import __version__
from libi.entrypoints.cli import app
from libi.session import SessionStore, default_runtime_db_path

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.mark.parametrize("arguments", [["--help"], ["mcp", "--help"], ["serve", "--help"]])
def test_cli_help_is_available_without_credentials(arguments):
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_python_module_entrypoint_works_without_credentials(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "libi", "--version"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == f"libi {__version__}"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--resume", "--session-id", "missing"], "use either"),
        (["--session-id", "missing", "-p", "hello"], "session not found"),
        (["--mode", "team", "-p", "hello"], "mode must be react or plan"),
        (["worker", "--workers", "0"], "workers must be at least 1"),
        (["mcp", "serve", "--transport", "udp"], "transport must be stdio or http"),
        (["mcp", "init-chrome", "--scope", "global"], "scope must be user or project"),
    ],
)
def test_cli_rejects_invalid_arguments(tmp_path, arguments, message, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, arguments)
    assert result.exit_code != 0
    assert message in result.output


def test_single_prompt_fails_cleanly_without_credentials(tmp_path):
    result = runner.invoke(app, ["--cwd", str(tmp_path), "-p", "hello"])
    assert result.exit_code == 1
    assert "LLM API key is not configured" in result.output


@pytest.mark.parametrize("mode", ["react", "plan"])
def test_cli_prompt_uses_real_runtime_and_persists_turn(
    tmp_path, monkeypatch, provider_stream, mode
):
    monkeypatch.setenv("LIBI_API_KEY", "synthetic-cli-credential")
    monkeypatch.setenv("LIBI_MCP", "false")
    batches, requests = provider_stream
    if mode == "plan":
        batches.append(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": json.dumps(
                                    {
                                        "tasks": [
                                            {"id": "answer", "description": "Answer the request"}
                                        ]
                                    }
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            ]
        )
    batches.append(
        [
            {"choices": [{"delta": {"content": "CLI answer"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        ]
    )
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--mode", mode, "--json", "-p", "hello"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "CLI answer" in payload["text"]
    assert payload["mode"] == mode
    assert payload["total_tokens"] == 15
    assert payload["usage"]["input_tokens"] == 12
    assert len(requests) == (2 if mode == "plan" else 1)
    store = SessionStore(default_runtime_db_path())
    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].status == "completed"


def test_cli_provider_failure_is_nonzero_and_records_failed_turn(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("LIBI_API_KEY", "synthetic-cli-credential")
    monkeypatch.setenv("LIBI_MCP", "false")

    async def reject(_transport, _request):
        return httpx.Response(401, text="provider diagnostic must stay private")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)
    result = runner.invoke(app, ["--cwd", str(tmp_path), "-p", "hello"])
    assert result.exit_code == 1
    assert "HTTP 401" in result.output
    assert "provider diagnostic" not in result.output
    assert "synthetic-cli-credential" not in result.output
    assert SessionStore(default_runtime_db_path()).list_sessions()[0].status == "failed"


def test_cli_session_selection_retries_invalid_input_then_resumes(tmp_path, monkeypatch):
    store = SessionStore(default_runtime_db_path(), workspace=tmp_path)
    sid = store.create_session()
    calls = []

    async def repl(cwd, config, *, session_id):
        calls.append((cwd, session_id))

    monkeypatch.setattr("libi.entrypoints.repl.start_repl", repl)
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--resume"], input="99\n1\n")
    assert result.exit_code == 0, result.output
    assert "Please enter a number" in result.output
    assert calls == [(str(tmp_path), sid)]


@pytest.mark.parametrize("has_session", [False, True])
def test_cli_session_selection_can_exit(tmp_path, has_session):
    if has_session:
        SessionStore(default_runtime_db_path(), workspace=tmp_path).create_session()
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--resume"], input="q\n")
    assert result.exit_code == (0 if has_session else 1)


def test_cli_cannot_resume_an_unbound_session(tmp_path):
    store = SessionStore(default_runtime_db_path())
    session_id = store.create_session()
    store.append(session_id, "user/message", {"content": "older question"})
    args = ["--cwd", str(tmp_path), "--session-id", session_id]
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert "session not found" in result.output
    assert store.events(session_id)[0].payload == {"content": "older question"}


def test_cli_doctor_reports_status_without_disclosing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBI_API_KEY", "synthetic-doctor-credential")
    result = runner.invoke(app, ["doctor", "--cwd", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["api_key"] == "configured"
    assert "synthetic-doctor-credential" not in result.output


def test_cli_serve_requires_runtime_credentials(tmp_path):
    result = runner.invoke(app, ["serve", "--cwd", str(tmp_path)])
    assert result.exit_code == 1
    assert "LIBI_RUNTIME_API_KEY is required" in result.output


def test_cli_serve_fails_on_occupied_project_port(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBI_RUNTIME_API_KEY", "synthetic-runtime-key")
    config_dir = tmp_path / ".libi"
    config_dir.mkdir()

    def unexpected_listener(_server):
        pytest.fail("Runtime must not bind a different port")

    monkeypatch.setattr("libi.runtime.api.RuntimeApiServer._start_workers", unexpected_listener)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        (config_dir / "config.json").write_text(json.dumps({"runtime": {"port": port}}))
        result = runner.invoke(app, ["serve", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert f"Cannot listen on 127.0.0.1:{port}" in result.output


def test_cli_mcp_config_roundtrip(tmp_path):
    arguments = ["--cwd", str(tmp_path)]
    empty = runner.invoke(app, ["mcp", "list", *arguments])
    assert empty.exit_code == 0
    assert "No MCP servers" in empty.output
    created = runner.invoke(app, ["mcp", "init-chrome", "--headless", *arguments])
    assert created.exit_code == 0, created.output
    listed = runner.invoke(app, ["mcp", "list", *arguments])
    assert listed.exit_code == 0
    assert "chrome-devtools" in listed.output
