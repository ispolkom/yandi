"""
llm_gateway/native_secure_store_parity_test.py — доказательство, что РОДНОЕ хранилище настроек моделей узла (rustlib/yandi_llm/src/secure_store.rs) ФАЙЛОВО СОВМЕСТИМО
с llm_gateway/secure_store.py и ловит те же подделки с теми же сообщениями.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Проверяется в обе стороны (Python пишет → Rust читает; Rust пишет → Python читает; смешанная цепочка операций), на ОДНИХ И ТЕХ ЖЕ файлах: SQLite-база с триггерами, файл ключа (KEK, права 0600, каталог 0700),
файл контрольной точки цепочки рядом с ключом (формат `{"seq": N, "hash": "hex"}`), шифроблоки, слепой индекс имени, хеш-цепочка журнала.
Подделки (каждая строится Python-реализацией, потом ОБА исполнения — Python и Rust — из ОДНОГО и того же снимка файлов, результат и ТЕКСТ ошибки должны совпасть):
строка вставлена в обход журнала / удалена напрямую / изменена (после снятия триггера) / запись журнала испорчена / файл базы откатан старой копией / контрольная точка удалена, испорчена,
отстаёт, опережает / база стёрта при живой точке / журнал пуст при живых строках / ключ удалён / права ключа 0644 / ключ не 32 байта / база старой схемы (пустая — пересоздаётся, непустая — отказ).
Плюс: дубликат имени, удаление отсутствующего, чтение отсутствующего, пустое хранилище, юникод и вложенные значения, 20 имён.
Расхождение формата (не считается ошибкой): открытый текст конфигурации родная версия сериализует компактно (`serde_json`), Python — `json.dumps` с пробелами; расшифровка даёт тот же объект.

Требует собранного моста yandi_llm — если не установлен, SKIP.

Run: python -m llm_gateway.native_secure_store_parity_test
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _OK
    if condition:
        _OK += 1
    else:
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_llm
    except ImportError as e:
        print(f"SKIP: yandi_llm не собран ({e})")
        return 0
    from agent.db.sql import keys as K
    from llm_gateway import secure_store as ss

    tmp = Path(tempfile.mkdtemp(prefix="yandi-store-parity-"))
    n = 0
    counter = [0]

    def fresh() -> Path:
        counter[0] += 1
        d = tmp / f"s{counter[0]}"
        d.mkdir(parents=True)
        return d

    def use(d: Path):
        os.environ["YANDI_KEK_PATH"] = str(d / "keys" / "node_kek.bin")
        os.environ["YANDI_NODE_DB"] = str(d / "node_config.sqlite")

    def kind_of(e: Exception) -> str:
        return {ss.SecureStoreError: "SecureStoreError", K.KeyPermissionError: "KeyPermissionError", K.KeyStorageError: "KeyStorageError"}.get(type(e), "Other")

    def py(op, d: Path, *a):
        use(d)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                if op == "get":
                    r = ss.get_model_entry(*a)
                elif op == "set":
                    r = ss.set_model_entry(*a)
                elif op == "remove":
                    r = ss.remove_model_entry(*a)
                else:
                    r = ss.list_models()
            return ("ok", r)
        except Exception as e:  # noqa: BLE001
            return ("err", kind_of(e), str(e))

    def rs(op, d: Path, *a):
        use(d)
        args = {"get": {"model": a[0] if a else ""}, "set": {"model": a[0] if a else "", "entry": a[1] if len(a) > 1 else None}, "remove": {"model": a[0] if a else ""}, "list": {}}[op]
        out = json.loads(yandi_llm.call("store_" + op, json.dumps(args, ensure_ascii=False)))
        if "error" in out:
            return ("err", out["error"]["kind"], out["error"]["msg"])
        return ("ok", out["ok"])

    def snap(d: Path) -> Path:
        e = fresh()
        shutil.rmtree(e)
        shutil.copytree(d, e)
        return e

    import re

    def same_outcome(a, b) -> bool:
        # каталоги снимков (s124 / s125) различаются по построению — в тексте ошибок с путём заменяем их на общий маркер
        norm = lambda x: re.sub(r"/s\d+/", "/S/", json.dumps(x, ensure_ascii=False, sort_keys=True))
        return norm(a) == norm(b)

    def both_from_snapshot(label, base: Path, op, *a):
        """Один и тот же снимок файлов → Python и Rust; результат и текст ошибки совпадают; затем состояния читаются ДРУГОЙ реализацией одинаково."""
        nonlocal n
        d1, d2 = snap(base), snap(base)
        p = py(op, d1, *a)
        r = rs(op, d2, *a)
        n += 1
        check(label, same_outcome(p, r), f"\n py={p}\n rs={r}")
        # перекрёстное чтение итогового состояния
        cp = py("list", d2)
        cr = rs("list", d1)
        n += 1
        check(label + " (итог, перекрёстно)", same_outcome(cp, cr), f"\n py-reads-rs-state={cp}\n rs-reads-py-state={cr}")
        return p, r

    def build_py(d: Path, entries: dict) -> None:
        for k, v in entries.items():
            r = py("set", d, k, v)
            assert r[0] == "ok", r

    E = {
        "heretic:q8": {"backend": "remote", "protocol": "openai", "base_url": "http://192.168.1.5:8080/v1", "model": "qwen-14b", "api_key_env": "MY_KEY"},
        "юникод-модель 🌍": {"a": [1, 2.5, None, True], "b": {"вложенный": {"x": "ключ"}}, "c": "строка\nс переносом", "n": 0.1},
        "local": {"backend": "llama_cpp", "path": "/mnt/models/x.gguf", "n_ctx": 8192, "n_gpu_layers": -1},
    }
    try:
        # ---- A. Python пишет → Rust читает ---------------------------------------------------------------------------------------
        d = fresh()
        build_py(d, E)
        p_list, r_list = py("list", d), rs("list", d)
        n += 1
        check("A1 Python пишет → Rust list", same_outcome(p_list, r_list), f"{p_list} vs {r_list}")
        for k in list(E) + ["нет-такой", ""]:
            n += 1
            check("A2 Python пишет → Rust get", same_outcome(py("get", d, k), rs("get", d, k)), k)
        # ---- B. Rust пишет с нуля → Python читает; формат файлов -------------------------------------------------------------------------
        d2 = fresh()
        for k, v in E.items():
            r = rs("set", d2, k, v)
            n += 1
            check("B1 Rust set с нуля", r == ("ok", None), str(r))
        n += 1
        check("B2 Rust пишет → Python list", same_outcome(py("list", d2), ("ok", E)), f"{py('list', d2)}")
        for k in list(E) + ["нет-такой"]:
            n += 1
            check("B3 Rust пишет → Python get", same_outcome(py("get", d2, k), rs("get", d2, k)), k)
        kek = d2 / "keys" / "node_kek.bin"
        tip = d2 / "keys" / "node_config_chain_tip.json"
        n += 1
        check("B4 Rust: ключ 32 байта, права 0600, каталог 0700", kek.stat().st_size == 32 and stat.S_IMODE(kek.stat().st_mode) == 0o600 and stat.S_IMODE(kek.parent.stat().st_mode) == 0o700,
              f"{kek.stat().st_size} {oct(stat.S_IMODE(kek.stat().st_mode))} {oct(stat.S_IMODE(kek.parent.stat().st_mode))}")
        tj = json.loads(tip.read_text())
        con = sqlite3.connect(str(d2 / "node_config.sqlite"))
        last = con.execute("SELECT seq, entry_hash FROM model_config_journal ORDER BY seq DESC LIMIT 1").fetchone()
        con.close()
        n += 1
        check("B5 Rust: контрольная точка = последняя запись журнала; формат как у Python", tj == {"seq": last[0], "hash": last[1].hex()} and tip.read_text() == json.dumps({"seq": last[0], "hash": last[1].hex()})
              and stat.S_IMODE(tip.stat().st_mode) == 0o600, tip.read_text())
        con = sqlite3.connect(str(d2 / "node_config.sqlite"))
        seqs = [r[0] for r in con.execute("SELECT seq FROM model_config_journal ORDER BY seq")]
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.close()
        n += 1
        check("B5b Rust: журнал начинается с seq=1 без пропусков; база в режиме WAL", seqs == list(range(1, len(seqs) + 1)) and len(seqs) == 3 and mode == "wal", f"{seqs} {mode}")
        # схема идентична: тексты CREATE в sqlite_master
        d3 = fresh()
        build_py(d3, {"x": {"a": 1}})
        c1, c2 = sqlite3.connect(str(d3 / "node_config.sqlite")), sqlite3.connect(str(d2 / "node_config.sqlite"))
        s1 = sorted(c1.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall())
        s2 = sorted(c2.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall())
        c1.close(); c2.close()
        n += 1
        check("B6 схема SQLite и триггеры идентичны (текст CREATE)", s1 == s2, f"\n {s1}\n {s2}")
        # ---- C. смешанная цепочка операций ------------------------------------------------------------------------------------------------
        d4 = fresh()
        seq = [("py", "set", "a", {"v": 1}), ("rs", "set", "b", {"v": 2}), ("py", "set", "c", {"v": 3}), ("rs", "remove", "a"), ("py", "get", "b"), ("rs", "set", "a", {"v": 4}),
               ("py", "remove", "c"), ("rs", "list"), ("py", "remove", "нет"), ("rs", "remove", "b"), ("py", "set", "b", {"v": 5}), ("rs", "get", "b")]
        for who, op, *a in seq:
            r_py = py(op, d4, *a) if who == "py" else rs(op, d4, *a)
            other_view = rs("list", d4) if who == "py" else py("list", d4)
            mine_view = py("list", d4) if who == "py" else rs("list", d4)
            n += 1
            check(f"C1 смешанная цепочка {who} {op}", r_py[0] == "ok" and same_outcome(other_view, mine_view), f"{r_py} {other_view} {mine_view}")
        # ---- D. дубликаты, отсутствующие, пустое хранилище ---------------------------------------------------------------------------------
        both_from_snapshot("D1 дубликат имени", d, "set", "heretic:q8", {"x": 1})
        both_from_snapshot("D2 удаление отсутствующего", d, "remove", "нет-такого")
        both_from_snapshot("D3 чтение отсутствующего", d, "get", "нет-такого")
        empty = fresh()
        for op in ("get", "list", "remove"):
            p, r = py(op, empty, "x") if op != "list" else py(op, empty), (rs(op, empty, "x") if op != "list" else rs(op, empty))
            n += 1
            check(f"D4 пустое хранилище {op}", same_outcome(p, r) and not (empty / "keys" / "node_kek.bin").exists(), f"{p} {r}")
        many = fresh()
        build_py(many, {f"model-{i}": {"i": i, "s": "x" * i} for i in range(20)})
        n += 1
        check("D5 20 записей: Rust читает то, что записал Python", same_outcome(py("list", many), rs("list", many)))

        # ---- E. подделки ------------------------------------------------------------------------------------------------------------------
        def base():
            b = fresh()
            build_py(b, {"one": {"v": 1}, "two": {"v": 2}, "three": {"v": 3}})
            return b

        def sql(b: Path, *stmts, drop_triggers=False):
            con = sqlite3.connect(str(b / "node_config.sqlite"))
            if drop_triggers:
                for t in ("model_config_no_update", "model_config_journal_no_update", "model_config_journal_no_delete"):
                    con.execute(f"DROP TRIGGER IF EXISTS {t}")
            for s_ in stmts:
                con.execute(*s_) if isinstance(s_, tuple) else con.execute(s_)
            con.commit()
            con.close()

        cases = {}
        b = base(); sql(b, ("INSERT INTO model_config VALUES ('rogue', x'00', x'00', 0)",)); cases["строка в обход журнала"] = b
        b = base(); sql(b, "DELETE FROM model_config WHERE rowid = 1"); cases["строка удалена напрямую"] = b
        b = base(); sql(b, "UPDATE model_config SET config_blob = x'0102' WHERE rowid = 2", drop_triggers=True); cases["строка изменена (триггер снят)"] = b
        b = base(); sql(b, "UPDATE model_config_journal SET entry_hash = x'00' WHERE seq = 2", drop_triggers=True); cases["запись журнала испорчена"] = b
        b = base(); sql(b, "UPDATE model_config_journal SET op = 'delete' WHERE seq = 3", drop_triggers=True); cases["op журнала подменён"] = b
        b = base(); (b / "keys" / "node_config_chain_tip.json").unlink(); cases["контрольная точка удалена"] = b
        b = base(); t = b / "keys" / "node_config_chain_tip.json"; j = json.loads(t.read_text()); j["hash"] = "00" * 32; t.write_text(json.dumps(j)); cases["контрольная точка: неверный хеш"] = b
        b = base(); t = b / "keys" / "node_config_chain_tip.json"; j = json.loads(t.read_text()); j["seq"] = 2; t.write_text(json.dumps(j)); cases["контрольная точка отстаёт"] = b
        b = base(); t = b / "keys" / "node_config_chain_tip.json"; j = json.loads(t.read_text()); j["seq"] = 99; t.write_text(json.dumps(j)); cases["контрольная точка опережает (откат базы)"] = b
        b = base(); sql(b, "DELETE FROM model_config", drop_triggers=True); sql(b, "DELETE FROM model_config_journal", drop_triggers=True); cases["база стёрта при живой точке"] = b
        b = base(); sql(b, "DELETE FROM model_config_journal", drop_triggers=True); cases["журнал пуст, строки живы"] = b
        b = base(); (b / "keys" / "node_kek.bin").unlink(); cases["ключ удалён"] = b
        for mode in (0o644, 0o640, 0o660, 0o604, 0o606, 0o666, 0o700, 0o400):
            b = base(); os.chmod(b / "keys" / "node_kek.bin", mode); cases[f"права ключа {oct(mode)}"] = b
        for size in (0, 5, 31, 33, 64):
            b = base(); (b / "keys" / "node_kek.bin").write_bytes(b"k" * size); os.chmod(b / "keys" / "node_kek.bin", 0o600); cases[f"ключ {size} байт"] = b
        # откат: копия базы ДО последней записи поверх текущей
        b = fresh(); build_py(b, {"one": {"v": 1}, "two": {"v": 2}}); old = b / "old.sqlite"; shutil.copy(b / "node_config.sqlite", old)
        con = sqlite3.connect(str(b / "node_config.sqlite")); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.close(); shutil.copy(b / "node_config.sqlite", old)
        build_py(b, {"three": {"v": 3}})
        con = sqlite3.connect(str(b / "node_config.sqlite")); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.close()
        shutil.copy(old, b / "node_config.sqlite"); [p_.unlink() for p_ in b.glob("node_config.sqlite-*")]
        cases["файл базы откатан старой копией"] = b
        for name, b in cases.items():
            for op, args in (("list", ()), ("get", ("one",)), ("remove", ("two",)), ("set", ("new", {"z": 1}))):
                both_from_snapshot(f"E1 подделка «{name}» / {op}", b, op, *args)

        # запись, которую НЕЛЬЗЯ расшифровать, но журнал и таблица согласованы (нужен ключ целостности → строим Python-ом): ошибка расшифровки с ПУСТЫМ хвостом
        b = base()
        use(b)
        kek_ = K.load_kek(str(b / "keys" / "node_kek.bin"))
        ik, xk, ck = K.derive_integrity_key(kek_), K.derive_blind_index_key(kek_), K.derive_node_config_key(kek_)
        from agent.db.sql import crypto as C
        ni = C.blind_index(xk, "node-model-name:v1", "victim")
        nb = C.encrypt_field(ck, "victim", entity_type="node_model_config", entity_id=ni, field_name="model_name")
        cb = C.encrypt_field(os.urandom(32), "{}", entity_type="node_model_config", entity_id=ni, field_name="config")     # ЧУЖОЙ ключ
        con = sqlite3.connect(str(b / "node_config.sqlite"))
        con.execute("INSERT INTO model_config VALUES (?, ?, ?, 0)", (ni, nb, cb))
        seq_, eh_ = ss._append_journal(con, ik, "insert", ni, nb + b"|" + cb)
        con.commit(); con.close()
        ss._write_chain_tip(seq_, eh_)
        for op, args in (("get", ("victim",)), ("list", ()), ("get", ("one",))):
            both_from_snapshot(f"E4 недешифруемая запись при согласованном журнале / {op}", b, op, *args)
        # состояние после самовосстановления контрольной точки: обе стороны дописывают ту же точку
        b = base(); (b / "keys" / "node_config_chain_tip.json").unlink()
        d1, d2 = snap(b), snap(b)
        py("list", d1); rs("list", d2)
        t1 = json.loads((d1 / "keys" / "node_config_chain_tip.json").read_text()); t2 = json.loads((d2 / "keys" / "node_config_chain_tip.json").read_text())
        n += 1
        check("E2 контрольная точка восстановлена одинаково", t1 == t2, f"{t1} {t2}")
        # опережающая точка перезаписывается (процесс упал между базой и точкой)
        b = base(); t = b / "keys" / "node_config_chain_tip.json"; j = json.loads(t.read_text()); good = dict(j); j["seq"] = 2; j["hash"] = "ab" * 32
        # (seq 2 <3, значит db_seq > tip_seq → точка переписывается)
        t.write_text(json.dumps(j))
        d1, d2 = snap(b), snap(b)
        py("list", d1); rs("list", d2)
        n += 1
        check("E3 отстающая точка переписана на текущую", json.loads((d1 / "keys" / "node_config_chain_tip.json").read_text()) == json.loads((d2 / "keys" / "node_config_chain_tip.json").read_text()) == good)
        # ---- F. старая схема ----------------------------------------------------------------------------------------------------------------
        for rows in (0, 2):
            b = fresh()
            (b / "keys").mkdir()
            con = sqlite3.connect(str(b / "node_config.sqlite"))
            con.execute("CREATE TABLE model_config (model_name TEXT PRIMARY KEY, config TEXT)")
            for i in range(rows):
                con.execute("INSERT INTO model_config VALUES (?, ?)", (f"m{i}", "{}"))
            con.commit(); con.close()
            for op, args in (("list", ()), ("get", ("m0",)), ("set", ("x", {"a": 1}))):
                both_from_snapshot(f"F1 старая схема ({rows} строк) / {op}", b, op, *args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for k_ in ("YANDI_KEK_PATH", "YANDI_NODE_DB"):
            os.environ.pop(k_, None)
    print(f"\n(сценариев: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
