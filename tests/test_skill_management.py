from __future__ import annotations

import asyncio

import pytest

from libi.config import LiBiConfig
from libi.skill import SkillContextBuffer, SkillRegistry, SkillStateStore
from libi.skill.management import SkillConflict, SkillManagement
from libi.tools.base import ToolContext
from libi.tools.builtins import load_skill


def document(name="release-check", body="Run the release checks."):
    return (
        f'---\nname: {name}\ndescription: >\n  检查发布流程\n  并验证结果\nversion: "2.1"\n'
        f'tags:\n  - 发布\n  - "a,b"\nauthor: team\n---\n\n{body}\n'
    )


def save(management, action, **body):
    management.mutate(action, {"revision": management.inventory()["revision"], **body})


def test_management_layers_project_switches_and_fallback(tmp_path):
    other = tmp_path / "other"
    registry = SkillRegistry(tmp_path)
    registry.create("release-check", description="Global workflow", body="User body", scope="user")
    registry.create("release-check", description="Project workflow", body="Project body")
    management = SkillManagement(str(tmp_path))
    rows = [s for s in management.inventory()["skills"] if s["name"] == "release-check"]
    assert len(rows) == 2
    assert next(s for s in rows if s["scope"] == "user")["overridden"]
    save(management, "toggle", slug="release-check", scope="project", enabled=False)
    assert SkillRegistry(tmp_path).load("release-check") is None
    assert SkillRegistry(other).load("release-check").body == "User body"
    save(management, "reset", slug="release-check", scope="project")
    assert SkillRegistry(tmp_path).load("release-check").body == "Project body"
    save(management, "remove", slug="release-check", scope="project")
    assert SkillRegistry(tmp_path).load("release-check").body == "User body"


def test_import_preserves_resources_and_requires_unchanged_preview(tmp_path):
    source = tmp_path / "download" / "release-check"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(document(), encoding="utf-8")
    (source / "references").mkdir()
    reference = source / "references" / "release.txt"
    reference.write_text("original", encoding="utf-8")
    management = SkillManagement(str(tmp_path))
    body = {"slug": "release-check", "scope": "user", "source_path": str(source)}
    preview = management.preview(body)
    assert not preview["conflict"]
    assert len(preview["files"]) == 2
    reference.write_text("updated", encoding="utf-8")
    with pytest.raises(SkillConflict, match="导入源"):
        save(management, "import", **body, fingerprint=preview["fingerprint"])
    preview = management.preview(body)
    save(management, "import", **body, fingerprint=preview["fingerprint"])
    skill = SkillRegistry(tmp_path).load("release-check")
    assert skill.tags == ["发布", "a,b"]
    assert "检查发布流程 并验证结果" in skill.description
    assert (skill.path.parent / "references" / "release.txt").read_text() == "updated"
    detail = management.detail(body)
    assert detail["document"] == document()
    assert len(detail["resources"]) == 2
    save(management, "edit", slug="release-check", scope="user", document=document(body="New body"))
    assert (skill.path.parent / "references" / "release.txt").is_file()
    assert "author: team" in skill.path.read_text(encoding="utf-8")


def test_stale_revision_and_invalid_writes_leave_files_untouched(tmp_path):
    management = SkillManagement(str(tmp_path))
    original_revision = management.inventory()["revision"]
    save(management, "add", slug="release-check", scope="project", document=document())
    with pytest.raises(SkillConflict):
        management.mutate(
            "remove", {"slug": "release-check", "scope": "project", "revision": original_revision}
        )
    with pytest.raises(ValueError, match="name"):
        save(
            management,
            "edit",
            slug="release-check",
            scope="project",
            document=document("different"),
        )
    with pytest.raises(ValueError):
        save(management, "add", slug="../escape", scope="user", document=document())
    with pytest.raises(ValueError, match="只读"):
        save(management, "remove", slug="web-access", scope="builtin")
    assert SkillRegistry(tmp_path).load("release-check").body == "Run the release checks."
    skill = SkillRegistry(tmp_path).load("release-check")
    skill.path.write_text(document(body="External edit"), encoding="utf-8")
    with pytest.raises(SkillConflict):
        management.mutate(
            "edit",
            {
                "slug": "release-check",
                "scope": "project",
                "document": document(),
                "revision": original_revision,
            },
        )


def test_corrupt_state_is_reported_and_not_overwritten(tmp_path):
    state = SkillStateStore()
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text("{invalid", encoding="utf-8")
    management = SkillManagement(str(tmp_path))
    inventory = management.inventory()
    assert inventory["revision"] is None
    assert inventory["warnings"]
    with pytest.raises(SkillConflict):
        save(management, "add", slug="release-check", scope="user", document=document())
    assert state.path.read_text() == "{invalid"


def test_legacy_switches_survive_workspace_overrides(tmp_path):
    state = SkillStateStore()
    state.disable("release-check")
    state.set_project(tmp_path, "release-check", True)
    state.disable("other")
    assert "release-check" not in state.disabled(tmp_path)
    assert {"release-check", "other"} <= state.disabled()
    state.set_project(tmp_path, "release-check", None)
    assert "release-check" in state.disabled(tmp_path)


def test_invalid_documents_remain_visible_for_repair_but_are_not_loaded(tmp_path):
    registry = SkillRegistry(tmp_path)
    path = tmp_path / ".libi/skills/broken/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: [invalid\n---\nBody", encoding="utf-8")
    management = SkillManagement(str(tmp_path))
    entry = next(s for s in management.inventory()["skills"] if s["slug"] == "broken")
    assert entry["problem"]
    assert registry.load("broken") is None
    path.write_bytes(b"\xff\xfe\x00")
    registry.reload()
    entry = next(s for s in management.inventory()["skills"] if s["slug"] == "broken")
    assert "UTF-8" in entry["problem"]
    assert registry.load("broken") is None


def test_loader_provides_resource_base_and_longer_manual(tmp_path):
    manual = "x" * 7000
    skill = SkillRegistry(tmp_path).create("release-check", description="Release", body=manual)
    buffer = SkillContextBuffer()
    context = ToolContext(cwd=str(tmp_path), config=LiBiConfig(), skill_context_buffer=buffer)
    result = asyncio.run(load_skill({"name": "release-check"}, context))
    assert not result.is_error
    loaded = buffer.drain()
    assert str(skill.path.parent.resolve()) in loaded
    assert manual in loaded


def test_import_rejects_symlinks_and_oversized_packages(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(document(), encoding="utf-8")
    (source / "large.bin").write_bytes(b"x" * (10 * 1024 * 1024))
    management = SkillManagement(str(tmp_path))
    body = {"slug": "release-check", "scope": "project", "source_path": str(source)}
    with pytest.raises(ValueError, match="10 MiB"):
        management.preview(body)
    (source / "large.bin").unlink()
    try:
        (source / "linked").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation requires privileges on this Windows host")
    with pytest.raises(ValueError, match="链接"):
        management.preview(body)
