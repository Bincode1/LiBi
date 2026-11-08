from __future__ import annotations

import json

from libi.mcp import load_mcp_server_specs, write_chrome_devtools_config


def test_mcp_config_merges_scopes_and_expands_environment(tmp_path, isolated_home, monkeypatch):
    monkeypatch.setenv("TEST_MCP_HOST", "https://example.test/mcp")
    user = isolated_home / ".libi/mcp.json"
    user.parent.mkdir()
    user.write_text(
        json.dumps(
            {"mcpServers": {"shared": {"command": "old"}, "user-only": {"command": "user-command"}}}
        ),
        encoding="utf-8",
    )
    project = tmp_path / ".libi/mcp.json"
    project.parent.mkdir()
    project.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {
                        "url": "${TEST_MCP_HOST}",
                        "cwd": "${PROJECT_DIR}",
                        "headers": {"x-home": "${HOME}"},
                    },
                    "disabled": {"command": "never-start", "enabled": False},
                }
            }
        ),
        encoding="utf-8",
    )
    specs = load_mcp_server_specs(tmp_path)
    assert set(specs) == {"shared", "user-only", "disabled"}
    assert specs["shared"].type == "streamable_http"
    assert specs["shared"].url == "https://example.test/mcp"
    assert specs["shared"].cwd == str(tmp_path)
    assert specs["shared"].headers["x-home"] == str(isolated_home)
    assert not specs["disabled"].enabled


def test_chrome_config_preserves_other_servers_and_roundtrips_options(tmp_path):
    path = tmp_path / ".libi/mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"existing": {"command": "keep"}}}), encoding="utf-8")
    assert (
        write_chrome_devtools_config(
            scope_root=tmp_path, browser_url="http://localhost:9222", headless=True, slim=True
        )
        == path
    )
    specs = load_mcp_server_specs(tmp_path)
    assert specs["existing"].command == "keep"
    assert specs["chrome-devtools"].command == "npx"
    assert {
        "--headless",
        "--slim",
        "--no-usage-statistics",
        "--browser-url=http://localhost:9222",
    } <= set(specs["chrome-devtools"].args)
