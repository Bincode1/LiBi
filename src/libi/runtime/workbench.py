from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import threading
import webbrowser
from contextlib import suppress
from pathlib import Path
from time import monotonic, sleep

import httpx

from libi.config import load_config
from libi.runtime.api import RuntimeApiServer
from libi.runtime.model_settings import WorkbenchModelSettings

_FRONTEND = Path(__file__).resolve().parents[3] / "frontend"


class WorkbenchError(RuntimeError):
    pass


def _find_node() -> str | None:
    executable = "node.exe" if os.name == "nt" else "node"
    for entry in os.get_exec_path():
        directory = Path(entry)
        if directory.is_absolute() and (node := shutil.which(str(directory / executable))):
            return node
    return None


def run_workbench(cwd: Path, *, port: int, open_browser: bool) -> None:
    node = _find_node()
    vite = _FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite.is_file():
        raise WorkbenchError(
            "Node.js and the frontend dependencies are required. See frontend/README.md."
        )

    with suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
        raise WorkbenchError(f"Workbench port {port} is already in use. Choose another --port.")

    origin = f"http://127.0.0.1:{port}"
    key = secrets.token_urlsafe(32)
    runtime = RuntimeApiServer(
        cwd=str(cwd),
        config=load_config(project_root=cwd),
        api_key=key,
        port=0,
        model_settings=WorkbenchModelSettings(),
    )
    with runtime.listen() as server:
        runtime_origin = f"http://127.0.0.1:{server.server_port}"
        env = {
            **os.environ,
            "LIBI_WEB_RUNTIME_URL": runtime_origin,
            "LIBI_WEB_RUNTIME_KEY": key,
        }
        env.pop("LIBI_API_KEY", None)
        env.pop("LIBI_RUNTIME_API_KEY", None)
        thread = threading.Thread(target=server.serve_forever, name="libi-workbench-runtime")
        thread.start()
        try:
            with subprocess.Popen(
                [node, str(vite), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=_FRONTEND,
                env=env,
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            ) as frontend:
                try:
                    _wait_until_ready(frontend, thread, origin, runtime_origin)
                    runtime.start_background_work()
                    print(f"LiBi workbench ready at {origin}", flush=True)
                    if open_browser:
                        with suppress(OSError, webbrowser.Error):
                            webbrowser.open(origin)
                    while frontend.poll() is None and thread.is_alive():
                        thread.join(0.25)
                    _check_processes(frontend, thread, origin)
                finally:
                    _stop_frontend(frontend)
        finally:
            server.shutdown()
            thread.join()


def _check_processes(frontend: subprocess.Popen, thread: threading.Thread, origin: str) -> None:
    status = frontend.poll()
    if status is not None:
        raise WorkbenchError(
            f"The workbench frontend exited with code {status}. "
            f"Check the Vite output and whether {origin} is already in use."
        )
    if not thread.is_alive():
        raise WorkbenchError("The workbench Runtime exited unexpectedly.")


def _wait_until_ready(
    frontend: subprocess.Popen, thread: threading.Thread, origin: str, runtime_origin: str
) -> None:
    deadline = monotonic() + 20
    with httpx.Client(base_url=origin, timeout=1, trust_env=False) as client:
        while monotonic() < deadline:
            _check_processes(frontend, thread, origin)
            try:
                bridge = client.get("/__libi/status").raise_for_status().json()
                if (
                    isinstance(bridge, dict)
                    and bridge.get("runtimeOrigin") == runtime_origin
                    and client.get("/v1/health").status_code == 200
                ):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            sleep(0.15)
    raise WorkbenchError(f"The workbench at {origin} did not become ready within 20 seconds.")


def _stop_frontend(frontend: subprocess.Popen) -> None:
    if frontend.poll() is None:
        with suppress(ProcessLookupError):
            frontend.terminate()
        try:
            frontend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            frontend.kill()
            frontend.wait()
