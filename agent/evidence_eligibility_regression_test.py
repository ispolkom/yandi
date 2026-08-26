"""
agent/evidence_eligibility_regression_test.py

Дешёвый offline regression suite для Evidence Eligibility / Directness /
Registry Provenance (YANDI_EVIDENCE_ELIGIBILITY_AND_REGISTRY_AUDIT.md).

НЕ требует Ollama/web — embedding-зависимые функции при недоступном
Ollama детерминированно деградируют до 0.0 (проверяется явно), а
composite-логика Claim Status gate тестируется как воспроизведённая
копия реального кода (тот же паттерн, что и в предыдущих regression
suite этой сессии — orchestrator_v2.py слишком велик и требует bs4
для полного импорта в этой sandbox).

Запуск:
    cd /home/iam/yandi
    python3 -m agent.evidence_eligibility_regression_test
"""

from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# Воспроизведение реальной composite-логики Claim Status gate
# (orchestrator_v2.py) для offline-тестирования.
HARD_BLOCKED_SOURCE_CLASSES = {
    "generated_pipeline", "social", "forum",
    "blog_opinion", "speculative", "news", "popular_article",
}
DIRECTNESS_SUPPORT_THRESHOLD = 0.60


def counts_toward_status(rel: dict) -> "tuple[bool, str | None]":
    if rel.get("evidence_role") == "direct" and rel.get("evidence_eligible") is True:
        return True, "authority"
    if (
        rel.get("source_class") not in HARD_BLOCKED_SOURCE_CLASSES
        and rel.get("retrieval_origin") != "local_registry"
        and float(rel.get("directness", 0.0) or 0.0) >= DIRECTNESS_SUPPORT_THRESHOLD
    ):
        return True, "directness"
    return False, None


def main() -> int:
    from agent.source_quality import evaluate_source_quality, evaluate_evidence_directness

    print("=" * 72)
    print("A. source_quality max-quality table (математическая проверка)")
    print("=" * 72)

    # .gov -> primary
    q = evaluate_source_quality(url="https://science.nasa.gov/x", title="T" * 10, text="X" * 1500, source_type="web")
    check("primary (.gov) достигает eligible=True", q.evidence_eligible is True, f"quality={q.quality_score}")
    check("primary role=direct", q.evidence_role == "direct")

    # wikipedia -> reference
    q = evaluate_source_quality(url="https://en.wikipedia.org/wiki/X", title="T" * 10, text="X" * 1500, source_type="web")
    check("reference (wikipedia) достигает eligible=True", q.evidence_eligible is True, f"quality={q.quality_score}")

    # unknown domain, max possible traceability
    q = evaluate_source_quality(url="https://random-example-domain.test/x", title="T" * 10, text="X" * 1500, source_type="web")
    check(
        "unknown domain НИКОГДА не пересекает eligibility threshold "
        "(математический потолок ~0.655 < 0.70)",
        q.evidence_eligible is False,
        f"quality={q.quality_score} class={q.source_class}",
    )
    check("unknown domain role=context (не direct)", q.evidence_role == "context")

    # local registry (no URL)
    q = evaluate_source_quality(url="", title="Registry doc", text="X" * 1500, source_type="local")
    check(
        "local registry (без URL) хуже, чем anonymous unknown web "
        "(authority=0.25/primaryness=0.20 vs 0.50/0.40)",
        q.evidence_eligible is False,
        f"quality={q.quality_score}",
    )

    # forum -> hard blocked regardless of content
    q = evaluate_source_quality(url="https://reddit.com/r/x/y", title="T" * 10, text="X" * 1500, source_type="web")
    check("forum НИКОГДА не eligible (blocked_classes)", q.evidence_eligible is False)

    print()
    print("=" * 72)
    print("1-8. Claim Status composite gate (authority OR directness)")
    print("=" * 72)

    # 1. authoritative + direct + supports -> countable
    check(
        "1. authoritative+direct+eligible -> counted via=authority",
        counts_toward_status({"evidence_role": "direct", "evidence_eligible": True, "source_class": "scientific", "directness": 0.1})
        == (True, "authority"),
    )

    # 2. authoritative but relation=unrelated -> counted (role/eligible gate
    #    only), НО supports_count не вырастет (relation-фильтр отдельно,
    #    ниже по коду, не в counts_toward_status — существующее поведение).
    counted, via = counts_toward_status({"evidence_role": "direct", "evidence_eligible": True, "source_class": "scientific", "directness": 0.0})
    check("2. authoritative (role/eligible) countable независимо от relation", counted is True and via == "authority")

    # 3. unknown domain + strong direct evidence -> новое поведение (directness)
    check(
        "3. unknown domain + directness>=0.60 -> counted via=directness",
        counts_toward_status({"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.75})
        == (True, "directness"),
    )

    # 4. unknown domain + vague context -> not eligible
    check(
        "4. unknown domain + directness<0.60 -> НЕ counted",
        counts_toward_status({"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.40})
        == (False, None),
    )

    # 5. local registry без provenance -> НЕ доверяем автоматически,
    #    даже с максимальной directness.
    check(
        "5. local_registry + directness=0.95 -> ВСЁ РАВНО НЕ counted "
        "(P0-E: registry = прошлые UNVERIFIED ответы модели)",
        counts_toward_status({"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.95, "retrieval_origin": "local_registry"})
        == (False, None),
    )

    # 6. local registry WITH "validated provenance" — архитектура НЕ
    #    поддерживает эту метку вообще (нет такого поля нигде в
    #    orch_registry_search.py/evidence_pool.py — подтверждено
    #    аудитом). Документируем это явно, а не притворяемся, что оно
    #    работает.
    print(
        "    6. [ДОКУМЕНТИРОВАНО, НЕ ТЕСТ] registry-записи не несут "
        "provenance-метки вообще (_extract_docs_from_file() жёстко "
        "ставит trust_level='UNVERIFIED' для 100% записей, других "
        "полей provenance не существует) — 'validated provenance' "
        "физически нечего проверить в текущей схеме данных."
    )

    # 7. eligible=False + supports -> Claim Status НЕ увеличивает
    #    supports, ЕСЛИ ТАКЖЕ и directness не проходит порог (старый
    #    инвариант сохранён для слабых случаев).
    check(
        "7. eligible=False, directness=0.30 (низкий) -> НЕ counted "
        "(старый инвариант сохранён)",
        counts_toward_status({"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.30})
        == (False, None),
    )

    # 8. direct evidence vs source authority — независимые оси:
    #    blocked-класс с ОЧЕНЬ высокой directness всё равно не в счёт
    #    (authority per class остаётся жёстким гейтом), а unknown-класс
    #    с высокой directness — да (доказывает, что оси не слиты в одну).
    check(
        "8a. forum (blocked) + directness=0.99 -> НЕ counted "
        "(authority-гейт доминирует над directness)",
        counts_toward_status({"evidence_role": "context", "evidence_eligible": False, "source_class": "forum", "directness": 0.99})
        == (False, None),
    )
    check(
        "8b. unknown (не blocked) + directness=0.99 -> counted "
        "(доказывает: оси независимы, не одна общая формула)",
        counts_toward_status({"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.99})
        == (True, "directness"),
    )

    print()
    print("=" * 72)
    print("9. Final Coverage parser robustness")
    print("=" * 72)

    from unittest.mock import patch
    import agent.final_claim_coverage as fcc

    def extract(raw, done_reason="stop"):
        # P0-B (autonomous fix pass): final_claim_coverage.py now uses its
        # own dedicated _call_ollama_for_extraction() (dict response with
        # done_reason/eval_count metadata), not the shared string-returning
        # _call_ollama() from orch_web_query.py.
        mock_gen = {"response": raw, "done_reason": done_reason, "eval_count": 42, "num_predict": 2000}
        with patch("agent.final_claim_coverage._call_ollama_for_extraction", lambda p: mock_gen):
            return fcc.extract_final_claims("Длинный содержательный ответ " * 10)

    fenced = (
        "Вот результат:\n```json\n"
        '{"claims": [{"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"}]}'
        "\n```\n"
    )
    claims, status = extract(fenced)
    check("9a. fenced JSON блок распознаётся", status == "ok" and len(claims) == 1)

    prose = 'Вот claims: {"claims": [{"claim_text": "Атмосфера содержит водород.", "claim_type": "factual"}]}'
    claims, status = extract(prose)
    check("9b. prose перед JSON распознаётся", status == "ok" and len(claims) == 1)

    malformed = '{"claims": [{"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},]}'
    claims, status = extract(malformed)
    check("9c. trailing comma исправляется", status == "ok" and len(claims) == 1)

    empty = '{"claims": []}'
    claims, status = extract(empty)
    check("9d. валидный пустой JSON -> status=ok, 0 claims", status == "ok" and len(claims) == 0)

    garbage = "Извините, не могу выполнить это прямо сейчас."
    claims, status = extract(garbage)
    check("9e. неразбираемый garbage -> status=parse_error (не молчаливый успех)", status == "parse_error")

    print()
    print("=" * 72)
    print("10. Negative observational claim — directness как компенсация")
    print("=" * 72)

    # Демонстрация: absence-claim ("не обнаружено X") с evidence с
    # unknown-домена, но passage которого СЕМАНТИЧЕСКИ прямо отвечает
    # на claim, теперь МОЖЕТ засчитаться (через directness) — раньше
    # не могло НИ ПРИ КАКИХ обстоятельствах (математический потолок).
    # Сам NLI/claim_relation.py НЕ менялся — только Claim Status gate.
    absence_rel_low_directness = {"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.35, "relation": "uncertain"}
    absence_rel_high_directness = {"evidence_role": "context", "evidence_eligible": False, "source_class": "unknown", "directness": 0.70, "relation": "supports"}

    counted_low, _ = counts_toward_status(absence_rel_low_directness)
    counted_high, via_high = counts_toward_status(absence_rel_high_directness)

    check(
        "10a. absence-claim, generic/uncertain passage -> по-прежнему НЕ counted "
        "(directness low — не изменилось поведение для генерик-контента)",
        counted_low is False,
    )
    check(
        "10b. absence-claim, passage с высокой directness И relation=supports "
        "-> теперь МОЖЕТ засчитаться (раньше — никогда)",
        counted_high is True and via_high == "directness",
    )
    print(
        "    ПРИМЕЧАНИЕ: directness сама по себе не гарантирует, что "
        "NLI вернёт relation=supports для absence-claims — это "
        "по-прежнему решает NLI (не изменён). Directness лишь снимает "
        "МАТЕМАТИЧЕСКИ НЕДОСТИЖИМЫЙ eligibility-барьер для unknown-"
        "доменов, если NLI УЖЕ сказал supports/contradicts."
    )

    print()
    print("=" * 72)
    print("Directness graceful degradation (детерминированный mock, НЕ зависит "
          "от того, реально ли Ollama запущен в этом окружении)")
    print("=" * 72)

    # ВАЖНО: просто вызвать evaluate_evidence_directness() и ожидать 0.0
    # НЕЛЬЗЯ — если Ollama реально запущен (как в project venv), функция
    # корректно вернёт настоящий cosine similarity (что и произошло:
    # got 0.5500... для двух generic строк "тестовый claim"/"тестовый
    # passage" — это правильное поведение, не баг). Чтобы протестировать
    # именно fallback-ветку (except -> 0.0), сеть детерминированно рвём
    # мокой, а не полагаемся на состояние машины.
    import requests as _requests_for_mock

    def _raise_connection_error(*_args, **_kwargs):
        raise _requests_for_mock.exceptions.ConnectionError(
            "simulated: ollama unreachable"
        )

    with patch("requests.Session.post", side_effect=_raise_connection_error):
        d = evaluate_evidence_directness("тестовый claim", "тестовый passage")
    check(
        "directness возвращает 0.0 (не падает) при принудительно недоступном "
        "Ollama (мокнутый ConnectionError)",
        d == 0.0,
        f"got {d}",
    )

    print()
    print("=" * 72)
    print("Live embedding sanity check (НЕ regression gate — пропускается, "
          "если Ollama недоступен в этом окружении)")
    print("=" * 72)

    claim = "Разумная жизнь на Юпитере не обнаружена."
    passage_direct = (
        "Никаких подтверждённых признаков разумной жизни на Юпитере "
        "обнаружено не было."
    )
    passage_unrelated = "Температура поверхности Марса изменяется в течение суток."

    d_direct = evaluate_evidence_directness(claim, passage_direct)
    d_unrelated = evaluate_evidence_directness(claim, passage_unrelated)

    if d_direct == 0.0 and d_unrelated == 0.0:
        print(
            "    [SKIP] Ollama недоступен в этом окружении (оба вызова "
            "вернули fallback 0.0) — sanity check не может ничего "
            "показать здесь, это НЕ провал теста."
        )
    else:
        check(
            "live sanity: directness(direct passage) > directness(unrelated passage)",
            d_direct > d_unrelated,
            f"direct={d_direct:.4f} unrelated={d_unrelated:.4f}",
        )

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
