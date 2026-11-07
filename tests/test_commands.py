from __future__ import annotations

import asyncio
import sys

import pytest

from paicli.policy.command_guard import CommandPolicyError
from paicli.tools.commands import CommandExecutor, classify_command, sensitive_command_summary


@pytest.mark.integration
@pytest.mark.parametrize("exit_code", [0, 7])
def test_command_preserves_output_exit_code_cwd_and_env(tmp_path, exit_code):
    script = (
        "import os,sys; print(os.getcwd()); print(os.environ['PAICLI_TEST_VALUE']); "
        f"sys.stderr.write('diagnostic'); sys.exit({exit_code})"
    )
    result = asyncio.run(
        CommandExecutor().execute_list(
            [sys.executable, "-c", script], cwd=str(tmp_path), env={"PAICLI_TEST_VALUE": "injected"}
        )
    )
    assert str(tmp_path) in result.stdout
    assert "injected" in result.stdout
    assert result.stderr == "diagnostic"
    assert result.exit_code == exit_code
    assert result.success is (exit_code == 0)


@pytest.mark.integration
def test_command_timeout_kills_process_and_reports_failure(tmp_path):
    result = asyncio.run(
        CommandExecutor().execute_list(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(tmp_path),
            timeout=0.1,
        )
    )
    assert result.timed_out and not result.success
    assert result.exit_code == -1
    assert result.summary == "(timed out)"


@pytest.mark.integration
def test_command_cancellation_kills_and_reaps_child_process(tmp_path):
    async def run():
        executor = CommandExecutor()
        original_handle = executor._handle_process
        started = asyncio.Event()
        processes = []

        async def handle(proc, *args):
            processes.append(proc)
            started.set()
            return await original_handle(proc, *args)

        executor._handle_process = handle
        task = asyncio.create_task(
            executor.execute_list(
                [sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(tmp_path)
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None

    asyncio.run(run())


@pytest.mark.integration
def test_command_output_truncation_respects_budget(tmp_path):
    result = asyncio.run(
        CommandExecutor().execute_list(
            [sys.executable, "-c", "print('x' * 1000)"], cwd=str(tmp_path), max_output_chars=80
        )
    )
    assert result.success and result.truncated
    assert len(result.combined) == 80
    assert result.stdout.endswith("[output truncated]")


def test_command_guard_blocks_before_subprocess_creation(tmp_path, monkeypatch):
    async def unexpected_process(*_args, **_kwargs):
        pytest.fail("blocked command reached subprocess creation")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", unexpected_process)
    with pytest.raises(CommandPolicyError):
        asyncio.run(CommandExecutor().execute("rm -rf /", cwd=str(tmp_path)))


@pytest.mark.parametrize(
    ("command", "level"),
    [
        ("", "safe"),
        ("git status", "safe"),
        ("git push origin main", "medium"),
        ("npm install example", "medium"),
        ("rm -rf /", "high"),
        ("shutdown", "high"),
    ],
)
def test_command_risk_classification(command, level):
    assert classify_command(command) == level
    assert (sensitive_command_summary(command) is None) is (level == "safe")


@pytest.mark.integration
def test_shell_command_runs_with_no_output_and_keeps_exit_status(tmp_path):
    result = asyncio.run(CommandExecutor().execute("exit 7", cwd=str(tmp_path)))
    assert result.exit_code == 7
    assert not result.success
    assert result.summary == "(exit 7, no output)"
