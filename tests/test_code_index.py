from __future__ import annotations

from pathlib import Path

import pytest

from paicli.rag.code_index import CodeIndex


def test_code_index_rebuild_search_and_refresh(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def Alpha():\n\n    return 'needle'\n", encoding="utf-8")
    index = CodeIndex(tmp_path)
    assert index.rebuild() == 2
    result = index.search("RETURN needle")[0]
    assert (result.path, result.line, result.snippet) == ("app.py", 3, "return 'needle'")
    assert index.search(" \n ") == []
    assert index.search("not-present") == []
    assert len(index.search("a", limit=1)) == 1
    target.write_text("new_content = 1\n", encoding="utf-8")
    assert index.rebuild() == 1
    assert index.search("needle") == []
    assert index.search("new_content")[0].line == 1


def test_code_index_ignores_builds_binaries_and_blank_lines(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.ts").write_text("const needle = 1;\n\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"needle")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/ignored.py").write_text("needle", encoding="utf-8")
    index = CodeIndex(tmp_path)
    assert index.rebuild() == 1
    assert Path(index.search("needle")[0].path).as_posix() == "src/app.ts"


def test_code_index_roots_share_database_without_leaking_results(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "a.py").write_text("needle_a", encoding="utf-8")
    (b / "b.py").write_text("needle_b", encoding="utf-8")
    first, second = CodeIndex(a, tmp_path / "index.db"), CodeIndex(b, tmp_path / "index.db")
    first.rebuild()
    second.rebuild()
    assert [result.path for result in first.search("needle")] == ["a.py"]
    assert [result.path for result in second.search("needle")] == ["b.py"]
    with pytest.raises(ValueError):
        first.rebuild("../b")
    assert first.search("needle")[0].path == "a.py"
