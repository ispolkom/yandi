"""
llm_gateway.secure_store — зашифрованное, вставить-или-стереть (никогда
не изменить) локальное хранилище настроек модели узла.

Переиспользует уже спроектированную и протестированную криптографию
проекта как есть (agent/db/sql/keys.py + crypto.py — AES-256-GCM,
ключевая иерархия, дисциплина "никогда не логировать байты ключа") —
просто нацеленную на локальный SQLite конкретного узла, а не на общую
MySQL-память агента (которая на этой машине ни разу не поднималась
по-настоящему, см. agent/db/sql/SECURITY_ARCHITECTURE.md §2/§4).

Гарантии:
- KEK создаётся кодом (CSPRNG), один раз, автоматически — ни владелец
  узла, ни кто-либо ещё его не выбирает и не вводит.
- Файл KEK нельзя перезаписать поверх существующего (save_kek() уже
  на это рассчитан) — при потере файла восстановить прежние данные
  невозможно, можно только стереть базу и начать заново (см.
  llm_gateway/harden_key.sh для дополнительного OS-уровня усиления,
  требующего root один раз, который я сам выполнить не могу).
- Каждая запись в базе — один AES-256-GCM блок с собственной AAD,
  привязанной к имени модели: подмена/правка байтов ломает
  расшифровку, а не тихо принимается.
- Таблица физически не поддерживает UPDATE (SQL-триггер) — только
  INSERT и DELETE. "Изменить" не существует как операция.
- Все запросы параметризованы (`?`), ни один SQL никогда не собирается
  конкатенацией строк — стандартная защита от инъекций.
- Ничто в этом модуле не печатает и не логирует ни KEK, ни производный
  ключ, ни расшифрованное содержимое — та же дисциплина, что уже
  проверяется статическим grep для agent/db/sql/keys.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from agent.db.sql import keys as _keys
from agent.db.sql import crypto as _crypto

DEFAULT_KEK_DIR = Path.home() / ".local" / "share" / "yandi" / "keys"
DEFAULT_KEK_PATH = DEFAULT_KEK_DIR / "node_kek.bin"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "yandi" / "node_config.sqlite"

_ENTITY_TYPE = "node_model_config"


class SecureStoreError(RuntimeError):
    """Хранилище недоступно, повреждено или обнаружена подделка данных."""


def kek_path() -> Path:
    override = os.environ.get(_keys.KEK_PATH_ENV)
    return Path(override) if override else DEFAULT_KEK_PATH


def db_path() -> Path:
    override = os.environ.get("YANDI_NODE_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def _ensure_kek() -> bytes:
    """Загружает KEK, если он уже есть; если нет — создаёт сам, один
    раз, без участия человека (только байты создаются автоматически;
    усиление прав до полной неизменяемости — отдельный, требующий root
    шаг, см. harden_key.sh, не выполняется этой функцией)."""
    path = str(kek_path())
    try:
        return _keys.load_kek(path)
    except _keys.KeyMissingError:
        pass

    kek = _keys.generate_kek()
    _keys.save_kek(path, kek)
    print(
        f"[llm_gateway] Создан новый ключ узла: {path}\n"
        f"[llm_gateway] Это единственная копия. Потеря файла = потеря доступа ко "
        f"всем сохранённым настройкам модели (не к самим моделям — только к записи "
        f"о том, где их искать). Резервная копия — на усмотрение владельца узла.\n"
        f"[llm_gateway] Рекомендуется дополнительно закрепить права на файл "
        f"(см. llm_gateway/harden_key.sh, требует root один раз)."
    )
    return kek


def _node_config_key() -> bytes:
    return _keys.derive_node_config_key(_ensure_kek())


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_config (
            model_name TEXT PRIMARY KEY,
            config_blob BLOB NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS model_config_no_update
        BEFORE UPDATE ON model_config
        BEGIN
            SELECT RAISE(ABORT, 'model_config is insert/delete-only — rows cannot be modified in place');
        END
        """
    )
    return conn


def get_model_entry(model: str) -> dict[str, Any] | None:
    """Настройка узла для этого имени модели, если есть. Расшифровка
    проверяет подлинность (AEAD-тег) — подменённые байты дают ошибку,
    а не тихо принятые поддельные данные."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT config_blob FROM model_config WHERE model_name = ?", (model,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    key = _node_config_key()
    try:
        plaintext = _crypto.decrypt_field(
            key, row[0], entity_type=_ENTITY_TYPE, entity_id=model, field_name="config"
        )
    except Exception as e:
        raise SecureStoreError(
            f"запись для {model!r} повреждена или подделана (расшифровка не прошла): {e}"
        ) from e
    return json.loads(plaintext)


def set_model_entry(model: str, entry: dict[str, Any]) -> None:
    """Вставляет НОВУЮ запись. Если запись с этим именем уже есть —
    сначала явно удали (remove_model_entry), это не INSERT OR REPLACE:
    "изменить" не должно быть доступно даже в одну функцию."""
    key = _node_config_key()
    blob = _crypto.encrypt_field(
        key, json.dumps(entry, ensure_ascii=False),
        entity_type=_ENTITY_TYPE, entity_id=model, field_name="config",
    )
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO model_config (model_name, config_blob, created_at_ms) VALUES (?, ?, ?)",
            (model, blob, int(time.time() * 1000)),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise SecureStoreError(
            f"запись для {model!r} уже существует — сначала удали её явно, "
            f"перезапись на месте запрещена"
        ) from e
    finally:
        conn.close()


def remove_model_entry(model: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM model_config WHERE model_name = ?", (model,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_models() -> dict[str, Any]:
    conn = _connect()
    try:
        names = [r[0] for r in conn.execute("SELECT model_name FROM model_config").fetchall()]
    finally:
        conn.close()
    result = {}
    for name in names:
        entry = get_model_entry(name)
        if entry is not None:
            result[name] = entry
    return result
