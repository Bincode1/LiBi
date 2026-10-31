from __future__ import annotations

import json

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
