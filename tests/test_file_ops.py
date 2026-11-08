from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from paicli.policy.path_guard import PathPolicyError
from paicli.rag.code_index import CodeIndex
from paicli.tools import file_ops


def test_recursive_tools_do_not_enter_outside_directory_links(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "private.py").write_text("private_marker", encoding="utf-8")
    (workspace / "public.py").write_text("public_marker", encoding="utf-8")
    link = workspace / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("directory junctions are unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        assert "private_marker" not in file_ops.grep(str(workspace), "marker").content
        assert "private.py" not in file_ops.directory_tree(str(workspace)).content
        assert file_ops.glob_files(str(workspace), "**/*.py").content == "public.py"
        index = CodeIndex(workspace)
        assert index.rebuild() == 1
        assert not index.search("private_marker")
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_write_and_edit_share_a_file_lock(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")
    with file_ops._lock_mutation(target):
        assert file_ops.write_file(str(tmp_path), target.name, "new").is_error
        assert file_ops.edit_file(str(tmp_path), target.name, "original", "new").is_error
    assert target.read_text(encoding="utf-8") == "original"
    assert not file_ops.edit_file(str(tmp_path), target.name, "original", "new").is_error
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("offset", "limit", "expected"),
    [
        (1, 2, "1: one\n2: two\n\n[Showing lines 1-2 of 3. Use offset=3 to continue.]"),
        (2, 1, "2: two\n\n[Showing lines 2-2 of 3. Use offset=3 to continue.]"),
        (0, 1, "1: one\n\n[Showing lines 1-1 of 3. Use offset=2 to continue.]"),
        (9, 2, ""),
        (1, 0, ""),
    ],
)
def test_read_file_line_boundaries(tmp_path, offset, limit, expected):
    (tmp_path / "note.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = file_ops.read_file(str(tmp_path), "note.txt", offset=offset, limit=limit)
    assert not result.is_error
    assert result.content == expected


def test_read_file_pages_at_the_utf8_output_budget(tmp_path):
    line = "中" * 10_000
    (tmp_path / "note.txt").write_text(f"{line}\n{line}\nlast\n", encoding="utf-8")

    first = file_ops.read_file(str(tmp_path), "note.txt")
    assert first.content == f"1: {line}\n\n[Showing lines 1-1 of 3. Use offset=2 to continue.]"
    second = file_ops.read_file(str(tmp_path), "note.txt", offset=2)
    assert second.content == f"2: {line}\n3: last"


def test_read_file_can_continue_after_an_oversized_first_line(tmp_path):
    (tmp_path / "note.txt").write_text(f"{'中' * 30_000}\nlast\n", encoding="utf-8")

    result = file_ops.read_file(str(tmp_path), "note.txt")
    assert not result.is_error
    assert result.content.startswith("1: 中")
    assert "Line 1 was shortened" in result.content
    assert "Use offset=2 to continue" in result.content
    assert len(result.content.encode("utf-8")) < 51_500
    assert file_ops.read_file(str(tmp_path), "note.txt", offset=2).content == "2: last"


@pytest.mark.parametrize("path", ["missing.txt", "."])
def test_read_missing_file_or_directory_reports_error(tmp_path, path):
    for read in (file_ops.read_file, file_ops.read_file_raw):
        assert read(str(tmp_path), path).is_error


def test_read_raw_preserves_content_and_replaces_invalid_utf8(tmp_path):
    (tmp_path / "note.txt").write_bytes(b"first\n\xfflast\n")
    result = file_ops.read_file_raw(str(tmp_path), "note.txt")
    assert not result.is_error
    assert result.content == "first\n\ufffdlast\n"


def test_write_creates_parents_appends_and_reports_python_diagnostics(tmp_path):
    cwd = str(tmp_path)
    assert not file_ops.write_file(cwd, "nested/note.txt", "first").is_error
    assert not file_ops.write_file(cwd, "nested/note.txt", "second", append=True).is_error
    assert (tmp_path / "nested/note.txt").read_text() == "firstsecond"
    result = file_ops.write_file(cwd, "bad.py", "def nope(:\n")
    assert not result.is_error
    assert "SyntaxError" in result.content


@pytest.mark.parametrize("size", [5 * 1024 * 1024, 5 * 1024 * 1024 + 1])
def test_write_size_limit_is_measured_in_bytes(tmp_path, size):
    content = "x" * size
    result = file_ops.write_file(str(tmp_path), "large.txt", content)
    assert result.is_error is (size > 5 * 1024 * 1024)
    assert (tmp_path / "large.txt").exists() is (not result.is_error)


@pytest.mark.parametrize("dry_run", [False, True])
def test_edit_replaces_only_first_match_and_dry_run_preserves_file(tmp_path, dry_run):
    target = tmp_path / "note.txt"
    target.write_text("one one", encoding="utf-8")
    result = file_ops.edit_file(str(tmp_path), "note.txt", "one", "two", dry_run=dry_run)
    assert not result.is_error
    assert target.read_text() == ("one one" if dry_run else "two one")
    assert "-one" in result.content and "+two" in result.content


def test_edit_missing_match_and_identical_replacement_preserve_content(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")
    assert file_ops.edit_file(str(tmp_path), "missing", "x", "y").is_error
    assert file_ops.edit_file(str(tmp_path), "note.txt", "missing", "x").is_error
    result = file_ops.edit_file(str(tmp_path), "note.txt", "original", "original")
    assert not result.is_error
    assert "no changes" in result.content
    assert target.read_text() == "original"


@pytest.mark.parametrize(
    "operation", ["read", "raw", "write", "edit", "list", "tree", "grep", "info"]
)
def test_file_operations_reject_workspace_escape(tmp_path, operation):
    cwd = str(tmp_path)
    calls = {
        "read": lambda: file_ops.read_file(cwd, "../outside"),
        "raw": lambda: file_ops.read_file_raw(cwd, "../outside"),
        "write": lambda: file_ops.write_file(cwd, "../outside", "x"),
        "edit": lambda: file_ops.edit_file(cwd, "../outside", "x", "y"),
        "list": lambda: file_ops.list_directory(cwd, ".."),
        "tree": lambda: file_ops.directory_tree(cwd, ".."),
        "grep": lambda: file_ops.grep(cwd, "x", path=".."),
        "info": lambda: file_ops.get_file_info(cwd, "../outside"),
    }
    with pytest.raises(PathPolicyError):
        calls[operation]()


def test_list_glob_tree_and_info_use_real_workspace(tmp_path):
    cwd = str(tmp_path)
    (tmp_path / "nested").mkdir()
    (tmp_path / "empty").mkdir()
    (tmp_path / "nested/code.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / "root.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/ignored.py").touch()
    listing = file_ops.list_directory(cwd, ".").content.splitlines()
    assert listing[-1] == "root.txt"
    assert "nested/" in listing
    assert file_ops.list_directory(cwd, "empty").content == "(empty directory)"
    assert file_ops.list_directory(cwd, "root.txt").is_error
    matches = file_ops.glob_files(cwd, "nested/**/*.py").content.splitlines()
    assert [Path(path).as_posix() for path in matches] == ["nested/code.py"]
    assert file_ops.glob_files(cwd, "*.missing").content == "(no matches)"
    assert "code.py" not in file_ops.directory_tree(cwd, max_depth=1).content
    tree = file_ops.directory_tree(cwd, max_depth=2).content
    assert "code.py" in tree and "node_modules" not in tree
    assert file_ops.directory_tree(cwd, "root.txt").is_error
    info = file_ops.get_file_info(cwd, "root.txt")
    assert "Type: file" in info.content and "Size: 5B" in info.content
    assert file_ops.get_file_info(cwd, "missing").is_error


@pytest.mark.parametrize("pattern", ["../*.py", "/outside/*.py"])
def test_glob_rejects_patterns_outside_workspace(tmp_path, pattern):
    assert file_ops.glob_files(str(tmp_path), pattern).is_error


def test_grep_handles_literal_regex_limits_and_ignored_paths(tmp_path):
    (tmp_path / "code.txt").write_text("a.b\naxb\na.b\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/hidden.txt").write_text("a.b", encoding="utf-8")
    cwd = str(tmp_path)
    literal = file_ops.grep(cwd, "a.b", use_regex=False).content.splitlines()
    assert len(literal) == 2
    assert literal[0] == "code.txt:1: a.b"
    assert len(file_ops.grep(cwd, "a.b", limit=1).content.splitlines()) == 1
    assert len(file_ops.grep(cwd, "a.b").content.splitlines()) == 3
    assert file_ops.grep(cwd, "[").is_error
    assert file_ops.grep(cwd, "no-such-content").content == "(no matches)"


def test_file_permission_failure_is_reported_and_preserves_original(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")
    original_open = Path.open

    def deny_write(path, mode="r", *args, **kwargs):
        if path == target and "w" in mode:
            raise PermissionError("write denied")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_write)
    result = file_ops.write_file(str(tmp_path), "note.txt", "replacement")
    assert result.is_error and "write denied" in result.content
    assert target.read_text() == "original"
