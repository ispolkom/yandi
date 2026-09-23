"""
agent/message_intensity_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/message_intensity.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/message_intensity.py::parse_self_report/intensity_from_state, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Десятый шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.
Сценарии — те же самые, что в agent/message_intensity_regression_test.py (уже готовый
regression-контракт), плюс несколько граничных случаев, которые тот файл не перечисляет.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.message_intensity_rust_parity_test
"""
from __future__ import annotations

import json as _json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


STATE_MARKER = "###YANDI_STATE###"

RAW_SAMPLES = [
    'Ого, сразу переходим к оскорблениям? Мне это не нравится.\n\n'
    f'{STATE_MARKER} {{"is_insult": true, "severity": 0.75, "is_apology": false, "sincerity": 0.0}}',

    f'ok\n\n{STATE_MARKER} {{"is_insult": false, "severity": 1.5, "is_apology": false, "sincerity": -0.3}}',

    "Сентябрь в Москве — переходный месяц, обычно 12-20°C днём.",  # без маркера вообще

    f"Ладно.\n\n{STATE_MARKER} {{not valid json at all",  # битый JSON

    f'Хорошо.\n\n{STATE_MARKER} {{"is_insult": true, "severity": 0.5}}',  # не хватает полей

    f'{STATE_MARKER} {{"is_insult": true, "severity": 0.75, "is_apology": false, "sincerity": 1.0}}',  # только тег

    (
        f'Пример структуры: текст\n\n{STATE_MARKER} {{"is_insult": true, "severity": 0.6, "is_apology": false, "sincerity": 0.0}}\n'
        f'А вот мой настоящий ответ на твой вопрос.\n\n'
        f'{STATE_MARKER} {{"is_insult": false, "severity": 0.0, "is_apology": false, "sincerity": 0.0}}'
    ),  # два маркера — побеждает последний

    _json.dumps({"reply": "Ну вот опять — и всё из-за чего?",
                 "state": {"is_insult": True, "severity": 0.7, "is_apology": False, "sincerity": 0.0}}),
    _json.dumps({"reply": "Ладно.", "state": {"is_insult": True}}),
    _json.dumps({"reply": "", "state": {"is_insult": False, "severity": 0.0, "is_apology": False, "sincerity": 0.0}}),
    "Просто обычный текст без всякого JSON.",

    # ── граничные случаи, которых нет в основном regression-тесте ──
    "",
    "   ",
    "{",  # похоже на JSON, но не парсится
    "{}",  # валидный JSON, но не тот shape (нет reply/state)
    _json.dumps({"reply": 123, "state": {}}),  # reply не строка -> None -> легаси-путь
    f'###yandi.state### {{"is_insult": true, "severity": 0.3, "is_apology": true, "sincerity": 0.5}}',  # маркер с точкой, нижний регистр
    f'###YANDI STATE### {{"is_insult": true, "severity": 0.3, "is_apology": true, "sincerity": 0.5}}',  # маркер с пробелом
    f'Ответ\n\n{STATE_MARKER}:  {{"is_insult": true, "severity": 0.3, "is_apology": false, "sincerity": 0.2}}',  # двоеточие после маркера
    f'Ответ с extra текстом после JSON {STATE_MARKER} {{"is_insult": true, "severity": 0.3, "is_apology": false, "sincerity": 0.2}} и хвост',
]


def result_to_dict(r) -> dict:
    d = asdict(r)
    d.pop("spans", None)  # spans никогда не устанавливается ни одной из перенесённых функций
    return d


def main() -> int:
    try:
        import yandi_rs.message_intensity as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.message_intensity as mi

    for i, raw in enumerate(RAW_SAMPLES):
        py_visible, py_result = mi.parse_self_report(raw)
        rs_visible, rs_result_dict = rs.parse_self_report(raw)
        py_dict = result_to_dict(py_result)
        check(f"P{i} parse_self_report совпадает (visible)", py_visible == rs_visible,
              f"python={py_visible!r} rust={rs_visible!r}")
        check(f"P{i} parse_self_report совпадает (result)", py_dict == rs_result_dict,
              f"python={py_dict!r} rust={rs_result_dict!r}")

    # ── strip_all_markers напрямую ──
    for i, raw in enumerate(RAW_SAMPLES):
        py_s = mi._strip_all_markers(raw)
        rs_s = rs.strip_all_markers(raw)
        check(f"S{i} _strip_all_markers совпадает", py_s == rs_s, f"python={py_s!r} rust={rs_s!r}")

    # ── intensity_from_state напрямую ──
    STATES = [
        {"is_insult": True, "is_apology": False, "severity": 0.5, "sincerity": 0.3},
        {"is_insult": False, "is_apology": True, "severity": 2.0, "sincerity": -1.0},  # клэмпинг
        {"is_insult": True, "is_apology": False, "severity": 0.5, "sincerity": 0.3, "is_promise": True, "claims_fulfilled": True},
        {"is_insult": True},  # не хватает полей
        {},
        "not a dict",
        123,
        None,
        [1, 2, 3],
    ]
    for i, state in enumerate(STATES):
        py_r = mi.intensity_from_state(state, error="test-error")
        rs_d = rs.intensity_from_state(state, "test-error")
        py_d = result_to_dict(py_r)
        check(f"IS{i} intensity_from_state({state!r}) совпадает", py_d == rs_d, f"python={py_d!r} rust={rs_d!r}")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_MESSAGE_INTENSITY_ENGINE")
    try:
        os.environ.pop("YANDI_MESSAGE_INTENSITY_ENGINE", None)
        mi._rust_mi = None
        check("G0 по умолчанию (без переменной) активен Python-движок", mi._get_rust_mi() is None)

        os.environ["YANDI_MESSAGE_INTENSITY_ENGINE"] = "rust"
        mi._rust_mi = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", mi._get_rust_mi() is rs)

        for i, raw in enumerate(RAW_SAMPLES):
            switch_visible, switch_result = mi.parse_self_report(raw)
            direct_visible, direct_dict = rs.parse_self_report(raw)
            check(f"G2.{i} переключённый parse_self_report совпадает с прямым вызовом Rust",
                  switch_visible == direct_visible and result_to_dict(switch_result) == direct_dict)
            check(f"G2.{i}t возвращённый объект — тот же класс IntensityResult",
                  type(switch_result).__name__ == "IntensityResult")

        os.environ.pop("YANDI_MESSAGE_INTENSITY_ENGINE", None)
        mi._rust_mi = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", mi._get_rust_mi() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_MESSAGE_INTENSITY_ENGINE", None)
        else:
            os.environ["YANDI_MESSAGE_INTENSITY_ENGINE"] = saved_env
        mi._rust_mi = None

    src = (ROOT / "agent" / "message_intensity.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError) и не падает наружу",
          "except ImportError as e:" in src)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
