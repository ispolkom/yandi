#!/usr/bin/env python3
"""
assistant/ast_intelligence.py — AST Code Intelligence для PET-проекта.

Строит граф импортов и вызовов функций по всем .py файлам проекта.
Позволяет анализировать влияние изменений в файле ("что сломается").

Команды CLI:
  python3 assistant/ast_intelligence.py index          — проиндексировать проект
  python3 assistant/ast_intelligence.py impact <file>  — что зависит от файла
  python3 assistant/ast_intelligence.py callers <func> — кто вызывает функцию
  python3 assistant/ast_intelligence.py stats          — статистика графа
  python3 assistant/ast_intelligence.py deps <file>    — от чего зависит файл
"""

from __future__ import annotations

import ast
import json
import sys
import time
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent.parent
INDEX_FILE = BASE / "registry" / "ast" / "index.json"

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             "adapters", "sft", "runs", "embeddings_cache"}


# ── AST helpers ───────────────────────────────────────────────────────────────

def _module_name(path: Path) -> str:
    """Relative dotted module name from project root."""
    rel = path.relative_to(BASE)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]  # strip .py
    return ".".join(parts)


def _resolve_import(module: str, from_file: Path) -> Optional[str]:
    """Try to resolve a dotted module name to an actual .py path."""
    parts = module.split(".")
    candidates = [
        BASE / Path(*parts).with_suffix(".py"),
        BASE / Path(*parts) / "__init__.py",
    ]
    for c in candidates:
        if c.exists():
            return _module_name(c)
    return None


def _parse_file(path: Path) -> dict:
    """Parse one Python file → {imports, calls, defines, classes}."""
    result = {
        "module": _module_name(path),
        "imports": [],
        "calls": [],
        "defines": [],
        "classes": [],
    }
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return result

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import,)):
            for alias in node.names:
                result["imports"].append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                base_mod = node.module
                result["imports"].append(base_mod)
                for alias in node.names:
                    if alias.name != "*":
                        result["imports"].append(f"{base_mod}.{alias.name}")

        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            result["defines"].append(node.name)

        elif isinstance(node, ast.ClassDef):
            result["classes"].append(node.name)

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                result["calls"].append(func.id)
            elif isinstance(func, ast.Attribute):
                result["calls"].append(func.attr)

    return result


# ── Indexer ───────────────────────────────────────────────────────────────────

class ASTIndex:
    def __init__(self):
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = {}
        if INDEX_FILE.exists():
            try:
                self.data = json.loads(INDEX_FILE.read_text())
            except Exception:
                self.data = {}

    # ── build ─────────────────────────────────────────────────────────────────

    def build(self, verbose: bool = False) -> dict:
        """Index all .py files in the project."""
        t0 = time.time()
        modules: dict[str, dict] = {}

        for py in BASE.rglob("*.py"):
            if any(s in py.parts for s in SKIP_DIRS):
                continue
            info = _parse_file(py)
            modules[info["module"]] = info

        # Resolve imports → module-level edges
        import_graph: dict[str, list[str]] = {}
        reverse_graph: dict[str, list[str]] = {}

        for mod, info in modules.items():
            resolved = []
            for imp in set(info["imports"]):
                r = _resolve_import(imp, BASE / Path(*mod.split(".")).with_suffix(".py"))
                if r and r in modules and r != mod:
                    resolved.append(r)
            import_graph[mod] = sorted(set(resolved))
            for dep in resolved:
                reverse_graph.setdefault(dep, [])
                if mod not in reverse_graph[dep]:
                    reverse_graph[dep].append(mod)

        self.data = {
            "indexed_at": time.time(),
            "total_modules": len(modules),
            "modules": modules,
            "import_graph": import_graph,      # mod → what it imports
            "reverse_graph": reverse_graph,    # mod → who imports it
        }
        INDEX_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        elapsed = time.time() - t0
        if verbose:
            print(f"  ✓ AST index: {len(modules)} модулей за {elapsed:.1f}s")
            print(f"    edges: {sum(len(v) for v in import_graph.values())}")
        return {"modules": len(modules), "elapsed": round(elapsed, 2)}

    # ── impact analysis ───────────────────────────────────────────────────────

    def impact(self, file_or_module: str, depth: int = 3) -> dict:
        """What modules break if this file/module changes (BFS on reverse_graph)."""
        mod = self._to_module(file_or_module)
        if not mod:
            return {"error": f"модуль не найден: {file_or_module}"}

        rg = self.data.get("reverse_graph", {})
        visited: set[str] = set()
        queue = [mod]
        layers: list[list[str]] = []

        for _ in range(depth):
            next_q = []
            for m in queue:
                for dep in rg.get(m, []):
                    if dep not in visited and dep != mod:
                        visited.add(dep)
                        next_q.append(dep)
            if not next_q:
                break
            layers.append(next_q)
            queue = next_q

        return {
            "module": mod,
            "depth": depth,
            "affected": list(visited),
            "layers": layers,
        }

    # ── reverse: dependencies of a file ──────────────────────────────────────

    def deps(self, file_or_module: str) -> dict:
        """What this module imports (dependencies)."""
        mod = self._to_module(file_or_module)
        if not mod:
            return {"error": f"модуль не найден: {file_or_module}"}
        ig = self.data.get("import_graph", {})
        return {"module": mod, "imports": ig.get(mod, [])}

    # ── who calls a function ──────────────────────────────────────────────────

    def callers(self, func_name: str) -> dict:
        """Which modules call a function by name."""
        result = []
        for mod, info in self.data.get("modules", {}).items():
            if func_name in info.get("calls", []):
                result.append(mod)
        return {"function": func_name, "callers": result}

    # ── where is a symbol defined ─────────────────────────────────────────────

    def find_definition(self, name: str) -> list[dict]:
        result = []
        for mod, info in self.data.get("modules", {}).items():
            if name in info.get("defines", []):
                result.append({"module": mod, "type": "function"})
            if name in info.get("classes", []):
                result.append({"module": mod, "type": "class"})
        return result

    # ── stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        mods = self.data.get("modules", {})
        ig = self.data.get("import_graph", {})
        rg = self.data.get("reverse_graph", {})
        # Most imported modules (by reverse_graph size)
        hotspots = sorted(
            [(m, len(v)) for m, v in rg.items()],
            key=lambda x: x[1], reverse=True
        )[:10]
        return {
            "total_modules": len(mods),
            "total_edges": sum(len(v) for v in ig.values()),
            "indexed_at": self.data.get("indexed_at", 0),
            "most_imported": [{"module": m, "importers": n} for m, n in hotspots],
        }

    # ── helpers ───────────────────────────────────────────────────────────────

    def _to_module(self, s: str) -> Optional[str]:
        """Convert file path or dotted module name to canonical module key."""
        s = s.strip()
        if s.endswith(".py"):
            try:
                p = Path(s)
                if not p.is_absolute():
                    p = BASE / p
                return _module_name(p)
            except Exception:
                return None
        return s if s in self.data.get("modules", {}) else None


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    idx = ASTIndex()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "index":
        result = idx.build(verbose=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "impact":
        result = idx.impact(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "deps":
        result = idx.deps(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "callers":
        result = idx.callers(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "find":
        result = idx.find_definition(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "stats":
        result = idx.stats()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: ast_intelligence.py index|stats|impact <file>|deps <file>|callers <fn>|find <name>")


if __name__ == "__main__":
    main()
