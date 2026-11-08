from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from paicli.policy.path_guard import PathGuard, PathPolicyError, scan_files

TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".sh",
}

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "target", "__pycache__"}


@dataclass(slots=True)
class CodeSearchResult:
    path: str
    line: int
    snippet: str


class CodeIndex:
    def __init__(self, root: str | Path, db_path: str | Path | None = None):
        self.root = Path(root).resolve()
        self._guard = PathGuard(self.root)
        self.db_path = (
            Path(db_path).expanduser()
            if db_path
            else self._guard.validate(".paicli/code_index.sqlite3")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def rebuild(self, path: str | Path | None = None) -> int:
        base = self._guard.validate(path or self.root)
        files = [base] if base.is_file() else self._iter_files(base)
        with closing(self._connect()) as conn, conn:
            conn.execute("delete from code_chunks where root = ?", (str(self.root),))
            count = 0
            for file_path in files:
                rel = str(file_path.relative_to(self.root))
                try:
                    with self._guard.open_read(file_path) as source:
                        lines = source.read().decode("utf-8", errors="ignore").splitlines()
                except (OSError, ValueError):
                    continue
                for line_number, line in enumerate(lines, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    conn.execute(
                        """
                        insert into code_chunks(root, path, line, content)
                        values (?, ?, ?, ?)
                        """,
                        (str(self.root), rel, line_number, stripped),
                    )
                    count += 1
            return count

    def search(self, query: str, limit: int = 20) -> list[CodeSearchResult]:
        terms = [term.casefold() for term in query.split()]
        if not terms or limit <= 0:
            return []
        results: list[CodeSearchResult] = []
        with closing(self._connect()) as conn:

            def matches(content: str) -> bool:
                text = content.casefold()
                return all(term in text for term in terms)

            conn.create_function("matches_terms", 1, matches)
            rows = conn.execute(
                """
                select path, line, content
                from code_chunks
                where root = ? and matches_terms(content)
                order by path, line
                """,
                (str(self.root),),
            )
            for path, line, content in rows:
                try:
                    self._guard.validate(path)
                except (OSError, PathPolicyError):
                    continue
                results.append(CodeSearchResult(path, int(line), content))
                if len(results) >= limit:
                    break
        return results

    def _iter_files(self, base: Path):
        for path in scan_files(base, exclude=SKIP_DIRS):
            if path.suffix.lower() in TEXT_SUFFIXES:
                yield path

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            if conn.execute("pragma user_version").fetchone()[0] >= 1:
                return
            conn.execute("begin immediate")
            if conn.execute("pragma user_version").fetchone()[0] >= 1:
                return
            conn.execute(
                """
                create table if not exists code_chunks (
                    id integer primary key autoincrement,
                    root text not null,
                    path text not null,
                    line integer not null,
                    content text not null
                )
                """
            )
            conn.execute(
                "create index if not exists idx_code_chunks_root_path on code_chunks(root, path)"
            )
            conn.execute(
                "create index if not exists idx_code_chunks_root_content "
                "on code_chunks(root, content)"
            )
            # Existing caches may contain data read through a now-removed link.
            conn.execute("delete from code_chunks")
            conn.execute("pragma user_version = 1")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)
