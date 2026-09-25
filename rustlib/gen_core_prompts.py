#!/usr/bin/env python3
"""Промпты и данные ядра берутся из Python-исходников БЕЗ переписывания (значения модулей): дрейфа текста между Python и Rust быть не может.
Запуск: python3 rustlib/gen_core_prompts.py"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "rustlib" / "yandi_core" / "src"
JOBS = [
    ("pet/event_extraction.py", {"_EXTRACT_SYSTEM": "event_extract_system.txt", "_CHECK_SYSTEM": "event_check_system.txt"}),
    ("pet/fact_extraction.py", {"_EXTRACT_SYSTEM": "fact_extract_system.txt", "_CHECK_SYSTEM": "fact_check_system.txt", "_SUPPORT_SYSTEM": "fact_support_system.txt",
                                "_LINK_SYSTEM": "fact_link_system.txt", "_CONFLICT_SYSTEM": "fact_conflict_system.txt"}),
]


def main():
    sys.path.insert(0, str(ROOT))
    for rel, mapping in JOBS:
        mod = importlib.import_module(rel[:-3].replace("/", "."))   # значения — ровно те, что видит Python (в т.ч. склеенные из кусков)
        for var, fn in mapping.items():
            text = getattr(mod, var)
            (OUT / fn).write_text(text, encoding="utf-8")
            print(f"{rel}:{var} → {fn} ({len(text)} символов)")


if __name__ == "__main__":
    main()
