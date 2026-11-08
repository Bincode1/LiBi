from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from libi.entrypoints.cli import app
from libi.runtime import workbench
from libi.runtime.tasks import DurableTaskManager

pytestmark = pytest.mark.integration


def setup_frontend(tmp_path, monkeypatch, code):
    # A real child process exercises launcher cleanup without Node dependencies in Python CI.
    entry = tmp_path / "node_modules" / "vite" / "bin" / "vite.js"
    entry.parent.mkdir(parents=True)
    entry.write_text(code, encoding="utf-8")
    monkeypatch.setattr(workbench, "_FRONTEND", tmp_path)
    monkeypatch.setattr(workbench, "_find_node", lambda: sys.executable)


def test_find_node_skips_the_current_directory(tmp_path, monkeypatch):
    executable = "node.exe" if os.name == "nt" else "node"
    trusted = tmp_path / "bin"
    trusted.mkdir()
    (tmp_path / executable).touch()
    expected = trusted / executable
    expected.touch()
    expected.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(trusted))))
    assert workbench._find_node() == str(expected)


def test_workbench_port_conflict_leaves_listener_untouched(tmp_path, monkeypatch):
    setup_frontend(tmp_path, monkeypatch, "raise RuntimeError('frontend should not start')")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = CliRunner().invoke(
            app, ["workbench", "--cwd", str(tmp_path), "--port", str(port), "--no-browser"]
        )
        assert result.exit_code == 1
        assert f"Workbench port {port} is already in use" in result.output
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass


def test_workbench_stops_runtime_when_frontend_startup_fails(tmp_path, monkeypatch):
    tasks = DurableTaskManager(Path.home() / ".libi" / "tasks" / "tasks.db", scope=tmp_path)
    task_id = tasks.add("Run after the workbench is ready")
    setup_frontend(
        tmp_path,
        monkeypatch,
        "import json, os, urllib.request\n"
        "from pathlib import Path\n"
        "origin = os.environ['LIBI_WEB_RUNTIME_URL']\n"
        "request = urllib.request.Request(origin + '/v1/health', headers={\n"
        "    'x-api-key': os.environ['LIBI_WEB_RUNTIME_KEY']})\n"
        "with urllib.request.urlopen(request, timeout=4) as response:\n"
        "    assert json.load(response)['status'] == 'ok'\n"
        "Path('origin.txt').write_text(origin, encoding='utf-8')\n"
        "raise SystemExit(12)\n",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with pytest.raises(workbench.WorkbenchError, match="frontend exited with code 12"):
        workbench.run_workbench(tmp_path, port=port, open_browser=False)
    task = tasks.get(task_id)
    assert task and task.status == "queued" and task.attempts == 0
    origin = (tmp_path / "origin.txt").read_text(encoding="utf-8")
    with (
        httpx.Client(timeout=1, trust_env=False) as client,
        pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)),
    ):
        client.get(origin + "/v1/health")
