"""
tool_search.py — поиск файлов и содержимого внутри проекта.
"""
import re
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent  # yandi/

EXCLUDE_DIRS = {".git", "__pycache__", "target", "node_modules", ".venv", "venv"}


def find(pattern: str, path: str = ".", file_type: str = "any") -> list[str]:
    """Найти файлы по паттерну имени (glob)."""
    base = (PROJECT_ROOT / path).resolve()
    results = []
    for p in base.rglob(pattern):
        if any(ex in p.parts for ex in EXCLUDE_DIRS):
            continue
        if file_type == "file" and not p.is_file():
            continue
        if file_type == "dir" and not p.is_dir():
            continue
        results.append(str(p.relative_to(PROJECT_ROOT)))
    return sorted(results)


def grep(pattern: str, path: str = ".", extensions: Optional[list[str]] = None,
         max_results: int = 50) -> list[dict]:
    """Найти строки содержащие паттерн (regex)."""
    base = (PROJECT_ROOT / path).resolve()
    exts = set(extensions or [".py", ".rs", ".js", ".md", ".json", ".sh", ".yaml", ".toml"])
    results = []
    rx = re.compile(pattern, re.IGNORECASE)
    for f in base.rglob("*"):
        if any(ex in f.parts for ex in EXCLUDE_DIRS):
            continue
        if not f.is_file():
            continue
        if f.suffix not in exts:
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    results.append({
                        "file": str(f.relative_to(PROJECT_ROOT)),
                        "line": i,
                        "text": line.strip()[:200],
                    })
                    if len(results) >= max_results:
                        return results
        except Exception:
            continue
    return results


def file_tree(path: str = ".", max_depth: int = 3) -> list[str]:
    """Дерево файлов до заданной глубины."""
    base = (PROJECT_ROOT / path).resolve()
    results = []

    def _walk(p: Path, depth: int):
        if depth > max_depth:
            return
        for child in sorted(p.iterdir()):
            if child.name in EXCLUDE_DIRS:
                continue
            rel = str(child.relative_to(PROJECT_ROOT))
            prefix = "  " * (depth - 1)
            results.append(f"{prefix}{'📁' if child.is_dir() else '📄'} {rel}")
            if child.is_dir():
                _walk(child, depth + 1)

    _walk(base, 1)
    return results
