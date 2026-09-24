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
  привязанной к слепому индексу имени модели: подмена/правка байтов
  ломает расшифровку, а не тихо принимается.
- Таблица физически не поддерживает UPDATE (SQL-триггер) — только
  INSERT и DELETE. "Изменить" не существует как операция.
- ИМЯ модели тоже зашифровано (не только сама настройка) — в базе
  вместо человекочитаемого имени лежит слепой индекс (HMAC от имени)
  для поиска и зашифрованная копия для отображения. Тот, у кого есть
  только файл базы, но нет ключа, не узнает даже СПИСОК настроенных
  моделей/сервисов — только количество непрозрачных записей.
- Каждая операция (вставка/удаление) добавляется в отдельный
  цепочечный журнал (append-only, тоже без UPDATE и без DELETE), где
  каждая запись содержит HMAC от себя и от предыдущей записи —
  подмена, удаление или переупорядочивание прошлых событий рвёт
  цепочку. Последняя проверенная точка цепочки дополнительно
  сохраняется в ОТДЕЛЬНОМ файле рядом с ключом — если кто-то подменит
  файл базы целиком более старой копией (например простым `cp`), при
  следующем открытии это будет обнаружено, потому что копия базы
  отстанет от контрольной точки. (Честный предел: если атакующий
  подменит ОБА файла синхронно — базу и контрольную точку — это уже
  не отличить от легитимного отката; полная защита от такого требует
  внешнего, вне этой машины, свидетеля — например сети узлов — это
  отдельная будущая работа, не решается одним локальным файлом.)
- Журнал сверяется не только сам с собой, но и с ФАКТИЧЕСКИМ
  содержимым таблицы настроек: строка, вставленная или удалённая
  напрямую через SQL в обход set_model_entry()/remove_model_entry()
  (а значит, в обход журнала), обнаруживается как расхождение при
  следующем открытии — до того, как что-либо будет расшифровано и
  отдано наружу. (Тот же честный предел: атакующий с самим ключом
  шифрования может вычислить корректный HMAC и обойти это — журнал
  защищает от подмены данных без ключа, не от владельца ключа.)
- Все запросы параметризованы (`?`), ни один SQL никогда не собирается
  конкатенацией строк — стандартная защита от инъекций.
- Ничто в этом модуле не печатает и не логирует ни KEK, ни производный
  ключ, ни расшифрованное содержимое — та же дисциплина, что уже
  проверяется статическим grep для agent/db/sql/keys.py.
"""
from __future__ import annotations

import hashlib
import hmac
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
_NAME_NAMESPACE = "node-model-name:v1"
_GENESIS_HASH = b"\x00" * 32


class SecureStoreError(RuntimeError):
    """Хранилище недоступно, повреждено, обнаружена подделка данных, или
    обнаружен откат файла базы к более старому состоянию."""


def kek_path() -> Path:
    override = os.environ.get(_keys.KEK_PATH_ENV)
    return Path(override) if override else DEFAULT_KEK_PATH


def db_path() -> Path:
    override = os.environ.get("YANDI_NODE_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def chain_tip_path() -> Path:
    """Живёт РЯДОМ с ключом, НЕ внутри файла базы — специально, чтобы
    подмена одного файла базы (например простым cp старой копии поверх
    текущей) не подменяла и эту контрольную точку заодно."""
    return kek_path().parent / "node_config_chain_tip.json"


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


def _load_kek_for_read() -> bytes:
    """Как _ensure_kek(), но НИКОГДА не создаёт новый ключ — если история
    уже не пуста, а ключа нет, это либо потеря ключа, либо попытка
    обхода проверки (удалить ключ, чтобы код молча сгенерировал новый и
    "не заметил" несоответствие). В обоих случаях — явная ошибка, не
    тихая деградация."""
    try:
        return _keys.load_kek(str(kek_path()))
    except _keys.KeyMissingError as e:
        raise SecureStoreError(
            "история конфигурации не пуста, но ключ узла отсутствует — либо ключ "
            "потерян, либо файл ключа был удалён намеренно. Восстановление данных "
            "без ключа невозможно."
        ) from e


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")

    # Schema drift guard: an older version of this module stored
    # model_config with a plaintext `model_name` primary key and no blind
    # index (before the blind-index + hash-chained journal redesign).
    # `CREATE TABLE IF NOT EXISTS` below never upgrades an existing table
    # with the old layout — a node whose database predates the redesign
    # would otherwise hit a confusing "no such column: name_index" deep
    # inside get_model_entry/set_model_entry instead of a clear error
    # here. An empty old-schema table is safe to drop and recreate; one
    # that already holds rows is left alone and fails loud instead —
    # migrating real encrypted data (under whatever old key scheme
    # produced it) is a deliberate, owner-approved operation, not
    # something to do silently on every connect.
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(model_config)").fetchall()
    }
    if existing_cols and "name_index" not in existing_cols:
        row_count = conn.execute("SELECT COUNT(*) FROM model_config").fetchone()[0]
        if row_count > 0:
            raise SecureStoreError(
                f"{path} has an old model_config schema (pre blind-index/journal "
                f"redesign) with {row_count} existing row(s) — refusing to touch it "
                f"automatically. This needs a deliberate, owner-approved migration, "
                f"not a silent drop."
            )
        conn.execute("DROP TABLE model_config")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_config (
            name_index TEXT PRIMARY KEY,
            name_blob BLOB NOT NULL,
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_config_journal (
            seq INTEGER PRIMARY KEY,
            op TEXT NOT NULL,
            name_index TEXT NOT NULL,
            content_hash BLOB NOT NULL,
            entry_hash BLOB NOT NULL,
            created_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS model_config_journal_no_update
        BEFORE UPDATE ON model_config_journal
        BEGIN
            SELECT RAISE(ABORT, 'model_config_journal is append-only — no update');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS model_config_journal_no_delete
        BEFORE DELETE ON model_config_journal
        BEGIN
            SELECT RAISE(ABORT, 'model_config_journal is append-only — no delete');
        END
        """
    )
    return conn


def _is_genuinely_empty(conn: sqlite3.Connection) -> bool:
    """True only if the journal, the live table, AND the external
    checkpoint are all empty/absent at once (a real fresh store — safe
    to answer "nothing here" without touching the key). Any other
    combination is an inconsistency between the three views of state
    and is a detected error, never a silent "nothing to see" shortcut:
    - journal empty, live table NOT empty -> a row was inserted straight
      into model_config bypassing set_model_entry() (journal skipped).
    - both tables empty, but a checkpoint file already exists -> the
      whole database was wiped and is being presented as "first ever
      use" to dodge the rollback check the checkpoint exists to do."""
    has_journal = conn.execute("SELECT 1 FROM model_config_journal LIMIT 1").fetchone() is not None
    has_live_rows = conn.execute("SELECT 1 FROM model_config LIMIT 1").fetchone() is not None
    if not has_journal and has_live_rows:
        raise SecureStoreError(
            "несоответствие: журнал истории пуст, а таблица настроек — нет. "
            "Похоже, запись была добавлена в обход журнала (set_model_entry() не вызывался)."
        )
    if not has_journal and not has_live_rows:
        if _read_chain_tip() is not None:
            raise SecureStoreError(
                "ОБНАРУЖЕН ОТКАТ: журнал и таблица настроек пусты, но файл контрольной "
                "точки утверждает, что история уже существовала — база была полностью "
                "стёрта и выдаётся за первую установку, чтобы обойти проверку отката."
            )
        return True
    return False


def _entry_hash(integrity_key: bytes, seq: int, op: str, name_index: str, content_hash: bytes, prev_hash: bytes) -> bytes:
    # Rust-перенос (2026-09-24): rustlib/yandi_rs/src/crypto.rs::entry_hash (YANDI_CRYPTO_ENGINE=rust, тот же переключатель, что у crypto.py).
    rs = _crypto._get_rust_cr()
    if (rs is not None and type(integrity_key) is bytes and type(seq) is int and 0 <= seq < 1 << 64 and type(op) is str
            and type(name_index) is str and type(content_hash) is bytes and type(prev_hash) is bytes):
        try:
            return rs.entry_hash(integrity_key, seq, op, name_index, content_hash, prev_hash)
        except UnicodeEncodeError:
            pass        # одинокий суррогат — исходный код ниже (там UnicodeEncodeError тоже)
    msg = (
        seq.to_bytes(8, "big") + b"|" + op.encode("utf-8") + b"|"
        + name_index.encode("utf-8") + b"|" + content_hash + b"|" + prev_hash
    )
    return hmac.new(integrity_key, msg, hashlib.sha256).digest()


def _append_journal(conn: sqlite3.Connection, integrity_key: bytes, op: str, name_index: str, content_bytes: bytes) -> tuple[int, bytes]:
    last = conn.execute("SELECT seq, entry_hash FROM model_config_journal ORDER BY seq DESC LIMIT 1").fetchone()
    prev_seq, prev_hash = (last[0], last[1]) if last else (0, _GENESIS_HASH)
    seq = prev_seq + 1
    content_hash = hashlib.sha256(content_bytes).digest()
    entry_hash = _entry_hash(integrity_key, seq, op, name_index, content_hash, prev_hash)
    conn.execute(
        "INSERT INTO model_config_journal (seq, op, name_index, content_hash, entry_hash, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (seq, op, name_index, content_hash, entry_hash, int(time.time() * 1000)),
    )
    return seq, entry_hash


def _read_chain_tip() -> tuple[int, bytes] | None:
    path = chain_tip_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data["seq"], bytes.fromhex(data["hash"])


def _write_chain_tip(seq: int, entry_hash: bytes) -> None:
    path = chain_tip_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"seq": seq, "hash": entry_hash.hex()}))
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _verify_chain(conn: sqlite3.Connection, integrity_key: bytes) -> None:
    """Пересчитывает всю цепочку журнала и сверяет её с последней
    известной контрольной точкой (лежащей в ОТДЕЛЬНОМ файле рядом с
    ключом). Бросает SecureStoreError при любом расхождении: подделанной
    записи, разрыве цепочки, или файле базы, откатанном назад."""
    rows = conn.execute(
        "SELECT seq, op, name_index, content_hash, entry_hash FROM model_config_journal ORDER BY seq ASC"
    ).fetchall()

    prev_hash = _GENESIS_HASH
    expected_live: dict[str, bytes] = {}
    for seq, op, name_index, content_hash, entry_hash in rows:
        expected = _entry_hash(integrity_key, seq, op, name_index, content_hash, prev_hash)
        if not hmac.compare_digest(expected, entry_hash):
            raise SecureStoreError(f"цепочка истории конфигурации повреждена или подделана на записи seq={seq}")
        prev_hash = entry_hash
        if op == "insert":
            expected_live[name_index] = content_hash
        elif op == "delete":
            expected_live.pop(name_index, None)

    if rows:
        db_seq, db_hash = rows[-1][0], rows[-1][4]
    else:
        db_seq, db_hash = 0, _GENESIS_HASH

    tip = _read_chain_tip()
    if tip is None:
        if rows:
            print(
                "[llm_gateway] Не найден файл контрольной точки истории конфигурации "
                "— либо это первое включение этой защиты на уже существующей базе, "
                "либо файл был удалён. Записываю контрольную точку по текущему состоянию."
            )
            _write_chain_tip(db_seq, db_hash)
        return

    tip_seq, tip_hash = tip
    if db_seq < tip_seq:
        raise SecureStoreError(
            "ОБНАРУЖЕН ОТКАТ: файл базы настроек отстаёт от последней известной "
            f"контрольной точки (в базе seq={db_seq}, ожидалось не меньше seq={tip_seq}) "
            "— похоже, файл базы был подменён более старой копией."
        )
    if db_seq == tip_seq and not hmac.compare_digest(db_hash, tip_hash):
        raise SecureStoreError(
            f"обнаружено расхождение истории на записи seq={db_seq} — данные подделаны."
        )
    if db_seq > tip_seq:
        # Легитимный случай: контрольная точка не успела обновиться (например,
        # процесс упал между записью в базу и записью контрольной точки).
        _write_chain_tip(db_seq, db_hash)

    # Журнал сам по себе может быть безупречен, но НЕ отражать фактическое
    # содержимое таблицы, если строку вставили/удалили напрямую через SQL,
    # в обход set_model_entry()/remove_model_entry(). Сверяем журнал-
    # предсказанный набор строк с тем, что реально лежит в model_config —
    # лишняя, отсутствующая или изменённая строка ловится здесь, ДО того,
    # как что-либо будет расшифровано и отдано наружу.
    actual_rows = conn.execute("SELECT name_index, name_blob, config_blob FROM model_config").fetchall()
    actual_live = {
        name_index: hashlib.sha256(name_blob + b"|" + config_blob).digest()
        for name_index, name_blob, config_blob in actual_rows
    }
    if actual_live != expected_live:
        extra = set(actual_live) - set(expected_live)
        missing = set(expected_live) - set(actual_live)
        changed = {k for k in (set(actual_live) & set(expected_live)) if actual_live[k] != expected_live[k]}
        raise SecureStoreError(
            "расхождение между журналом истории и фактической таблицей настроек — "
            "данные были изменены в обход журнала (set_model_entry()/remove_model_entry() не вызывались): "
            f"лишних строк={len(extra)}, отсутствующих строк={len(missing)}, изменённых={len(changed)}"
        )


def get_model_entry(model: str) -> dict[str, Any] | None:
    """Настройка узла для этого имени модели, если есть. Расшифровка
    проверяет подлинность (AEAD-тег) — подменённые байты дают ошибку,
    а не тихо принятые поддельные данные."""
    conn = _connect()
    try:
        if _is_genuinely_empty(conn):
            return None
        kek = _load_kek_for_read()
        integrity_key = _keys.derive_integrity_key(kek)
        _verify_chain(conn, integrity_key)

        index_key = _keys.derive_blind_index_key(kek)
        name_index = _crypto.blind_index(index_key, _NAME_NAMESPACE, model)
        row = conn.execute(
            "SELECT config_blob FROM model_config WHERE name_index = ?", (name_index,)
        ).fetchone()
        if row is None:
            return None

        config_key = _keys.derive_node_config_key(kek)
        try:
            plaintext = _crypto.decrypt_field(
                config_key, row[0], entity_type=_ENTITY_TYPE, entity_id=name_index, field_name="config"
            )
        except Exception as e:
            raise SecureStoreError(
                f"запись для {model!r} повреждена или подделана (расшифровка не прошла): {e}"
            ) from e
        return json.loads(plaintext)
    finally:
        conn.close()


def set_model_entry(model: str, entry: dict[str, Any]) -> None:
    """Вставляет НОВУЮ запись. Если запись с этим именем уже есть —
    сначала явно удали (remove_model_entry), это не INSERT OR REPLACE:
    "изменить" не должно быть доступно даже в одну функцию."""
    kek = _ensure_kek()
    index_key = _keys.derive_blind_index_key(kek)
    config_key = _keys.derive_node_config_key(kek)
    integrity_key = _keys.derive_integrity_key(kek)

    name_index = _crypto.blind_index(index_key, _NAME_NAMESPACE, model)
    name_blob = _crypto.encrypt_field(
        config_key, model, entity_type=_ENTITY_TYPE, entity_id=name_index, field_name="model_name"
    )
    config_blob = _crypto.encrypt_field(
        config_key, json.dumps(entry, ensure_ascii=False),
        entity_type=_ENTITY_TYPE, entity_id=name_index, field_name="config",
    )

    conn = _connect()
    try:
        _verify_chain(conn, integrity_key)
        try:
            conn.execute(
                "INSERT INTO model_config (name_index, name_blob, config_blob, created_at_ms) VALUES (?, ?, ?, ?)",
                (name_index, name_blob, config_blob, int(time.time() * 1000)),
            )
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise SecureStoreError(
                f"запись для {model!r} уже существует — сначала удали её явно, "
                f"перезапись на месте запрещена"
            ) from e
        seq, entry_hash = _append_journal(conn, integrity_key, "insert", name_index, name_blob + b"|" + config_blob)
        conn.commit()
    finally:
        conn.close()
    _write_chain_tip(seq, entry_hash)


def remove_model_entry(model: str) -> bool:
    conn = _connect()
    try:
        if _is_genuinely_empty(conn):
            return False
        kek = _load_kek_for_read()
        integrity_key = _keys.derive_integrity_key(kek)
        _verify_chain(conn, integrity_key)

        index_key = _keys.derive_blind_index_key(kek)
        name_index = _crypto.blind_index(index_key, _NAME_NAMESPACE, model)
        row = conn.execute(
            "SELECT name_blob, config_blob FROM model_config WHERE name_index = ?", (name_index,)
        ).fetchone()
        if row is None:
            return False

        conn.execute("DELETE FROM model_config WHERE name_index = ?", (name_index,))
        seq, entry_hash = _append_journal(conn, integrity_key, "delete", name_index, row[0] + b"|" + row[1])
        conn.commit()
    finally:
        conn.close()
    _write_chain_tip(seq, entry_hash)
    return True


def list_models() -> dict[str, Any]:
    conn = _connect()
    try:
        if _is_genuinely_empty(conn):
            return {}
        kek = _load_kek_for_read()
        integrity_key = _keys.derive_integrity_key(kek)
        _verify_chain(conn, integrity_key)
        config_key = _keys.derive_node_config_key(kek)
        rows = conn.execute("SELECT name_index, name_blob, config_blob FROM model_config").fetchall()
    finally:
        conn.close()

    result: dict[str, Any] = {}
    for name_index, name_blob, config_blob in rows:
        try:
            name = _crypto.decrypt_field(
                config_key, name_blob, entity_type=_ENTITY_TYPE, entity_id=name_index, field_name="model_name"
            )
            entry = _crypto.decrypt_field(
                config_key, config_blob, entity_type=_ENTITY_TYPE, entity_id=name_index, field_name="config"
            )
        except Exception as e:
            raise SecureStoreError(f"запись повреждена или подделана (расшифровка не прошла): {e}") from e
        result[name] = json.loads(entry)
    return result
