"""
llm_gateway/secure_store_regression_test.py

Проверяет: шифрование/расшифровку по кругу, обнаружение подделки
(AEAD), физическую невозможность UPDATE (SQL-триггер) даже в обход
Python API, защиту от SQL-инъекций (как во владении, так и в значениях
записи), то, что set_model_entry() никогда не переписывает существующую
запись молча, то, что ИМЯ модели не читается из файла базы без ключа,
и то, что подмена файла базы целиком более старой копией (откат)
обнаруживается при следующем открытии.

Каждый тест работает во временной директории со своими KEK/DB —
никогда не трогает реальные файлы владельца машины.

Запуск: python3 -m llm_gateway.secure_store_regression_test
"""
from __future__ import annotations

import glob
import shutil
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
        kek_path = Path(tmp) / "keys" / "kek.bin"
        db_path = Path(tmp) / "node.sqlite"
        env = {"YANDI_KEK_PATH": str(kek_path), "YANDI_NODE_DB": str(db_path)}

        with patch.dict("os.environ", env):
            check("no KEK file yet before first use", not kek_path.exists())
            check("no entry yet -> get returns None (and doesn't need a key just to find nothing)", cfg.get_model_entry("x") is None)
            check("KEK still not created by a lookup that found nothing", not kek_path.exists())
            check("no entry yet -> list is empty (and doesn't need a key)", cfg.list_models() == {})
            check("no entry yet -> remove returns False (and doesn't need a key)", cfg.remove_model_entry("x") is False)

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

            # ── FIX #1: model NAME is not readable from the raw file without the key ──
            raw = sqlite3.connect(str(db_path))
            names_column_text = " ".join(
                str(row) for row in raw.execute("SELECT name_index, name_blob FROM model_config").fetchall()
            )
            raw.close()
            check(
                "plaintext model names do not appear anywhere in the raw model_config rows",
                "my-model" not in names_column_text and evil_name not in names_column_text and "evil-value-holder" not in names_column_text,
            )

            # ── Immutability enforced at the DB level, not just the API ──
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute("UPDATE model_config SET name_index = 'hacked' WHERE name_index = name_index")
                raw.commit()
                check("direct SQL UPDATE is blocked by the trigger, even bypassing the Python API", False, "UPDATE succeeded")
            except sqlite3.DatabaseError:
                check("direct SQL UPDATE is blocked by the trigger, even bypassing the Python API", True)
            finally:
                raw.close()

            # ── Journal is append-only: no UPDATE, no DELETE, even bypassing Python ──
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute("UPDATE model_config_journal SET op = 'hacked' WHERE seq = 1")
                raw.commit()
                check("journal UPDATE is blocked by its own trigger", False, "UPDATE succeeded")
            except sqlite3.DatabaseError:
                check("journal UPDATE is blocked by its own trigger", True)
            try:
                raw.execute("DELETE FROM model_config_journal WHERE seq = 1")
                raw.commit()
                check("journal DELETE is blocked by its own trigger", False, "DELETE succeeded")
            except sqlite3.DatabaseError:
                check("journal DELETE is blocked by its own trigger", True)
            finally:
                raw.close()

            # ── Tamper detection survives even if the model_config trigger itself is bypassed ──
            raw = sqlite3.connect(str(db_path))
            raw.execute("DROP TRIGGER model_config_no_update")
            row = raw.execute("SELECT name_index, config_blob FROM model_config WHERE name_index = (SELECT name_index FROM model_config LIMIT 1)").fetchone()
            target_index, blob = row
            tampered = bytearray(blob)
            tampered[-1] ^= 0xFF
            raw.execute("UPDATE model_config SET config_blob = ? WHERE name_index = ?", (bytes(tampered), target_index))
            raw.commit()
            raw.close()
            try:
                cfg.list_models()
                check("tampering with raw ciphertext bytes is detected on read (AEAD)", False, "tampered data accepted silently")
            except store.SecureStoreError:
                check("tampering with raw ciphertext bytes is detected on read (AEAD)", True)

        # ── FIX #2: whole-file rollback is detected ──────────────────────
        with tempfile.TemporaryDirectory() as tmp2:
            kek_path2 = Path(tmp2) / "keys" / "kek.bin"
            db_path2 = Path(tmp2) / "node.sqlite"
            env2 = {"YANDI_KEK_PATH": str(kek_path2), "YANDI_NODE_DB": str(db_path2)}
            with patch.dict("os.environ", env2):
                cfg.set_model_entry("a", {"backend": "llamacpp", "path": "/a"})
                cfg.set_model_entry("b", {"backend": "llamacpp", "path": "/b"})

                # Force a checkpoint so the main .sqlite file is authoritative (no state hiding in -wal).
                conn = sqlite3.connect(str(db_path2))
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()

                snapshot_dir = Path(tmp2) / "snapshot"
                snapshot_dir.mkdir()
                for f in glob.glob(str(db_path2) + "*"):
                    shutil.copy(f, snapshot_dir / Path(f).name)

                cfg.remove_model_entry("b")
                conn = sqlite3.connect(str(db_path2))
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                check("'b' is gone after legitimate removal", cfg.get_model_entry("b") is None)

                # Attacker restores the OLD snapshot (db + wal + shm) over the current files.
                for f in snapshot_dir.iterdir():
                    shutil.copy(f, Path(tmp2) / f.name)

                try:
                    cfg.get_model_entry("a")
                    check("whole-file rollback to an older snapshot is detected", False, "rollback went undetected")
                except store.SecureStoreError as e:
                    check("whole-file rollback to an older snapshot is detected", True, str(e))

        # ── Journal-vs-table reconciliation: raw SQL bypassing the journal entirely ──
        with tempfile.TemporaryDirectory() as tmp4:
            env4 = {"YANDI_KEK_PATH": str(Path(tmp4) / "keys" / "kek.bin"), "YANDI_NODE_DB": str(Path(tmp4) / "node.sqlite")}
            with patch.dict("os.environ", env4):
                db_path4 = Path(tmp4) / "node.sqlite"
                cfg.set_model_entry("x", {"backend": "llamacpp", "path": "/x"})
                cfg.set_model_entry("y", {"backend": "llamacpp", "path": "/y"})

                # Raw DELETE straight on model_config, never touching remove_model_entry() or the journal.
                raw = sqlite3.connect(str(db_path4))
                raw.execute("DELETE FROM model_config WHERE rowid = (SELECT rowid FROM model_config LIMIT 1)")
                raw.commit()
                raw.close()
                try:
                    cfg.list_models()
                    check("raw DELETE on model_config bypassing the journal is detected", False, "silently accepted")
                except store.SecureStoreError:
                    check("raw DELETE on model_config bypassing the journal is detected", True)

        with tempfile.TemporaryDirectory() as tmp5:
            env5 = {"YANDI_KEK_PATH": str(Path(tmp5) / "keys" / "kek.bin"), "YANDI_NODE_DB": str(Path(tmp5) / "node.sqlite")}
            with patch.dict("os.environ", env5):
                db_path5 = Path(tmp5) / "node.sqlite"
                cfg.set_model_entry("x", {"backend": "llamacpp", "path": "/x"})
                raw = sqlite3.connect(str(db_path5))
                old_row = raw.execute("SELECT name_index, name_blob, config_blob, created_at_ms FROM model_config LIMIT 1").fetchone()
                raw.close()
                cfg.remove_model_entry("x")
                check("'x' legitimately removed before resurrection attempt", cfg.get_model_entry("x") is None)

                # Resurrect the deleted (still cryptographically valid) row via a raw INSERT, bypassing set_model_entry().
                raw = sqlite3.connect(str(db_path5))
                raw.execute("INSERT INTO model_config (name_index, name_blob, config_blob, created_at_ms) VALUES (?, ?, ?, ?)", old_row)
                raw.commit()
                raw.close()
                try:
                    r = cfg.get_model_entry("x")
                    check("resurrecting a deleted row via raw INSERT bypassing the journal is detected", r is None, f"got back {r}")
                except store.SecureStoreError:
                    check("resurrecting a deleted row via raw INSERT bypassing the journal is detected", True)

        # ── Third, independent temp dir: KEK is per-environment, never shared/reused implicitly ──
        with tempfile.TemporaryDirectory() as tmp3:
            env3 = {"YANDI_KEK_PATH": str(Path(tmp3) / "kek.bin"), "YANDI_NODE_DB": str(Path(tmp3) / "node.sqlite")}
            with patch.dict("os.environ", env3):
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
