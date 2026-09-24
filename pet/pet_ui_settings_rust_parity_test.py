"""
pet/pet_ui_settings_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/ui_settings.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
pet/ui_settings.py::validate (шлюз ввода настроек вкладки «YANDI»).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать четвёртый шаг переноса Python -> Rust (2026-09-24). Дифференциальный фаззинг сгенерированными документами из «родных» и
посторонних типов JSON: результат (проверенный словарь — включая порядок ключей — либо `SettingsError` с ТОЧНЫМ текстом) совпадает.
Проверяются: неизвестные поля, ключ API в `api` (key/api_key/token/secret), `api`/`local`/`remote` как falsy/не-объекты (ложные значения
равны `{}`), voice/advisors (тип, состав, дубликаты, порядок в результате), `local.path` (strip Python — включая U+001C..1F, длина в СИМВОЛАХ
1024/1025), адрес (`\\s` Python, первый символ не из `/$.?#`, регистр схемы), имя модели (ASCII-класс, длина 128/129 СИМВОЛОВ), service, порядок
ошибок (оригинал вычисляет ОБА `_text` модели до проверок формата), «блок выбран, но не заполнен».
ЕДИНСТВЕННОЕ отличие Rust: при нескольких незаполненных выбранных блоках оригинал называет случайный (перебор `set` строк, порядок зависит
от хэш-рандомизации) — тест принимает любой из настоящих незаполненных.
`load`/`save` (файл, права, атомарная запись) не переносились; собственный тест вкладки настроек прогоняется с включённым переключателем.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m pet.pet_ui_settings_rust_parity_test
"""
from __future__ import annotations

import copy
import os
import random
import sys
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


class MyDict(dict):
    pass


def main() -> int:
    try:
        import yandi_rs.ui_settings as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import pet.ui_settings as us

    rnd = random.Random(20260924)

    def run(doc):
        try:
            r = us.validate(copy.deepcopy(doc))
            return ("ok", repr(r), list(r), list(r["api"]))
        except us.SettingsError as e:
            return ("err", str(e))
        except Exception as e:  # noqa: BLE001
            return ("exc", type(e).__name__)

    def both(doc):
        us._rust_us = False
        p = run(doc)
        us._rust_us = rs
        r = run(doc)
        return p, r

    def unfilled_kinds(doc) -> set:
        """Какие из выбранных блоков на самом деле не заполнены (по отдельной проверке каждого как Голоса)."""
        out = set()
        d = copy.deepcopy(doc)
        sel = {d.get("voice")} | set(d.get("advisors") or [])
        for k in sel:
            if k not in us.KINDS:
                continue
            d2 = copy.deepcopy(d)
            d2["voice"], d2["advisors"] = k, []
            try:
                us.validate(d2)
            except us.SettingsError as e:
                if str(e).startswith("блок «"):
                    out.add(str(e))
            except Exception:  # noqa: BLE001
                pass
        return out

    saved = os.environ.get("YANDI_UI_SETTINGS_ENGINE")
    n = 0
    try:
        VOICE = ["", "local", "remote", "api", None, 5, "x", "Local", ["local"], True, "local "]
        ADV = [[], ["local"], ["remote", "api", "local"], ["api", "api"], ["local", "remote"], None, "local", [1], [None], ["x"], [["local"]], ["Local"], {}, ["api", "x"], (), 0]
        PATHS = [None, "", "/m/model.gguf", " /m/x ", 5, "a" * 1024, "a" * 1025, " " + "a" * 1024 + " ", "é" * 1024, "é" * 1025, "\x1c/x\x1d", " /x", ["p"], "\n"]
        ADDR = [None, "", "http://h", "https://192.168.1.5:8080", "http://", "http:///x", "ftp://h", " http://h ", "http://a b", "http://.", "http://é", "http://a\u0085b",
                "http://a​b", "HTTP://h", "http://$x", "http://h\n", "https://", "http://x/", "http://?", "http://#", "http://h\x1cx", "http://\x1cx", "http://\x1fabc", "http://\x85x", "http://\xa0x", "http://\u2028x", "http://\u3000x", "h", 5, "x" * 512, "http://" + "x" * 505,
                "http://" + "x" * 506, "http://　x", "http:// x"]
        MODELS = [None, "", "qwen-14b", "-x", "é", "a" * 128, "a" * 129, " m ", "m\n", "a b", "a/b:c@d+e.f_g", ".x", "_x", "Ａ", "ab\x00", 5, ["m"], "M" * 127, "x" * 100 + "é",
                  "m\x1c", "\x1cm"]
        SERV = [None, "openai", "anthropic", "other", "x", "OPENAI", 5, ["openai"], ""]
        OBJ = [None, {}, [], "", 0, False, True, 1, "x", [1], MyDict(), 0.0, 1.5, float("nan")]
        EXTRA = ["foo", "Voice", "key", "", "extensions", "z", "a"]

        def gen():
            d = {}
            if rnd.random() < 0.9:
                d["voice"] = rnd.choice(VOICE)
            if rnd.random() < 0.85:
                d["advisors"] = rnd.choice(ADV)
            if rnd.random() < 0.85:
                if rnd.random() < 0.85:
                    loc = {}
                    if rnd.random() < 0.9:
                        loc["path"] = rnd.choice(PATHS)
                    d["local"] = loc
                else:
                    d["local"] = rnd.choice(OBJ)
            if rnd.random() < 0.85:
                if rnd.random() < 0.85:
                    rem = {}
                    if rnd.random() < 0.9:
                        rem["address"] = rnd.choice(ADDR)
                    if rnd.random() < 0.9:
                        rem["model"] = rnd.choice(MODELS)
                    d["remote"] = rem
                else:
                    d["remote"] = rnd.choice(OBJ)
            if rnd.random() < 0.85:
                if rnd.random() < 0.85:
                    api = {}
                    if rnd.random() < 0.9:
                        api["model"] = rnd.choice(MODELS)
                    if rnd.random() < 0.7:
                        api["service"] = rnd.choice(SERV)
                    if rnd.random() < 0.1:
                        api[rnd.choice(["key", "api_key", "token", "secret", "Key", "keys"])] = rnd.choice(["x", None, 0])
                    d["api"] = api
                else:
                    d["api"] = rnd.choice(OBJ)
            if rnd.random() < 0.08:
                d[rnd.choice(EXTRA)] = rnd.choice([1, "x", None])
            return d

        def good():
            return {"voice": rnd.choice(["local", "remote", "api"]), "advisors": rnd.choice([[], ["local"], ["remote"], ["api"], ["api", "local", "remote"]]),
                    "local": {"path": "/m/x.gguf"}, "remote": {"address": "http://h:1", "model": "m1"}, "api": {"service": "other", "model": "gpt-x"}}

        n_ok = n_err = 0
        for i in range(60000):
            doc = gen() if i % 3 else good()
            # порча «хорошего» документа одним полем
            if i % 3 == 0 and rnd.random() < 0.8:
                which = rnd.choice(["voice", "advisors", "local", "remote", "api", "path", "address", "rmodel", "amodel", "service"])
                if which in ("voice", "advisors", "local", "remote", "api"):
                    doc[which] = rnd.choice({"voice": VOICE, "advisors": ADV, "local": OBJ, "remote": OBJ, "api": OBJ}[which])
                elif which == "path":
                    doc["local"]["path"] = rnd.choice(PATHS)
                elif which == "address":
                    doc["remote"]["address"] = rnd.choice(ADDR)
                elif which == "rmodel":
                    doc["remote"]["model"] = rnd.choice(MODELS)
                elif which == "amodel":
                    doc["api"]["model"] = rnd.choice(MODELS)
                else:
                    doc["api"]["service"] = rnd.choice(SERV)
            p, r = both(doc)
            n += 1
            if p[0] == "err" and p[1].startswith("блок «"):
                allowed = unfilled_kinds(doc)
                same = r[0] == "err" and r[1] in allowed and p[1] in allowed and (len(allowed) > 1 or p == r)
            else:
                same = p == r
            check("A1 validate", same, f"{doc!r}: {p} vs {r}")
            if p[0] == "ok":
                n_ok += 1
            else:
                n_err += 1

        # ---- B. вход не-объект и трудные --------------------------------------------------------------------------------
        for doc in (None, [], "x", 5, 1.5, True, {}, MyDict(voice="local"), {"voice": "local", "advisors": [], "local": {"path": "/m"}}, {1: 2}, {"voice": "local", 5: 6},
                    {"local": MyDict(path="/m"), "voice": "local"}, {"voice": "local", "local": {"path": "/m"}, "api": MyDict()}):
            p, r = both(doc)
            n += 1
            check("B1 трудные входы", p == r, f"{doc!r}: {p} vs {r}")
        # сортировка неизвестных полей
        for keys in (["b", "a"], ["я", "a", "Z"], ["é", "e"], ["a", "B"], ["\U0001f30d", "￿"]):
            doc = {k: 1 for k in keys}
            p, r = both(doc)
            n += 1
            check("B2 сортировка неизвестных", p == r, f"{keys}: {p} vs {r}")
        # оба текста модели вычисляются до проверок формата
        p, r = both({"voice": "api", "remote": {"model": "-bad"}, "api": {"model": 5}})
        check("B3 порядок ошибок model", p == r and p[1] == "api.model: нужна строка", f"{p} {r}")
        # порядок advisors в результате всегда local/remote/api
        p, r = both(good() | {"advisors": ["api", "remote", "local", "api"], "voice": "local"})
        check("B4 порядок и дубли advisors", p == r and "['local', 'remote', 'api']" in p[1], f"{p}")
        # суррогаты
        for doc in ({"voice": "local", "local": {"path": "\ud800"}}, {"voice": "\ud800"}, {"\ud800": 1}):
            p, r = both(doc)
            n += 1
            check("B5 суррогаты → Python-путь", p == r, f"{doc!r}: {p} vs {r}")

        # ---- C. константы изменены / переключатель ------------------------------------------------------------------------
        us._rust_us = rs
        old = us.MAX_PATH
        us.MAX_PATH = 5
        try:
            r = run({"voice": "local", "local": {"path": "abcdef"}})
            check("C1 изменённые константы уважаются (Python-путь)", r[0] == "err" and "слишком длинное" in r[1], str(r))
        finally:
            us.MAX_PATH = old
        os.environ.pop("YANDI_UI_SETTINGS_ENGINE", None)
        us._rust_us = None
        check("C2 по умолчанию выключен", us._get_rust_us() is None)
        os.environ["YANDI_UI_SETTINGS_ENGINE"] = "rust"
        us._rust_us = None
        check("C3 включается переменной", us._get_rust_us() is not None)
        os.environ.pop("YANDI_UI_SETTINGS_ENGINE", None)
        us._rust_us = None
        check("C4 выключение возвращает Python", us._get_rust_us() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_UI_SETTINGS_ENGINE", None)
        else:
            os.environ["YANDI_UI_SETTINGS_ENGINE"] = saved
        us._rust_us = None
    src = (ROOT / "pet" / "ui_settings.py").read_text(encoding="utf-8")
    check("C5 сбой импорта yandi_rs пойман (ImportError)", "except ImportError:" in src)
    print(f"\n(документов: {n}; годных: {n_ok}, отвергнутых: {n_err}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
