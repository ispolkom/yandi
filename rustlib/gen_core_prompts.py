#!/usr/bin/env python3
"""Промпты и данные ядра берутся из Python-исходников БЕЗ переписывания (разбор AST): дрейфа текста между Python и Rust быть не может.
Запуск: python3 rustlib/gen_core_prompts.py"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "rustlib" / "yandi_core" / "src"
JOBS = [
    ("pet/event_extraction.py", {"_EXTRACT_SYSTEM": "event_extract_system.txt", "_CHECK_SYSTEM": "event_check_system.txt"}),
]


def main():
    for rel, mapping in JOBS:
        mod = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        found = {}
        for n in mod.body:
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in mapping:
                found[n.targets[0].id] = ast.literal_eval(n.value)
        for var, fn in mapping.items():
            (OUT / fn).write_text(found[var], encoding="utf-8")
            print(f"{rel}:{var} → {fn} ({len(found[var])} символов)")


if __name__ == "__main__":
    main()
