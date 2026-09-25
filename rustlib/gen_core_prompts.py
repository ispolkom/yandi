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
    ("pet/commitment_verification.py", {"_CLASSIFY_SYSTEM": "commit_classify_system.txt", "_VERIFY_SYSTEM": "commit_verify_system.txt", "_DELIVERS_SYSTEM": "commit_delivers_system.txt"}),
    ("pet/fact_extraction.py", {"_EXTRACT_SYSTEM": "fact_extract_system.txt", "_CHECK_SYSTEM": "fact_check_system.txt", "_SUPPORT_SYSTEM": "fact_support_system.txt",
                                "_LINK_SYSTEM": "fact_link_system.txt", "_CONFLICT_SYSTEM": "fact_conflict_system.txt"}),
]


DATA_JOBS = [
    # (модуль, {имя_в_модуле: файл}) — значения любого типа сохраняются как JSON
    ("pet.chat_local", {"_BASE_CHARACTER_PROMPT": "chat_character_prompt.txt"}),
]


def main():
    import json
    sys.path.insert(0, str(ROOT))
    for modname, mapping in DATA_JOBS:
        mod = importlib.import_module(modname)
        for var, fn in mapping.items():
            (OUT / fn).write_text(getattr(mod, var), encoding="utf-8")
            print(f"{modname}:{var} → {fn}")
    cl = importlib.import_module("pet.chat_local")
    (OUT / "chat_tokens.json").write_text(json.dumps({"stop": cl._STOP_TOKENS, "cleanup": list(cl._CLEANUP_TOKENS), "failure_reply": cl._SEMANTIC_FAILURE_REPLY}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("pet.chat_local: токены → chat_tokens.json")
    sys.path.insert(0, str(ROOT))
    for rel, mapping in JOBS:
        mod = importlib.import_module(rel[:-3].replace("/", "."))   # значения — ровно те, что видит Python (в т.ч. склеенные из кусков)
        for var, fn in mapping.items():
            text = getattr(mod, var)
            (OUT / fn).write_text(text, encoding="utf-8")
            print(f"{rel}:{var} → {fn} ({len(text)} символов)")


if __name__ == "__main__":
    main()
