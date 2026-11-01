from __future__ import annotations

import json

import pytest

from paicli.config import load_config


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
    monkeypatch.setenv("PAICLI_PROVIDER", "process")

    config = load_config(
        project_root=project,
        overrides={"llm": {"model": "cli-model"}},
    )

    assert config.llm.provider == "process"
    assert config.llm.model == "cli-model"


def test_provider_specific_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_PROVIDER", "deepseek")
    monkeypatch.delenv("PAICLI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "deepseek-key"


def test_thinking_is_disabled_by_default_and_can_be_enabled_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_THINKING", "true")

    config = load_config(project_root=tmp_path)

    assert config.llm.thinking_enabled is True


def test_invalid_thinking_env_keeps_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PAICLI_THINKING", "maybe")

    config = load_config(project_root=tmp_path)

    assert config.llm.thinking_enabled is False


def test_removed_team_mode_is_rejected_from_config(tmp_path, monkeypatch):
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


@pytest.mark.parametrize("settings", [
    {"memory": {"compression_mode": "legacy_extractive"}},
    {"memory": {"summary_max_chars": 6000}},
    {"memory": {"max_conversation_history": 100}},
    {"features": {"context_compression": False}},
])
def test_removed_compression_settings_fail_with_migration_message(tmp_path, settings):
    with pytest.raises(ValueError, match="removed"):
        load_config(project_root=tmp_path, overrides=settings, env={})
