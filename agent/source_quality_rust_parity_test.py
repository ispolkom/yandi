"""
agent/source_quality_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/source_quality.rs::evaluate_source_quality даёт ПОСТРОЧНО ТЕ ЖЕ ответы,
что и agent/source_quality.py::evaluate_source_quality, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Пятый шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.

evaluate_evidence_directness() НЕ сравнивается здесь — она делает настоящий embedding-вызов
(сеть) и НЕ была перенесена (см. модульный docstring source_quality.py).

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.source_quality_rust_parity_test
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


LONG_TEXT = "A" * 1500
SHORT_TEXT = "A" * 50
MED_TEXT = "A" * 300

# (url, title, text, source_type)
CASES: list[tuple[str, str, str, str]] = [
    ("https://www.nature.com/articles/test", "Scientific paper", LONG_TEXT, "web"),
    ("https://science.nasa.gov/x", "T" * 10, LONG_TEXT, "web"),
    ("https://en.wikipedia.org/wiki/Jupiter", "Jupiter", LONG_TEXT, "web"),
    ("https://random-blog.example/post", "My opinion", LONG_TEXT, "web"),
    ("https://reddit.com/r/space/test", "Discussion", LONG_TEXT, "web"),
    ("https://unknown.example/article", "Unknown source", LONG_TEXT, "web"),
    ("", "", "generated answer", "generated_pipeline"),
    ("https://random-example-domain.test/x", "T" * 10, LONG_TEXT, "web"),
    ("", "Registry doc", LONG_TEXT, "local"),
    ("https://reddit.com/r/x/y", "T" * 10, LONG_TEXT, "web"),
    # ── .gov / .edu варианты (без явного домена из списков) ──
    ("https://www.someagency.gov/report", "Report", LONG_TEXT, "web"),
    ("https://data.gov.uk/dataset/x", "Dataset", LONG_TEXT, "web"),
    ("https://research.university.edu/paper", "Paper", LONG_TEXT, "web"),
    # ── контентные маркеры (форум/блог/спекулятивные/научные/новости) ──
    ("https://unknown.example/x", "Обсуждение на форуме", "форум комментарии пользователей " + LONG_TEXT, "web"),
    ("https://unknown.example/x", "Мой блог", "личный блог мое мнение " + LONG_TEXT, "web"),
    ("https://unknown.example/x", "Контакт с высшим разумом", "высший разум телепат астраль " + LONG_TEXT, "web"),
    ("https://unknown.example/x", "Обычная статья", "doi: 10.1/x peer-reviewed abstract references " + LONG_TEXT, "web"),
    ("https://unknown.example/x", "Новости", "информационное агентство редакция новости " + LONG_TEXT, "web"),
    # ── один случайный спекулятивный маркер в длинном тексте — НЕ должен сработать ──
    ("https://unknown.example/x", "Обычная статья", "в тексте упоминается ченнелинг мимоходом " + LONG_TEXT, "web"),
    # ── сильный спекулятивный маркер прямо в title/url ──
    ("https://unknown.example/x", "ченнелинг с высшим разумом", LONG_TEXT, "web"),
    # ── traceability границы ──
    ("https://unknown.example/x", "", "", "web"),
    ("https://unknown.example/x", "abcd", MED_TEXT, "web"),   # title < 5 символов
    ("https://unknown.example/x", "abcde", MED_TEXT, "web"),  # title ровно 5
    ("https://unknown.example/x", "T" * 10, "x" * 199, "web"),   # text чуть меньше 200
    ("https://unknown.example/x", "T" * 10, "x" * 200, "web"),   # text ровно 200
    ("https://unknown.example/x", "T" * 10, "x" * 999, "web"),   # text чуть меньше 1000
    ("https://unknown.example/x", "T" * 10, "x" * 1000, "web"),  # text ровно 1000
    # ── источник без URL вообще ──
    ("", "T" * 10, LONG_TEXT, "web"),
    # ── source_type "generated" в другом регистре/с примесью ──
    ("https://unknown.example/x", "T" * 10, LONG_TEXT, "GENERATED_PIPELINE"),
    ("https://unknown.example/x", "T" * 10, LONG_TEXT, "auto-generated"),
    # ── социальные сети ──
    ("https://vk.com/wall123", "Пост", LONG_TEXT, "web"),
    ("https://x.com/user/status/1", "Твит", LONG_TEXT, "web"),
    # ── blog/forum по URL без явного домена из списков ──
    ("https://myblog.example/post/1", "Заметка", SHORT_TEXT, "web"),
    ("https://someforum.example/thread/1", "Тред", SHORT_TEXT, "web"),
    # ── www. префикс снимается корректно ──
    ("https://www.reddit.com/r/x", "T" * 10, LONG_TEXT, "web"),
    # ── странные/пустые URL (терпимость _hostname) ──
    ("not-a-url-at-all", "T" * 10, LONG_TEXT, "web"),
    ("//protocol-relative.example/x", "T" * 10, LONG_TEXT, "web"),
    ("https://user:pass@example.com:8080/path", "T" * 10, LONG_TEXT, "web"),
]


def result_to_dict(r) -> dict:
    return {
        "quality_score": r.quality_score,
        "source_class": r.source_class,
        "evidence_eligible": r.evidence_eligible,
        "evidence_role": r.evidence_role,
        "authority": r.authority,
        "traceability": r.traceability,
        "primaryness": r.primaryness,
        "reasons": r.reasons,
    }


def main() -> int:
    try:
        import yandi_rs.source_quality as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.source_quality as py

    for i, (url, title, text, source_type) in enumerate(CASES):
        py_result = result_to_dict(py.evaluate_source_quality(url, title, text, source_type))
        rs_result = rs.evaluate_source_quality(url, title, text, source_type)
        check(f"C{i} evaluate_source_quality совпадает: url={url!r} type={source_type!r}",
              py_result == rs_result, f"python={py_result!r} rust={rs_result!r}")

        py_host = py._hostname(url)
        rs_host = rs.hostname(url)
        check(f"H{i} _hostname({url!r}) совпадает", py_host == rs_host, f"python={py_host!r} rust={rs_host!r}")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_SOURCE_QUALITY_ENGINE")
    try:
        os.environ.pop("YANDI_SOURCE_QUALITY_ENGINE", None)
        py._rust_sq = None
        check("G0 по умолчанию (без переменной) активен Python-движок", py._get_rust_sq() is None)

        os.environ["YANDI_SOURCE_QUALITY_ENGINE"] = "rust"
        py._rust_sq = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", py._get_rust_sq() is rs)

        for i, (url, title, text, source_type) in enumerate(CASES):
            via_switch = result_to_dict(py.evaluate_source_quality(url, title, text, source_type))
            direct_rust = rs.evaluate_source_quality(url, title, text, source_type)
            check(f"G2.{i} переключённый evaluate_source_quality совпадает с прямым вызовом Rust",
                  via_switch == direct_rust)
            # тип возвращаемого значения остаётся ТЕМ ЖЕ Python-датаклассом
            check(f"G2.{i}t возвращённый объект — тот же класс SourceQualityResult",
                  type(py.evaluate_source_quality(url, title, text, source_type)).__name__ == "SourceQualityResult")

        os.environ.pop("YANDI_SOURCE_QUALITY_ENGINE", None)
        py._rust_sq = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", py._get_rust_sq() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_SOURCE_QUALITY_ENGINE", None)
        else:
            os.environ["YANDI_SOURCE_QUALITY_ENGINE"] = saved_env
        py._rust_sq = None

    src = (ROOT / "agent" / "source_quality.py").read_text(encoding="utf-8")
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
