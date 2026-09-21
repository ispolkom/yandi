"""
agent/db/sql/protect.py — P1c-2: seal / unseal / back up / restore the protected columns of the personal ledger.

    yandi-keys core-key | python -m agent.db.sql.protect seal        # off -> sealed (every existing row), then protection is ON
    yandi-keys core-key | python -m agent.db.sql.protect unseal      # back to plaintext (rollback; the same checks)
    yandi-keys core-key | python -m agent.db.sql.protect backup --out FILE
    yandi-keys core-key | python -m agent.db.sql.protect restore --from FILE      (only into EMPTY protected tables)
    python -m agent.db.sql.protect status                            # needs no key: counts only

The key arrives on standard input (base64 of the 32-byte core key, as `yandi-keys core-key` prints it); the tool refuses to read it
from a terminal, never prints it and never writes it anywhere. STOP the node, the Core and the PET before `seal` / `unseal` / `restore`:
the tool proves at the end that nothing changed under it (a stray write makes it refuse to switch the mode), it does not lock the
application out.

HOW `seal` STAYS SAFE. It measures the plaintext content of every protected column first (a SHA-256 over table, row, column, text),
seals table by table — each table in ONE transaction that also re-reads and re-opens what it wrote, comparing to the same measure —
and only when the whole ledger opens back to exactly the measured content does it switch the mode to ON. An interruption leaves the
mode at MIGRATING, in which the application reads both forms; running `seal` again finishes the job. Append-only tables carry a
BEFORE UPDATE trigger: the tool drops exactly the trigger the schema layer created (`trg_<table>_no_update`), rewrites, and puts the
same trigger back in every case, success or failure; an UPDATE trigger it does not recognise stops it before anything is changed.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import decimal
import hashlib
import json
import os
import re
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.db.sql import field_protection as fp
from agent.db.sql.security_triggers import _reject_update_trigger

# FK order: a row's parents are restored first.
ORDER = ["interaction_turn", "personal_fact", "personal_fact_event", "commitment", "commitment_event", "grievance"]
PRIMARY_KEY = {
    "interaction_turn": "interaction_id", "personal_fact": "fact_id", "personal_fact_event": "event_id",
    "commitment": "commitment_id", "commitment_event": "event_id", "grievance": "id",
}
assert set(ORDER) == set(fp.PROTECTED) == set(PRIMARY_KEY)

WIDE_TYPES = ("text", "mediumtext", "longtext")
BATCH = 500
LOCK_NAME = "yandi_storage_protection"


class ProtectError(Exception):
    """Something the tool refuses to do or cannot prove. Messages carry table names and counts, never text or keys."""


# ── SQL text: identifiers only, and only validated ones ──────────────────────────────────────────────────────────
# Nothing a person typed ever reaches a SQL string (values are always parameters). Table and column names come from the fixed lists above
# or, for a restore, from a backup file that is authenticated and checked against the database's own columns; every one passes _ident().
# The builders return the SQL text; no f-string is ever handed to .execute() directly (agent/db_sql_security_injection_regression_test.py).
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ProtectError("a name that is not a plain SQL identifier was refused")
    return f"`{name}`"


def _table(table: str) -> str:
    if table not in PRIMARY_KEY:
        raise ProtectError("not a table of the personal ledger")
    return _ident(table)


def _sql_first_batch(table: str) -> str:
    return f"SELECT * FROM {_table(table)} ORDER BY {_ident(PRIMARY_KEY[table])} LIMIT %s"


def _sql_next_batch(table: str) -> str:
    pk = _ident(PRIMARY_KEY[table])
    return f"SELECT * FROM {_table(table)} WHERE {pk} > %s ORDER BY {pk} LIMIT %s"


def _sql_count(table: str) -> str:
    return f"SELECT COUNT(*) AS n FROM {_table(table)}"


def _sql_drop_update_trigger(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {_ident('trg_' + table + '_no_update')}"


def _sql_guarded_update(table: str, columns) -> str:
    """UPDATE ... SET each column WHERE the key matches AND each column still holds what was read (compare-and-set: never overwrite
    what changed under us)."""
    assignments = ", ".join(f"{_ident(c)} = %s" for c in columns)
    guards = " AND ".join(f"{_ident(c)} <=> %s" for c in columns)
    return f"UPDATE {_table(table)} SET {assignments} WHERE {_ident(PRIMARY_KEY[table])} = %s AND {guards}"


def _sql_insert(table: str, columns) -> str:
    return f"INSERT INTO {_table(table)} ({', '.join(_ident(c) for c in columns)}) VALUES ({', '.join(['%s'] * len(columns))})"


# ── reading ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _columns(conn, table: str) -> Dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name AS c, data_type AS t FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s", (table,))
        return {r["c"]: str(r["t"]).lower() for r in cur.fetchall()}


def check_schema(conn) -> None:
    """Every protected column must be a wide text column (schema v19); a ciphertext does not fit in a VARCHAR(500) or a JSON."""
    for table in ORDER:
        cols = _columns(conn, table)
        if not cols:
            raise ProtectError(f"table {table} does not exist: apply the schema first (python -m agent.db.sql.migrate)")
        for column in fp.PROTECTED[table]:
            if cols.get(column) not in WIDE_TYPES:
                raise ProtectError(f"{table}.{column} is {cols.get(column)!r}: apply schema v19 first (python -m agent.db.sql.migrate)")


def _rows(conn, table: str):
    """Every row of a table in primary-key order, in batches (keyset pagination)."""
    pk, last = PRIMARY_KEY[table], None
    while True:
        with conn.cursor() as cur:
            if last is None:
                cur.execute(_sql_first_batch(table), (BATCH,))
            else:
                cur.execute(_sql_next_batch(table), (last, BATCH))
            batch = cur.fetchall()
        if not batch:
            return
        for row in batch:
            yield row
        last = batch[-1][pk]


def classify(key: bytes, table: str, column: str, row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """('null'|'plain'|'sealed', the plaintext) of one stored value. A value that looks sealed and does not open is an error, not plaintext."""
    stored = row[column]
    if stored is None:
        return "null", None
    if fp.is_sealed(stored):
        try:
            return "sealed", fp.open_with(key, table, column, row, stored)
        except fp.StorageTampered:
            raise ProtectError(f"a value of {table}.{column} (row {row[PRIMARY_KEY[table]]}) looks sealed but does not open with this key") from None
    return "plain", stored[len(fp.ESCAPED_PREFIX):] if stored.startswith(fp.ESCAPED_PREFIX) else stored


class Scan:
    def __init__(self) -> None:
        self.rows: Dict[str, int] = {}
        self.plain: Dict[str, int] = {}
        self.sealed: Dict[str, int] = {}
        self._digest = hashlib.sha256()

    @property
    def digest(self) -> str:
        return self._digest.hexdigest()

    def total(self, which: Dict[str, int]) -> int:
        return sum(which.values())


def scan(conn, key: bytes, tables: Optional[List[str]] = None) -> Scan:
    result = Scan()
    for table in (tables or ORDER):
        result.rows[table] = result.plain[table] = result.sealed[table] = 0
        for row in _rows(conn, table):
            result.rows[table] += 1
            for column in fp.PROTECTED[table]:
                kind, text = classify(key, table, column, row)
                if kind == "plain":
                    result.plain[table] += 1
                elif kind == "sealed":
                    result.sealed[table] += 1
                result._digest.update(json.dumps([table, row[PRIMARY_KEY[table]], column, text], ensure_ascii=False).encode("utf-8") + b"\n")
    return result


def status(conn) -> Dict[str, Any]:
    """Counts by form, no key needed (a value is 'sealed' by its prefix)."""
    out: Dict[str, Any] = {"mode": None, "tables": {}}
    try:
        out["mode"] = fp.read_mode(conn)
    except fp.StorageTampered:
        out["mode"] = "unverified"
    for table in ORDER:
        rows = sealed = plain = 0
        for row in _rows(conn, table):
            rows += 1
            for column in fp.PROTECTED[table]:
                if row[column] is None:
                    continue
                if fp.is_sealed(row[column]):
                    sealed += 1
                else:
                    plain += 1
        out["tables"][table] = {"rows": rows, "sealed_values": sealed, "plain_values": plain}
    return out


# ── the mode record ───────────────────────────────────────────────────────────────────────────────────────────────

def set_mode(conn, mode: str) -> None:
    mode, nonce, proof = fp.new_mode_record(mode)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO storage_protection_event (mode, nonce, proof, created_at) VALUES (%s, %s, %s, UTC_TIMESTAMP())",
            (mode, nonce, proof))
    fp.forget_mode()


# ── rewriting one table ───────────────────────────────────────────────────────────────────────────────────────────

def _update_triggers(conn, table: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trigger_name AS n FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE() AND event_object_table = %s AND event_manipulation = 'UPDATE'", (table,))
        return [r["n"] for r in cur.fetchall()]


def _preflight(conn) -> None:
    """Refuse, before any change, a table whose UPDATE trigger is not the one the schema layer creates."""
    for table in ORDER:
        unknown = [t for t in _update_triggers(conn, table) if t != f"trg_{table}_no_update"]
        if unknown:
            raise ProtectError(f"{table} has an UPDATE trigger this tool did not create ({len(unknown)}): nothing was changed")


def _rewrite_table(conn, key: bytes, table: str, *, seal: bool, log: Callable[[str], None]) -> int:
    """Seal (or open) every value of one table in one transaction; returns how many values changed."""
    known = f"trg_{table}_no_update"
    triggers = _update_triggers(conn, table)
    unknown = [t for t in triggers if t != known]
    if unknown:
        raise ProtectError(f"{table} has an UPDATE trigger this tool did not create ({len(unknown)}): nothing was changed")
    had_trigger = known in triggers
    before = scan(conn, key, [table])
    if had_trigger:
        with conn.cursor() as cur:
            cur.execute(_sql_drop_update_trigger(table))
    changed = 0
    try:
        conn.begin()
        pk = PRIMARY_KEY[table]
        for row in _rows(conn, table):
            updates: Dict[str, str] = {}
            for column in fp.PROTECTED[table]:
                kind, text = classify(key, table, column, row)
                if seal and kind == "plain":
                    updates[column] = fp.seal_with(key, table, column, row, text)
                elif not seal and kind == "sealed":
                    updates[column] = fp.ESCAPED_PREFIX + text if text.startswith((fp.SEALED_PREFIX, fp.ESCAPED_PREFIX)) else text
            if not updates:
                continue
            with conn.cursor() as cur:
                cur.execute(_sql_guarded_update(table, list(updates)),
                            tuple(updates.values()) + (row[pk],) + tuple(row[c] for c in updates))
                if cur.rowcount != 1:
                    raise ProtectError(f"a row of {table} changed while it was being rewritten: rolled back")
            changed += len(updates)
        after = scan(conn, key, [table])
        if after.digest != before.digest:
            raise ProtectError(f"{table}: the content after the rewrite is not the content before it: rolled back")
        leftover = after.plain[table] if seal else after.sealed[table]
        if leftover:
            raise ProtectError(f"{table}: {leftover} values were left in the old form: rolled back")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if had_trigger:
            with conn.cursor() as cur:
                cur.execute(_reject_update_trigger(table))
    log(f"{table}: {changed} values {'sealed' if seal else 'opened'}")
    return changed


def seal_all(conn, core_key: bytes, log: Callable[[str], None] = print) -> Dict[str, Any]:
    return _convert(conn, core_key, seal=True, log=log)


def unseal_all(conn, core_key: bytes, log: Callable[[str], None] = print) -> Dict[str, Any]:
    return _convert(conn, core_key, seal=False, log=log)


def _convert(conn, core_key: bytes, *, seal: bool, log: Callable[[str], None]) -> Dict[str, Any]:
    fp.install_key(core_key)
    key = fp.storage_key()
    check_schema(conn)
    if not _acquire(conn):
        raise ProtectError("another protect run holds the lock")
    try:
        mode = fp.read_mode(conn)                      # verifies the record with this key: a foreign or forged record stops here
        _preflight(conn)
        before = scan(conn, key)
        log(f"mode {mode}; rows {before.total(before.rows)}; plaintext values {before.total(before.plain)}; sealed values {before.total(before.sealed)}")
        if seal and mode == fp.MODE_ON and before.total(before.plain) == 0:
            return {"changed": 0, "mode": mode, "digest": before.digest}
        if not seal and mode == fp.MODE_OFF and before.total(before.sealed) == 0:
            return {"changed": 0, "mode": mode, "digest": before.digest}
        if mode != fp.MODE_MIGRATING:
            set_mode(conn, fp.MODE_MIGRATING)
        changed = 0
        for table in (ORDER if seal else reversed(ORDER)):
            changed += _rewrite_table(conn, key, table, seal=seal, log=log)
        after = scan(conn, key)
        if after.digest != before.digest:
            raise ProtectError("the content of the ledger is not what it was before: the mode was NOT switched")
        if (after.total(after.plain) if seal else after.total(after.sealed)):
            raise ProtectError("values remain in the old form (a write happened during the run?): the mode was NOT switched")
        set_mode(conn, fp.MODE_ON if seal else fp.MODE_OFF)
        log(f"mode is now {fp.MODE_ON if seal else fp.MODE_OFF}; the content is identical to what it was")
        return {"changed": changed, "mode": fp.MODE_ON if seal else fp.MODE_OFF, "digest": after.digest}
    finally:
        _release(conn)


def _acquire(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 0) AS got", (LOCK_NAME,))
        return bool(cur.fetchone()["got"])


def _release(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
        cur.fetchall()


# ── backup / restore ──────────────────────────────────────────────────────────────────────────────────────────────

BACKUP_MAGIC = b"YANDIBK1"
BACKUP_AAD = b"YANDI|personal-backup|v1"


def _enc(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"$d": value.isoformat()}
    if isinstance(value, decimal.Decimal):
        return {"$n": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _dec(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        (k, v), = value.items()
        if k == "$dt":
            return _dt.datetime.fromisoformat(v)
        if k == "$d":
            return _dt.date.fromisoformat(v)
        if k == "$n":
            return decimal.Decimal(v)
        if k == "$b":
            return base64.b64decode(v)
    return value


def _backup_key(storage_key: bytes) -> bytes:
    return fp._hkdf(storage_key, b"yandi/storage/backup/v1")


def backup(conn, core_key: bytes, path: str, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Write every row of the protected tables, exactly as stored, into ONE file that is itself encrypted (AES-GCM under a key derived from the
    same root). A backup of plaintext rows is therefore not plaintext on disk either."""
    fp.install_key(core_key)
    key = fp.storage_key()
    check_schema(conn)
    mode = fp.read_mode(conn)
    tables: Dict[str, Any] = {}
    for table in ORDER:
        rows = [{k: _enc(v) for k, v in row.items()} for row in _rows(conn, table)]
        tables[table] = {"columns": list(_columns(conn, table)), "rows": rows}
    measured = scan(conn, key)
    doc = {"version": 1, "mode": mode, "digest": measured.digest,
           "counts": {t: len(tables[t]["rows"]) for t in ORDER}, "tables": tables}
    blob = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    data = BACKUP_MAGIC + nonce + AESGCM(_backup_key(key)).encrypt(nonce, blob, BACKUP_AAD)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".backup-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log(f"backup written: {sum(doc['counts'].values())} rows in {len(ORDER)} tables (mode {mode})")
    return {"counts": doc["counts"], "mode": mode, "digest": measured.digest}


def restore(conn, core_key: bytes, path: str, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Put a backup back into EMPTY protected tables. A non-empty table is never touched: the tool refuses. The content is measured
    afterwards and must equal the measure stored in the backup."""
    fp.install_key(core_key)
    key = fp.storage_key()
    check_schema(conn)
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.startswith(BACKUP_MAGIC) or len(data) < len(BACKUP_MAGIC) + 12 + 16:
        raise ProtectError("this is not a backup file made by this tool")
    nonce = data[len(BACKUP_MAGIC):len(BACKUP_MAGIC) + 12]
    try:
        doc = json.loads(AESGCM(_backup_key(key)).decrypt(nonce, data[len(BACKUP_MAGIC) + 12:], BACKUP_AAD).decode("utf-8"))
    except (InvalidTag, ValueError):
        raise ProtectError("the backup does not open with this key (wrong key or a damaged file)") from None
    if not _acquire(conn):
        raise ProtectError("another protect run holds the lock")
    try:
        for table in ORDER:
            with conn.cursor() as cur:
                cur.execute(_sql_count(table))
                if cur.fetchone()["n"]:
                    raise ProtectError(f"{table} is not empty: nothing was restored")
        try:
            conn.begin()
            for table in ORDER:
                spec = doc["tables"][table]
                columns = spec["columns"]
                have = set(_columns(conn, table))
                if not set(columns) <= have:
                    raise ProtectError(f"{table}: the backup has columns this database does not")
                sql = _sql_insert(table, columns)
                for row in spec["rows"]:
                    with conn.cursor() as cur:
                        cur.execute(sql, tuple(_dec(row[c]) for c in columns))
            got = scan(conn, key)
            if got.digest != doc["digest"]:
                raise ProtectError("the restored content is not the content that was backed up: rolled back")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if doc["mode"] != fp.MODE_OFF:
            mode = fp.read_mode(conn)
            if mode != doc["mode"]:
                set_mode(conn, doc["mode"])
        log(f"restored {sum(doc['counts'].values())} rows; mode {doc['mode']}")
        return {"counts": doc["counts"], "mode": doc["mode"], "digest": got.digest}
    finally:
        _release(conn)


# ── the command line ──────────────────────────────────────────────────────────────────────────────────────────────

def read_core_key(stream=None) -> bytes:
    stream = stream or sys.stdin
    if stream.isatty():
        raise ProtectError("the key is read from a pipe (yandi-keys core-key | ...), never typed")
    try:
        key = base64.b64decode(stream.readline().strip(), validate=True)
    except ValueError:
        raise ProtectError("the key on standard input is not base64") from None
    if len(key) != 32:
        raise ProtectError("the key on standard input is not 32 bytes")
    return key


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.db.sql.protect")
    parser.add_argument("command", choices=["status", "seal", "unseal", "backup", "restore"])
    parser.add_argument("--out")
    parser.add_argument("--from", dest="src")
    args = parser.parse_args(argv)

    from agent.db.sql.connection import SqlUnavailable, get_connection
    try:
        with get_connection(autocommit=True) as conn:
            if args.command == "status":
                info = status(conn)
                print(f"mode: {info['mode']}")
                for table, t in info["tables"].items():
                    print(f"  {table}: rows {t['rows']}, sealed values {t['sealed_values']}, plaintext values {t['plain_values']}")
                return 0
            key = read_core_key()
            if args.command == "seal":
                seal_all(conn, key)
            elif args.command == "unseal":
                unseal_all(conn, key)
            elif args.command == "backup":
                if not args.out:
                    parser.error("backup needs --out FILE")
                backup(conn, key, args.out)
            elif args.command == "restore":
                if not args.src:
                    parser.error("restore needs --from FILE")
                restore(conn, key, args.src)
        return 0
    except (ProtectError, fp.StorageProtectionError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except SqlUnavailable as exc:
        print(f"SQL UNAVAILABLE: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
