from __future__ import annotations

import json

import pytest

from paicli.config import load_config


@pytest.mark.parametrize("port", [0, 65536, True, "24158"])
def test_runtime_port_rejects_non_listening_values(port):
    with pytest.raises(ValueError, match="runtime.port"):
        load_config(overrides={"runtime": {"port": port}})


def test_config_precedence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".paicli").mkdir(parents=True)
    (project / ".paicli").mkdir(parents=True)
    (home / ".paicli" / "config.json").write_text(
        json.dumps({"llm": {"provider": "home", "model": "home-model"}}),
        encoding="utf-8",
    )
    (project / ".paicli" / "config.json").write_text(
        json.dumps({"llm": {"provider": "project", "model": "project-model"}}),
        encoding="utf-8",
    )
    (project / ".env").write_text("PAICLI_MODEL=env-file-model\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PAICLI_PROVIDER", "process")

    config = load_config(
        project_root=project,
        overrides={"llm": {"model": "cli-model"}},
    )

    assert config.llm.provider == "process"
    assert config.llm.model == "cli-model"


@pytest.mark.parametrize("highest_layer", range(6))
def test_config_precedence_at_every_layer(tmp_path, isolated_home, highest_layer):
    user = isolated_home / ".paicli/config.json"
    project = tmp_path / ".paicli/config.json"
    if highest_layer >= 1:
        user.parent.mkdir()
        user.write_text('{"llm":{"model":"user"}}', encoding="utf-8")
    if highest_layer >= 2:
        project.parent.mkdir()
        project.write_text('{"llm":{"model":"project"}}', encoding="utf-8")
    if highest_layer >= 3:
        (tmp_path / ".env").write_text("PAICLI_MODEL=dotenv\n", encoding="utf-8")
    overrides = {"llm": {"model": "cli"}} if highest_layer >= 4 else None
    env = {"PAICLI_MODEL": "process"} if highest_layer >= 5 else {}
    config = load_config(project_root=tmp_path, env=env, overrides=overrides)
    assert (
        config.llm.model
        == ["deepseek-flash", "user", "project", "dotenv", "cli", "process"][highest_layer]
    )


def test_provider_specific_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PAICLI_PROVIDER", "deepseek")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "deepseek-key"


@pytest.mark.parametrize("trusted", [False, True])
def test_project_configuration_cannot_retarget_process_credentials(tmp_path, trusted):
    (tmp_path / ".paicli").mkdir()
    (tmp_path / ".paicli/config.json").write_text(
        json.dumps(
            {
                "llm": {"base_url": "https://project.example/v1", "model": "project-model"},
                "policy": {"hitl_mode": "never", "path_guard_enabled": False},
                "features": {"audit_log": False},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "PAICLI_TRUST_PROJECT_CONFIG=1\nDEEPSEEK_BASE_URL=https://dotenv.example/v1\n"
        "PAICLI_HITL=never\n",
        encoding="utf-8",
    )
    env = {"DEEPSEEK_API_KEY": "synthetic-key"}
    if trusted:
        env["PAICLI_TRUST_PROJECT_CONFIG"] = "1"
    config = load_config(project_root=tmp_path, env=env)
    assert config.llm.model == "project-model"
    assert config.llm.api_key == "synthetic-key"
    assert config.llm.base_url == ("https://dotenv.example/v1" if trusted else None)
    assert config.policy.hitl_mode == ("never" if trusted else "auto")
    assert config.policy.path_guard_enabled is not trusted
    assert config.features.audit_log is not trusted


def test_project_dotenv_provider_key_only_applies_to_default_endpoint(tmp_path):
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=synthetic-project-key\n", encoding="utf-8")
    assert load_config(tmp_path, env={}).llm.api_key == "synthetic-project-key"
    assert not load_config(
        tmp_path, env={"PAICLI_BASE_URL": "https://proxy.example/v1"}
    ).llm.api_key
    assert (
        load_config(tmp_path, env={"DEEPSEEK_API_KEY": "synthetic-process-key"}).llm.api_key
        == "synthetic-process-key"
    )


def test_invalid_thinking_env_keeps_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PAICLI_THINKING", "maybe")

    config = load_config(project_root=tmp_path)

    assert config.llm.thinking_enabled is False


def test_removed_team_mode_is_rejected_from_config(tmp_path):
    config_dir = tmp_path / ".paicli"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"prompt": {"agent_mode": "team"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Team mode has been removed"):
        load_config(project_root=tmp_path)


def test_existing_config_uses_llm_compression_default(tmp_path, monkeypatch):
    monkeypatch.setattr("paicli.config._home", lambda: tmp_path)
    config_dir = tmp_path / ".paicli"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"render_mode":"plain"}', encoding="utf-8")
    assert load_config(project_root=tmp_path, env={}).memory.compression_mode == "llm"


@pytest.mark.parametrize(
    "settings",
    [
        {"memory": {"compression_mode": "legacy_extractive"}},
        {"memory": {"summary_max_chars": 6000}},
        {"memory": {"max_conversation_history": 100}},
        {"features": {"context_compression": False}},
    ],
)
def test_removed_compression_settings_fail_with_migration_message(tmp_path, settings):
    with pytest.raises(ValueError, match="removed"):
        load_config(project_root=tmp_path, overrides=settings, env={})


@pytest.mark.parametrize(
    ("source", "content"), [("user", "{invalid"), ("project", "[]"), ("project", "null")]
)
def test_invalid_json_config_fails_without_exposing_content(tmp_path, monkeypatch, source, content):
    home = tmp_path / "home"
    project = tmp_path / "project"
    root = home if source == "user" else project
    path = root / ".paicli" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("paicli.config._home", lambda: home)

    with pytest.raises(ValueError) as captured:
        load_config(project_root=project, env={})
    assert str(path) in str(captured.value)
    assert content not in str(captured.value)


@pytest.mark.parametrize("filename", [".paicli/config.json", ".env"])
def test_unreadable_config_is_not_silently_ignored(tmp_path, monkeypatch, filename):
    from pathlib import Path

    monkeypatch.setattr("paicli.config._home", lambda: tmp_path / "home")
    original = Path.read_text
    target = tmp_path / filename

    def read(path, *args, **kwargs):
        if path == target:
            raise PermissionError("configuration is unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError, match="unreadable"):
        load_config(project_root=tmp_path, env={})


def test_subagent_limits_survive_config_loading(tmp_path):
    config = load_config(
        project_root=tmp_path,
        env={},
        overrides={"subagents": {"max_active": 2, "max_depth": 1, "drain_timeout": 0.5}},
    )
    assert config.subagents.max_active == 2
    assert config.subagents.max_depth == 1
    assert config.subagents.drain_timeout == 0.5


@pytest.mark.parametrize("enabled", ["1", "true", "yes", "on", "enabled", " TRUE "])
def test_thinking_enabled_aliases(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("PAICLI_THINKING", enabled)
    assert load_config(project_root=tmp_path).llm.thinking_enabled


@pytest.mark.parametrize("disabled", ["0", "false", "no", "off", "disabled", " FALSE "])
def test_thinking_disabled_aliases(tmp_path, disabled):
    assert not load_config(
        project_root=tmp_path,
        env={"PAICLI_THINKING": disabled},
        overrides={"llm": {"thinking_enabled": True}},
    ).llm.thinking_enabled


@pytest.mark.parametrize(
    "overrides", [{"max_output_tokens": 0}, {"max_output_tokens": -1}, {"temperature": -0.1}]
)
def test_invalid_compaction_parameters_fail_explicitly(tmp_path, overrides):
    with pytest.raises(ValueError, match="compaction"):
        load_config(project_root=tmp_path, env={}, overrides={"compaction": overrides})


def test_public_config_masks_key_without_mutating_original(config):
    from paicli.config import config_to_public_dict

    config.llm.api_key = load_config(
        env={"PAICLI_API_KEY": "synthetic-public-credential"}
    ).llm.api_key
    public = config_to_public_dict(config)
    assert public["llm"]["api_key"] == "***"
    assert config.llm.api_key == "synthetic-public-credential"
