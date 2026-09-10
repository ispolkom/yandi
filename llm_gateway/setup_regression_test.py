"""
llm_gateway/setup_regression_test.py

Проверяет только чистую логику (поиск файлов, сборка записи конфига)
— не сам интерактивный ввод (input()), тот проверяется вручную при
живом запуске python3 -m llm_gateway.setup.

Запуск: python3 -m llm_gateway.setup_regression_test
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from llm_gateway import setup as su

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        check("empty directory -> no files found", su.discover_gguf_files(base) == [])
        check("nonexistent directory -> no crash, empty list", su.discover_gguf_files(base / "nope") == [])

        (base / "model-a.gguf").write_bytes(b"x")
        (base / "not-a-model.txt").write_bytes(b"x")
        sub = base / "subdir"
        sub.mkdir()
        (sub / "model-b.gguf").write_bytes(b"x")
        (sub / "deeper").mkdir()
        (sub / "deeper" / "model-c.gguf").write_bytes(b"x")

        found = su.discover_gguf_files(base)
        found_names = sorted(f.name for f in found)
        check("finds .gguf in the given directory", "model-a.gguf" in found_names, repr(found_names))
        check("finds .gguf one level down", "model-b.gguf" in found_names, repr(found_names))
        check("does not pick up non-.gguf files", "not-a-model.txt" not in found_names, repr(found_names))
        check("does not recurse more than one level deep", "model-c.gguf" not in found_names, repr(found_names))

    entry = su.build_local_entry(Path("/some/model.gguf"))
    check("local entry has backend=llamacpp", entry["backend"] == "llamacpp", repr(entry))
    check("local entry carries the path as a string", entry["path"] == "/some/model.gguf", repr(entry))
    check("local entry has sane n_ctx/n_gpu_layers defaults", entry["n_ctx"] == 8192 and entry["n_gpu_layers"] == -1, repr(entry))

    remote_entry = su.build_remote_entry("anthropic", "https://api.anthropic.com", "claude-sonnet-5", "MY_KEY")
    check(
        "remote entry has all required fields, no actual key value",
        remote_entry == {
            "backend": "remote", "protocol": "anthropic",
            "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5",
            "api_key_env": "MY_KEY",
        },
        repr(remote_entry),
    )
    check("remote entry never contains a literal key-shaped string", "sk-" not in str(remote_entry) and "ant-" not in str(remote_entry))

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
