"""
pet/pet_local_guard_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/local_guard.rs
даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и pet/local_guard.py, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».
    Если кто-то поменяет правило в pet/local_guard.py и забудет обновить local_guard.rs (или
    наоборот) — этот тест это поймает первым, до того как это увидит в бою владелец.

Первый шаг переноса Python -> Rust (2026-09-23, по задаче владельца: "начнём с малого... rust
пишешь отдельно, python пока не удаляешь"). Методология и следующие шаги — rustlib/README.md.

Требует собранного нативного модуля (rustlib/yandi_rs, собирается через `maturin develop`,
см. rustlib/README.md) — если он не установлен в текущем интерпретаторе, тест пропускается
(SKIP), а не падает: сборка Rust — не часть обычного `scripts/test-all.sh` окружения.

Run: python -m pet.pet_local_guard_rust_parity_test
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# (host, origin, sec_fetch_site, path) — None означает «заголовка не было». Каждый случай отражает
# ровно один сценарий из pet/pet_web_guard_regression_test.py плюс несколько граничных случаев,
# которые тот файл прямым текстом не перечисляет (см. E1-E9 ниже).
EXT_UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
EXT_ORIGIN = f"moz-extension://{EXT_UUID}"

SCENARIOS: list[tuple[str | None, str | None, str | None, str | None]] = [
    # ── локальные программы: без Origin/Sec-Fetch-Site вообще ──
    ("127.0.0.1:9010", None, None, None),
    ("localhost:9010", None, None, None),
    ("127.0.0.1", None, None, None),
    ("[::1]:9010", None, None, None),
    # ── своя страница: Origin совпадает с Host, same-origin/none ──
    ("127.0.0.1:9010", "http://127.0.0.1:9010", "same-origin", None),
    ("127.0.0.1:9010", "http://127.0.0.1:9010", "none", None),
    ("127.0.0.1:9010", "http://127.0.0.1:9010", None, None),
    # ── чужой хост целиком ──
    ("evil.example", None, None, None),
    ("evil.example:80", "http://evil.example:80", "same-origin", None),
    # ── чужой Origin при своём Host (DNS-rebinding-подобная попытка) ──
    ("127.0.0.1:9010", "http://evil.example", None, None),
    ("127.0.0.1:9010", "http://evil.example", "same-origin", None),  # Origin важнее Sec-Fetch-Site
    # ── cross-site Sec-Fetch-Site без Origin ──
    ("127.0.0.1:9010", None, "cross-site", None),
    ("127.0.0.1:9010", None, "cross-origin", None),
    # ── расширение: разрешённые и запрещённые для него пути ──
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/ext/poll"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/ext/result"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/orchestrator/ask"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/orch/history"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/council/connections"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/secret"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", None),           # path неизвестен -> строгая форма
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/ext/../secret"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api//ext/poll"),
    ("127.0.0.1:9010", EXT_ORIGIN, "cross-site", "/api/ext/poll\\x"),
    ("127.0.0.1:9010", "moz-extension://not-a-real-uuid", "cross-site", "/api/ext/poll"),
    ("127.0.0.1:9010", "moz-extension://" + EXT_UUID + "x", "cross-site", "/api/ext/poll"),  # лишний хвост
    # ── E: граничные случаи ──
    ("", None, None, None),                                        # E1 пустой Host
    ("127.0.0.1:9010", "", None, None),                             # E2 Origin: пустая строка (не None!)
    ("127.0.0.1:9010", None, "", None),                             # E3 Sec-Fetch-Site: пустая строка
    ("LOCALHOST:9010", "http://LOCALHOST:9010", "same-origin", None),  # E4 регистр хоста
    ("127.0.0.1:9010", "http://127.0.0.1:9010/", "same-origin", None),  # E5 слэш на конце Origin — не совпадёт
    ("::1", None, None, None),                                      # E6 «голый» IPv6 loopback без скобок
    ("[::1]", "http://[::1]", "same-origin", None),                 # E7 IPv6 без порта
    ("127.0.0.1:9010", "http://127.0.0.1:9010", "SAME-ORIGIN", None),  # E8 регистр Sec-Fetch-Site важен
    (EXT_ORIGIN, None, None, None),                                 # E9 Host сам похож на moz-extension:// (абсурдный, но не должен падать)
]


def py_headers(host, origin, sfs) -> dict:
    d: dict[str, str] = {}
    if host is not None:
        d["host"] = host
    if origin is not None:
        d["origin"] = origin
    if sfs is not None:
        d["sec-fetch-site"] = sfs
    return d


def main() -> int:
    try:
        import yandi_rs.local_guard as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import pet.local_guard as py

    for i, (host, origin, sfs, path) in enumerate(SCENARIOS):
        headers = py_headers(host, origin, sfs)
        py_result = py.is_allowed_request(headers, path)
        rs_result = tuple(rs.is_allowed_request(headers, path))
        check(
            f"P{i} is_allowed_request совпадает: host={host!r} origin={origin!r} sfs={sfs!r} path={path!r}",
            py_result == rs_result,
            f"python={py_result!r} rust={rs_result!r}",
        )

    # is_local_request (строгая форма, path всегда None по определению)
    for i, (host, origin, sfs, _path) in enumerate(SCENARIOS):
        headers = py_headers(host, origin, sfs)
        py_result = py.is_local_request(headers)
        rs_result = tuple(rs.is_local_request(headers))
        check(
            f"L{i} is_local_request совпадает: host={host!r} origin={origin!r} sfs={sfs!r}",
            py_result == rs_result,
            f"python={py_result!r} rust={rs_result!r}",
        )

    # host_name / is_extension_path напрямую
    for host in ("127.0.0.1:9010", "[::1]:9010", "LOCALHOST", "", "[", "a:b:c", "::1"):
        check(f"H host_name({host!r}) совпадает", py._host_name(host) == rs.host_name(host),
              f"python={py._host_name(host)!r} rust={rs.host_name(host)!r}")

    for path in ("/api/ext/poll", "/api/orchestrator/ask", "/api/secret", "", "/api/ext/../x", None):
        if path is None:
            continue  # is_extension_path Python-сигнатура не принимает None
        check(f"X is_extension_path({path!r}) совпадает", py.is_extension_path(path) == rs.is_extension_path(path))

    # ── G: сам переключатель (YANDI_GUARD_ENGINE=rust) в pet/local_guard.py — не только "обе
    # реализации по отдельности совпадают", а "переключённый боевой вызов даёт то же самое" ──
    saved_env = os.environ.get("YANDI_GUARD_ENGINE")
    try:
        os.environ.pop("YANDI_GUARD_ENGINE", None)
        py._rust_guard = None
        check("G0 по умолчанию (без переменной окружения) активен Python-движок", py._get_rust_guard() is None)

        os.environ["YANDI_GUARD_ENGINE"] = "rust"
        py._rust_guard = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", py._get_rust_guard() is rs)

        for i, (host, origin, sfs, path) in enumerate(SCENARIOS):
            headers = py_headers(host, origin, sfs)
            via_switch = py.is_allowed_request(headers, path)
            direct_rust = tuple(rs.is_allowed_request(headers, path))
            check(f"G2.{i} переключённый pet.local_guard.is_allowed_request совпадает с прямым вызовом Rust",
                  via_switch == direct_rust, f"switch={via_switch!r} direct={direct_rust!r}")

        os.environ.pop("YANDI_GUARD_ENGINE", None)
        py._rust_guard = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", py._get_rust_guard() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_GUARD_ENGINE", None)
        else:
            os.environ["YANDI_GUARD_ENGINE"] = saved_env
        py._rust_guard = None

    # ── статическая проверка: отказ модуля (не собран) не роняет сервер, тихо остаёмся на Python ──
    src = (ROOT / "pet" / "local_guard.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError) и не падает наружу",
          "except ImportError as e:" in src and "_rust_guard = False" in src)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
