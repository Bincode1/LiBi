from __future__ import annotations

import asyncio
import json

from paicli.config import PaiCliConfig
from paicli.policy import AuditLog
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import ToolContext
from paicli.tools.executor import ToolExecutor


def test_audit_redacts_content_commands_nested_fields_and_known_values(tmp_path):
    marker = "synthetic-sensitive-marker"
    audit = AuditLog(tmp_path / "audit.jsonl", sensitive_values=(marker,))
    payload = {
        "content": "api_key=synthetic-inline-value",
        "command": "curl -H 'Authorization: Bearer synthetic-bearer-value'",
        "nested": [{"headers": {"cookie": "synthetic-cookie-value"}}],
        "detail": marker,
        "token": "synthetic-token-value",
        "env_file": "DEEPSEEK_API_KEY='synthetic quoted value'",
        "json_string": json.dumps({"api_key": "synthetic-json-value"}),
    }
    audit.record(
        tool_name="write_file", input_data=payload, outcome="allow", approver="hitl", cwd="."
    )

    raw = audit.path.read_text(encoding="utf-8")
    assert "synthetic-" not in raw
    assert "quoted value" not in raw
    assert payload["detail"] == marker
    assert audit.tail()[0]["tool_name"] == "write_file"


def test_audit_tail_sanitizes_legacy_log_entries(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.path.write_text(
        json.dumps({"input": {"content": "password=synthetic-legacy-value"}}) + "\n",
        encoding="utf-8",
    )
    assert "synthetic-legacy-value" not in json.dumps(audit.tail())


def test_executor_audit_masks_configured_key_without_changing_written_content(tmp_path):
    config = PaiCliConfig()
    config.llm.api_key = "synthetic-configured-value"
    config.policy.hitl_mode = "never"
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)
    call = {
        "id": "write-1",
        "name": "write_file",
        "arguments": {"path": "note.txt", "content": config.llm.api_key},
    }
    result = asyncio.run(ToolExecutor(registry).execute_all([call], context))[0]

    assert not result.is_error
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == config.llm.api_key
    assert config.llm.api_key not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
