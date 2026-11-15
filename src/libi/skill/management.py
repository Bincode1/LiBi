from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from libi.skill.registry import (
    SKILL_LOCK,
    Skill,
    SkillRegistry,
    _parse_frontmatter,
    _validate_skill_slug,
    atomic_write,
)

MAX_DOCUMENT = 256_000
MAX_PACKAGE = 10 * 1024 * 1024
MAX_FILES = 200
_IGNORED = {".git", "node_modules", ".venv", "__pycache__"}


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


class SkillConflict(ValueError):
    pass


def _document(value: Any, slug: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_DOCUMENT:
        raise ValueError("SKILL.md 必须是 UTF-8 文本，且不能超过 256 KB。")
    metadata = _parse_frontmatter(value)
    name = metadata.get("name") or slug
    _validate_skill_slug(name)
    if name != slug:
        raise ValueError("元数据中的 name 必须与技能目录名称一致。")
    if not metadata.get("description", "").strip():
        raise ValueError("请在 YAML 元数据中填写 description，说明何时使用此技能。")
    skill = Skill(name, metadata["description"], Path("SKILL.md"), value)
    if not skill.body:
        raise ValueError("请填写技能正文。")
    return value


def _manifest(root: Path) -> list[Path]:
    """Bounded, non-following traversal, shared by imports and resource listings."""
    files: list[Path] = []
    total = 0
    if _linked(root):
        raise ValueError("技能包不能是符号链接或目录联接。")

    def fail(error: OSError) -> None:
        raise error

    for directories, (directory, folders, names) in enumerate(
        os.walk(root, followlinks=False, onerror=fail), start=1
    ):
        if directories > MAX_FILES:
            raise ValueError("技能包目录层级或目录数量过多，最多支持 200 个目录。")
        folder = Path(directory)
        folders[:] = [name for name in folders if name not in _IGNORED]
        for name in [*folders, *names]:
            path = folder / name
            if _linked(path):
                raise ValueError("技能包不能包含符号链接或目录联接。请复制实际文件后重试。")
            path.resolve().relative_to(root.resolve())
        for name in names:
            path = folder / name
            if not path.is_file():
                raise ValueError("技能包只能包含普通文件。")
            total += path.stat().st_size
            files.append(path)
            if len(files) > MAX_FILES or total > MAX_PACKAGE:
                raise ValueError("技能包最多包含 200 个文件，总大小不能超过 10 MiB。")
    return sorted(files)


class SkillManagement:
    def __init__(self, cwd: str):
        self.cwd = Path(cwd).resolve()

    def _registry(self) -> SkillRegistry:
        return SkillRegistry(self.cwd)

    def inventory(self, *, feature_enabled: bool = True) -> dict:
        with SKILL_LOCK:
            registry = self._registry()
            warnings = []
            try:
                definitions = registry.definitions()
                overrides = registry.state_store.overrides(self.cwd)
            except ValueError as exc:
                return {
                    "skills": [],
                    "revision": None,
                    "workspace": str(self.cwd),
                    "feature_enabled": feature_enabled,
                    "warnings": [str(exc)],
                }
            effective = {s.name: s for s in definitions if not s.problem}
            rows = []
            revision = hashlib.sha256()
            state_path = registry.state_store.path
            revision.update(state_path.read_bytes() if state_path.exists() else b"")
            for skill in definitions:
                slug = skill.path.parent.name
                problem = skill.problem
                resources = []
                try:
                    for path in _manifest(skill.path.parent):
                        stat = path.stat()
                        relative = path.relative_to(skill.path.parent).as_posix()
                        resources.append({"path": relative, "bytes": stat.st_size})
                        revision.update(
                            f"{skill.path}:{relative}:{stat.st_mtime_ns}:{stat.st_size}".encode()
                        )
                except (OSError, ValueError) as exc:
                    problem = problem or str(exc)
                revision.update(str(skill.path).encode())
                revision.update(skill.content.encode())
                winner = effective.get(skill.name)
                rows.append(
                    {
                        "id": f"{skill.source}:{slug}",
                        "slug": slug,
                        "name": skill.name,
                        "description": skill.description,
                        "version": skill.version,
                        "tags": skill.tags,
                        "scope": skill.source,
                        "path": str(skill.path),
                        "enabled": skill.enabled,
                        "overridden": bool(winner and winner.path != skill.path),
                        "overridden_by": winner.source
                        if winner and winner.path != skill.path
                        else None,
                        "activation_source": "project" if skill.name in overrides else "default",
                        "problem": problem,
                        "file_count": len(resources),
                        "characters": len(skill.body),
                        "modified_at": skill.path.stat().st_mtime,
                    }
                )
            return {
                "skills": rows,
                "revision": revision.hexdigest(),
                "workspace": str(self.cwd),
                "feature_enabled": feature_enabled,
                "warnings": warnings,
            }

    def _target(self, body: dict, *, writable: bool = False) -> Path:
        scope = body.get("scope")
        slug = body.get("slug")
        _validate_skill_slug(slug)
        registry = self._registry()
        if scope == "builtin" and not writable:
            path = registry.builtin_root / slug / "SKILL.md"
        elif scope in {"user", "project"}:
            root = registry.user_root if scope == "user" else registry.project_skill_root
            if _linked(root) or _linked(root.parent):
                raise ValueError("技能存储目录不能通过符号链接或目录联接访问。")
            path = registry._skill_path(slug, scope)
        else:
            raise ValueError("内置技能只读；请选择用户或项目范围。")
        # Reject linked package roots as well as linked SKILL.md files.
        if _linked(path.parent) or _linked(path):
            raise ValueError("请先将链接技能复制为实际文件后再管理。")
        return path

    def detail(self, body: dict) -> dict:
        with SKILL_LOCK:
            path = self._target(body)
            if not path.is_file():
                raise FileNotFoundError("技能已不存在，请刷新列表。")
            files = _manifest(path.parent)
            return {
                "document": path.read_text(encoding="utf-8"),
                "resources": [
                    {"path": p.relative_to(path.parent).as_posix(), "bytes": p.stat().st_size}
                    for p in files
                ],
            }

    def _import(self, body: dict) -> tuple[str, dict[str, bytes]]:
        slug = body.get("slug")
        _validate_skill_slug(slug)
        source = body.get("source_path")
        if source:
            if not isinstance(source, str) or not Path(source).expanduser().is_absolute():
                raise ValueError("请填写 Runtime 所在电脑上的技能目录绝对路径。")
            root = Path(source).expanduser()
            if _linked(root):
                raise ValueError("导入目录不能是链接。")
            root = root.resolve()
            if not (root / "SKILL.md").is_file():
                raise ValueError("目录中未找到 SKILL.md，请选择单个技能的根目录。")
            files = {p.relative_to(root).as_posix(): p.read_bytes() for p in _manifest(root)}
            try:
                document = files["SKILL.md"].decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("SKILL.md 必须使用 UTF-8 编码。") from exc
        else:
            document = body.get("document")
            files = {}
        document = _document(document, slug)
        files["SKILL.md"] = document.encode("utf-8")
        digest = hashlib.sha256()
        for name, content in sorted(files.items()):
            digest.update(name.encode())
            digest.update(content)
        return digest.hexdigest(), files

    def preview(self, body: dict) -> dict:
        with SKILL_LOCK:
            target = self._target(body, writable=True)
            fingerprint, files = self._import(body)
            metadata = _parse_frontmatter(files["SKILL.md"].decode())
            return {
                "fingerprint": fingerprint,
                "name": metadata.get("name", body["slug"]),
                "description": metadata["description"],
                "conflict": target.parent.exists(),
                "files": [{"path": name, "bytes": len(data)} for name, data in files.items()],
            }

    def mutate(self, action: str, body: dict) -> None:
        with SKILL_LOCK:
            revision = self.inventory()["revision"]
            if not revision or body.get("revision") != revision:
                raise SkillConflict("技能已发生变化。请刷新列表后重试；编辑内容仍可保留。")
            path = self._target(body, writable=action not in {"toggle", "reset"})
            registry = self._registry()
            if action in {"toggle", "reset"}:
                skill = (
                    registry._load_skill_file(
                        path, body["scope"], registry.state_store.disabled(self.cwd)
                    )
                    if path.is_file()
                    else None
                )
                if not skill:
                    raise FileNotFoundError("技能已不存在，请刷新列表。")
                if action == "toggle" and type(body.get("enabled")) is not bool:
                    raise ValueError("enabled 必须是布尔值。")
                registry.state_store.set_project(
                    self.cwd, skill.name, body.get("enabled") if action == "toggle" else None
                )
            elif action == "remove":
                if not path.is_file():
                    raise FileNotFoundError("技能已不存在，请刷新列表。")
                _manifest(path.parent)
                # _target verifies the resolved path remains within the selected scope root.
                shutil.rmtree(path.parent)
            elif action == "edit":
                if not path.is_file():
                    raise FileNotFoundError("技能已不存在，请刷新列表。")
                atomic_write(path, _document(body.get("document"), body["slug"]))
            elif action in {"add", "import"}:
                if path.parent.exists():
                    raise SkillConflict("此范围内已有同名技能。请选择编辑，或更换名称。")
                if action == "import":
                    fingerprint, files = self._import(body)
                    if body.get("fingerprint") != fingerprint:
                        raise SkillConflict("导入源已变化，请重新预览后导入。")
                else:
                    files = {"SKILL.md": _document(body.get("document"), body["slug"]).encode()}
                path.parent.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".import-", dir=path.parent.parent) as tmp:
                    staging = Path(tmp) / body["slug"]
                    staging.mkdir()
                    for name, data in files.items():
                        destination = staging / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
                    staging.rename(path.parent)
            else:
                raise ValueError("不支持的技能操作。")
