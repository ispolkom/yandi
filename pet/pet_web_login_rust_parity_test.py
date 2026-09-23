"""
pet/pet_web_login_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/web_login.rs
(Sessions, Throttle) даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и pet/web_login.py, на одной и той же
временной шкале.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Второй шаг переноса Python -> Rust (2026-09-23; первый — pet/pet_local_guard_rust_parity_test.py).
Методология — rustlib/README.md.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m pet.pet_web_login_rust_parity_test
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


class Clock:
    """Тот же тестовый двойник часов, что и в pet_web_login_regression_test.py: __call__ вместо
    настоящего time.time, чтобы гонять время в тестах без реального ожидания."""
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def main() -> int:
    try:
        import yandi_rs.web_login as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import pet.web_login as py

    # ── Sessions: одна и та же временная шкала для обеих реализаций ──
    clock = Clock()
    py_s = py.Sessions(clock=clock)
    rs_s = rs.Sessions()
    rs_s._clock = clock  # тот же способ, каким pet_web_login_regression_test.py подменяет часы

    for remember in (False, True):
        py_tok, py_secs = py_s.create(remember)
        rs_tok, rs_secs = rs_s.create(remember)
        check(f"S1 create(remember={remember}) секунды совпадают", py_secs == rs_secs, f"python={py_secs} rust={rs_secs}")
        check(f"S2 create(remember={remember}) токен выглядит как настоящий token_urlsafe(32) (43 символа)",
              len(py_tok) == 43 and len(rs_tok) == 43, f"python len={len(py_tok)} rust len={len(rs_tok)}")
        check(f"S3 create(remember={remember}) сразу валиден в обеих реализациях",
              py_s.valid(py_tok) and rs_s.valid(rs_tok))

        clock.now += py_secs - 1
        check(f"S4 remember={remember}: за секунду до истечения ещё валиден (обе)",
              py_s.valid(py_tok) == rs_s.valid(rs_tok) == True, f"python={py_s.valid(py_tok)} rust={rs_s.valid(rs_tok)}")

        clock.now += 2
        py_after = py_s.valid(py_tok)
        rs_after = rs_s.valid(rs_tok)
        check(f"S5 remember={remember}: после истечения невалиден в обеих реализациях",
              py_after == rs_after == False, f"python={py_after} rust={rs_after}")

        clock.now = 1_000_000.0  # сброс шкалы для следующего remember

    # ── end() / clear() ──
    t1, _ = py_s.create(False)
    t2, _ = py_s.create(False)
    py_s.end(t1)
    check("S6 end() снимает только указанный токен (python)", not py_s.valid(t1) and py_s.valid(t2))
    r1, _ = rs_s.create(False)
    r2, _ = rs_s.create(False)
    rs_s.end(r1)
    check("S6 end() снимает только указанный токен (rust)", not rs_s.valid(r1) and rs_s.valid(r2))

    py_s.clear()
    rs_s.clear()
    check("S7 clear() снимает все токены (python)", not py_s.valid(t2))
    check("S7 clear() снимает все токены (rust)", not rs_s.valid(r2))

    # ── краевые случаи valid()/end() ──
    for bad in (None, ""):
        check(f"S8 valid({bad!r}) -> False в обеих реализациях", py_s.valid(bad) == rs_s.valid(bad) == False)
    py_s.end(None)  # не должно падать
    rs_s.end(None)
    check("S9 end(None) не падает ни в одной реализации", True)

    # ── Throttle: одна и та же временная шкала ──
    clock2 = Clock()
    py_t = py.Throttle(clock=clock2)
    rs_t = rs.Throttle()
    rs_t._clock = clock2

    for failures in range(0, 15):
        py_b = py_t.backoff_after(failures)
        rs_b = rs_t.backoff_after(failures)
        check(f"T1 backoff_after({failures}) совпадает", py_b == rs_b, f"python={py_b} rust={rs_b}")

    check("T2 remaining()==0 без единой неудачи (обе)", py_t.remaining() == rs_t.remaining() == 0)

    for i in range(6):
        py_t.failed()
        rs_t.failed()
        check(f"T3.{i} после {i+1}-й неудачи failures совпадает", py_t.failures == rs_t.failures)
        check(f"T3.{i} после {i+1}-й неудачи remaining() совпадает", py_t.remaining() == rs_t.remaining(),
              f"python={py_t.remaining()} rust={rs_t.remaining()}")

    for delta in (1, 5, 100, 1000):
        clock2.now += delta
        check(f"T4 remaining() через +{delta}с совпадает", py_t.remaining() == rs_t.remaining(),
              f"python={py_t.remaining()} rust={rs_t.remaining()}")

    py_t.succeeded()
    rs_t.succeeded()
    check("T5 succeeded() обнуляет failures в обеих реализациях", py_t.failures == rs_t.failures == 0)
    check("T6 после succeeded() remaining()==0 (обе)", py_t.remaining() == rs_t.remaining() == 0)

    # ── прямая перезапись .failures/.last (то, что делает reset_state()) работает в обеих ──
    rs_t.failures = 7
    rs_t.last = 42.0
    check("T7 .failures напрямую перезаписывается (rust)", rs_t.failures == 7)
    check("T8 .last напрямую перезаписывается (rust)", rs_t.last == 42.0)

    # ── G: сам переключатель в pet/web_login.py (не только «обе реализации сами по себе совпадают») ──
    saved_env = os.environ.get("YANDI_LOGIN_ENGINE")
    try:
        os.environ.pop("YANDI_LOGIN_ENGINE", None)
        check("G0 по умолчанию (без переменной) _make_sessions даёт Python Sessions", type(py._make_sessions()).__module__ == "pet.web_login")
        check("G0 по умолчанию (без переменной) _make_throttle даёт Python Throttle", type(py._make_throttle()).__module__ == "pet.web_login")

        os.environ["YANDI_LOGIN_ENGINE"] = "rust"
        made_s = py._make_sessions()
        made_t = py._make_throttle()
        check("G1 переменная окружения реально переключает Sessions на собранный Rust-класс",
              type(made_s) is type(rs_s), f"got {type(made_s)}")
        check("G1 переменная окружения реально переключает Throttle на собранный Rust-класс",
              type(made_t) is type(rs_t), f"got {type(made_t)}")

        # тот же временной сценарий через ЗАВОДСКУЮ функцию (не через прямой rs.Sessions()) —
        # доказывает, что переключённый объект действительно ведёт себя как Rust-версия, а не
        # просто имеет правильный тип
        clock3 = Clock()
        made_s._clock = clock3
        tok, secs = made_s.create(False)
        check("G2 переключённый Sessions.create() ведёт себя как прямой rs.Sessions()", made_s.valid(tok) is True)
        clock3.now += secs + 1
        check("G3 переключённый Sessions корректно считает истечение", made_s.valid(tok) is False)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_LOGIN_ENGINE", None)
        else:
            os.environ["YANDI_LOGIN_ENGINE"] = saved_env

    # ── статическая проверка: отказ модуля (не собран) не роняет сервер ──
    src = (ROOT / "pet" / "web_login.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError) и не падает наружу — Sessions",
          src.count("except ImportError as e:") >= 2)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
