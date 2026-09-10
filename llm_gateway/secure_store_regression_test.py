"""
llm_gateway/secure_store_regression_test.py

Проверяет: шифрование/расшифровку по кругу, обнаружение подделки
(AEAD), физическую невозможность UPDATE (SQL-триггер) даже в обход
Python API, защиту от SQL-инъекций (как во владении, так и в значениях
записи), и то, что set_model_entry() никогда не переписывает
существующую запись молча.

Каждый тест работает во временной директории со своими KEK/DB —
никогда не трогает реальные файлы владельца машины.

Запуск: python3 -m llm_gateway.secure_store_regression_test
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from llm_gateway import config as cfg
    from llm_gateway import secure_store as store

    with tempfile.TemporaryDirectory() as tmp:
        kek_path = Path(tmp) / "kek.bin"
        db_path = Path(tmp) / "node.sqlite"
        env = {"YANDI_KEK_PATH": str(kek_path), "YANDI_NODE_DB": str(db_path)}

        with patch.dict("os.environ", env):
            check("no KEK file yet before first use", not kek_path.exists())
            check("no entry yet -> get returns None (and doesn't need a key just to find nothing)", cfg.get_model_entry("x") is None)
            check("KEK still not created by a lookup that found nothing", not kek_path.exists())

            cfg.set_model_entry("my-model", {"backend": "llamacpp", "path": "/x/y.gguf"})
            check("KEK auto-created on first real write (needs a key to encrypt)", kek_path.exists())
            check(
                "KEK file has owner-only permissions (0600)",
                oct(kek_path.stat().st_mode)[-3:] == "600",
                oct(kek_path.stat().st_mode),
            )

            entry = cfg.get_model_entry("my-model")
            check("set entry round-trips correctly", entry == {"backend": "llamacpp", "path": "/x/y.gguf"}, repr(entry))
            check("entry is listed", "my-model" in cfg.list_models())

            # ── No modification, ever — only insert or delete ───────
            try:
                cfg.set_model_entry("my-model", {"backend": "llamacpp", "path": "/DIFFERENT.gguf"})
                check("set_model_entry() on an existing name refuses to overwrite", False, "no exception raised")
            except store.SecureStoreError:
                check("set_model_entry() on an existing name refuses to overwrite", True)
            check("original value is unchanged after the refused overwrite attempt", cfg.get_model_entry("my-model") == entry)

            check("remove_model_entry() returns True for an existing name", cfg.remove_model_entry("my-model") is True)
            check("entry is gone after removal", cfg.get_model_entry("my-model") is None)
            check("remove_model_entry() returns False for a missing name", cfg.remove_model_entry("my-model") is False)

            # After delete, re-creating under the same name is fine (delete-then-insert, not update).
            cfg.set_model_entry("my-model", {"backend": "remote", "protocol": "openai", "base_url": "https://x", "model": "m"})
            check("re-creating after explicit delete works", cfg.get_model_entry("my-model") is not None)

            # ── SQL injection safety ─────────────────────────────────
            evil_name = "x'); DROP TABLE model_config; --"
            cfg.set_model_entry(evil_name, {"backend": "llamacpp", "path": "/safe"})
            check("injection-shaped model NAME is stored as plain data, no SQL executed", cfg.get_model_entry(evil_name) is not None)
            check("table survives a DROP-TABLE-shaped name (proves parameterization)", cfg.get_model_entry("my-model") is not None)

            evil_value = {
                "backend": "remote", "base_url": "'; DROP TABLE model_config; --",
                "note": "quotes \" and apostrophes ' and unicode — просто данные",
            }
            cfg.set_model_entry("evil-value-holder", evil_value)
            check("injection-shaped VALUE round-trips as plain data", cfg.get_model_entry("evil-value-holder") == evil_value)
            check("table still intact after injection-shaped value", cfg.get_model_entry("my-model") is not None)

            # ── Immutability enforced at the DB level, not just the API ──
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute("UPDATE model_config SET model_name = 'hacked' WHERE model_name = 'my-model'")
                raw.commit()
                check("direct SQL UPDATE is blocked by the trigger, even bypassing the Python API", False, "UPDATE succeeded")
            except sqlite3.DatabaseError:
                check("direct SQL UPDATE is blocked by the trigger, even bypassing the Python API", True)
            finally:
                raw.close()

            # ── Tamper detection survives even if the trigger itself is bypassed ──
            raw = sqlite3.connect(str(db_path))
            raw.execute("DROP TRIGGER model_config_no_update")
            blob = raw.execute("SELECT config_blob FROM model_config WHERE model_name = 'my-model'").fetchone()[0]
            tampered = bytearray(blob)
            tampered[-1] ^= 0xFF
            raw.execute("UPDATE model_config SET config_blob = ? WHERE model_name = 'my-model'", (bytes(tampered),))
            raw.commit()
            raw.close()
            try:
                cfg.get_model_entry("my-model")
                check("tampering with raw ciphertext bytes is detected on read (AEAD)", False, "tampered data accepted silently")
            except store.SecureStoreError:
                check("tampering with raw ciphertext bytes is detected on read (AEAD)", True)

            # A tampered row for ONE model must not affect others (AAD binds ciphertext to its own entity_id).
            check(
                "tampering with one entry's ciphertext does not affect other entries",
                cfg.get_model_entry(evil_name) is not None and cfg.get_model_entry("evil-value-holder") == evil_value,
            )

        # ── Second, independent temp dir: KEK is per-environment, never shared/reused implicitly ──
        with tempfile.TemporaryDirectory() as tmp2:
            env2 = {"YANDI_KEK_PATH": str(Path(tmp2) / "kek.bin"), "YANDI_NODE_DB": str(Path(tmp2) / "node.sqlite")}
            with patch.dict("os.environ", env2):
                check("a fresh KEK/DB environment starts with no data", cfg.list_models() == {})

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
