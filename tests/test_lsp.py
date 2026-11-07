from __future__ import annotations

from paicli.lsp import diagnose_file


def test_python_diagnostics(tmp_path):
    path = tmp_path / "bad.py"
    path.write_text("def nope(:\n", encoding="utf-8")

    diagnostics = diagnose_file(path)

    assert diagnostics
    assert "SyntaxError" in diagnostics[0]


def test_valid_python_and_non_python_files_have_no_diagnostics(tmp_path):
    valid = tmp_path / "good.py"
    valid.write_text("answer = 42\n", encoding="utf-8")
    other = tmp_path / "note.txt"
    other.write_text("def invalid(:", encoding="utf-8")
    assert diagnose_file(valid) == []
    assert diagnose_file(other) == []
