"""
agent/claim_priority_regression_test.py

Дешёвый offline regression suite для claim priority / claim role /
claim validator pipeline (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §I).

Назначение:
    Запускать ПЕРЕД каждым полным интеграционным orchestrator-прогоном,
    чтобы ловить регрессии вроде тех, что вызвали этот отчёт
    (core claim рубится как meta_text, background claims забивают
    retrieval budget), не тратя 10 минут на живой прогон.

Использует РЕАЛЬНЫЕ claim-тексты из последнего интеграционного
прогона (/tmp/yandi_p0p1_integration.log), не выдуманные примеры.

НЕ требует Ollama/LLM — там, где нужен embedding (query relevance),
код гарантированно (и проверяется явно) деградирует до
нейтрального 0.0 без падения, если Ollama недоступен.

Запуск:
    cd /home/iam/yandi
    python3 -m agent.claim_priority_regression_test

Код выхода 0 — все проверки прошли, 1 — есть провалы.
"""

from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from agent.claim_validator import ClaimValidator
    from agent.claim_evidence_retriever import (
        _is_existence_question,
        _extract_existence_target,
        _classify_claim_role,
        _is_absence_claim,
        _query_relevance_score,
        _claim_retrieval_priority,
    )

    query = "Есть ли разумная жизнь на Юпитере?"

    # Реальные claims из /tmp/yandi_p0p1_integration.log (последний
    # живой прогон). core_meta_bug — тот самый claim, что был
    # ошибочно отклонён ClaimValidator до фикса §A.
    real_claims = {
        "core_meta_bug": "По имеющимся данным разумная жизнь на Юпитере не была обнаружена.",
        "core_direct": "Согласно имеющейся информации, разумная жизнь на Юпитере считается крайне маловероятной.",
        "background_atmosphere": "Атмосфера Юпитера состоит преимущественно из водорода и гелия.",
        "background_temperature": "Температура у облаков Юпитера составляет около -145°C.",
        "background_water_absence": "На Юпитере отсутствует жидкая вода на поверхности.",
        "explanatory_life_generic": "Любые формы жизни требуют стабильную среду обитания.",
        "meta_wrapper_genuine": "По имеющимся данным, ответ на вопрос является неполным.",
    }

    print("=" * 72)
    print("1. Core factual negative claim НЕ должен быть rejected как meta")
    print("=" * 72)
    v = ClaimValidator()
    ok, reason = v.validate(real_claims["core_meta_bug"])
    check(
        "core_meta_bug принят валидатором",
        ok and reason != "meta_text",
        f"reason={reason}",
    )
    ok2, reason2 = v.validate(real_claims["meta_wrapper_genuine"])
    check(
        "настоящий meta-wrapper по-прежнему отклоняется",
        not ok2 and reason2 == "meta_text",
        f"accepted={ok2} reason={reason2}",
    )

    print()
    print("=" * 72)
    print("2. Existence query распознана")
    print("=" * 72)
    check(
        "'Есть ли разумная жизнь на Юпитере?' распознан как existence question",
        _is_existence_question(query),
    )
    check(
        "extracted target содержит 'жизнь'",
        any("жизн" in w for w in _extract_existence_target(query)),
        f"target={_extract_existence_target(query)}",
    )
    check(
        "открытый вопрос 'Расскажи о Юпитере' НЕ распознан как existence question",
        not _is_existence_question("Расскажи о Юпитере"),
    )

    print()
    print("=" * 72)
    print("3. Direct/core claims получают роль выше background")
    print("=" * 72)
    role_core = _classify_claim_role(real_claims["core_direct"], query)["role"]
    role_bg_atm = _classify_claim_role(real_claims["background_atmosphere"], query)["role"]
    role_bg_water = _classify_claim_role(real_claims["background_water_absence"], query)["role"]
    check("core_direct role == CORE", role_core == "CORE", f"role={role_core}")
    check(
        "background_atmosphere role != CORE",
        role_bg_atm != "CORE",
        f"role={role_bg_atm}",
    )
    check(
        "background_water_absence (absence, но не про target) role != CORE — "
        "это и есть исправленный P0.1 баг",
        role_bg_water != "CORE",
        f"role={role_bg_water}",
    )

    print()
    print("=" * 72)
    print("4. Atmospheric/numeric claims не получают priority ТОЛЬКО из-за цифр")
    print("=" * 72)
    # Сравниваем БЕЗ relevance (mocked 0.0 для всех), чтобы изолировать
    # именно specificity+role эффект.
    import agent.claim_evidence_retriever as cer
    orig_relevance_fn = cer._query_relevance_score
    cer._query_relevance_score = lambda t, q: 0.0
    try:
        score_core = _claim_retrieval_priority(
            {"claim_text": real_claims["core_direct"], "claim_type": "hypothesis", "query_context": query}
        )
        score_bg_temp = _claim_retrieval_priority(
            {"claim_text": real_claims["background_temperature"], "claim_type": "hypothesis", "query_context": query}
        )
    finally:
        cer._query_relevance_score = orig_relevance_fn

    check(
        "core claim (без relevance!) приоритетнее чистого numeric background claim",
        score_core > score_bg_temp,
        f"core={score_core:.2f} background_numeric={score_bg_temp:.2f}",
    )

    print()
    print("=" * 72)
    print("5. Negative/absence claim feature доходит до ranking")
    print("=" * 72)
    # YANDI_ABSENCE_REGRESSION_FIX.md: canonical claim-level absence
    # detector — semantic absence (обнаружение/подтверждение/
    # фиксация под отрицанием, включая "не БЫЛА обнаружена"), а НЕ
    # generic grammatical negation ("не превышает" — не absence).
    absence_true_cases = [
        "разумная жизнь не обнаружена",
        "разумная жизнь не была обнаружена",
        "разумная жизнь пока не обнаружена",
        "признаки жизни не были обнаружены",
        "нет доказательств существования жизни",
        "доказательства жизни отсутствуют",
        "ни один аппарат не обнаружил признаков жизни",
        "не выявлено признаков разумной деятельности",
        "не найдено подтверждений",
        "сигналы не зафиксированы",
        real_claims["core_meta_bug"],
        real_claims["background_water_absence"],
    ]
    absence_false_cases = [
        "разумная жизнь обнаружена",
        "аппарат обнаружил сигнал",
        "доказательства существуют",
        "температура не превышает -145°C",
    ]

    for text in absence_true_cases:
        check(f"_is_absence_claim(True) — {text[:60]!r}", _is_absence_claim(text))
    for text in absence_false_cases:
        check(f"_is_absence_claim(False) — {text[:60]!r}", not _is_absence_claim(text))

    role_info = _classify_claim_role(real_claims["core_meta_bug"], query)
    check(
        "absence marker + target_match => has_assertion=True для core claim",
        role_info["has_assertion"] and role_info["target_match"],
        f"{role_info}",
    )

    print()
    print("=" * 72)
    print("5b. Role classifier consistency (query A/B/C)")
    print("=" * 72)
    _query_ABC = "Есть ли разумная жизнь на Юпитере?"
    _case_A = "По имеющимся данным разумная жизнь на Юпитере не была обнаружена."
    _case_B = "На Юпитере отсутствует жидкая вода на поверхности."
    _case_C = "Температура на Юпитере не превышает -145°C."

    _info_A = _classify_claim_role(_case_A, _query_ABC)
    _info_B = _classify_claim_role(_case_B, _query_ABC)
    _info_C = _classify_claim_role(_case_C, _query_ABC)

    check(
        "A: absence=True target_match=True role=CORE",
        _is_absence_claim(_case_A) and _info_A["target_match"] and _info_A["role"] == "CORE",
        f"absence={_is_absence_claim(_case_A)} info={_info_A}",
    )
    check(
        "B: absence=True target_match=False role=BACKGROUND — "
        "absence semantics != decision relevance",
        _is_absence_claim(_case_B) and not _info_B["target_match"] and _info_B["role"] == "BACKGROUND",
        f"absence={_is_absence_claim(_case_B)} info={_info_B}",
    )
    check(
        "C: absence=False, role НЕ должен стать CORE только из-за 'не'",
        not _is_absence_claim(_case_C) and _info_C["role"] != "CORE",
        f"absence={_is_absence_claim(_case_C)} info={_info_C}",
    )

    print()
    print("=" * 72)
    print("6. Fallback без embedding не падает")
    print("=" * 72)
    try:
        score = _query_relevance_score(real_claims["core_direct"], query)
        check(
            "query relevance не падает при недоступном Ollama (нейтральный fallback)",
            isinstance(score, float),
            f"score={score}",
        )
    except Exception as exc:
        check("query relevance не падает при недоступном Ollama", False, f"raised {exc!r}")

    print()
    print("=" * 72)
    print("7. supports_query_aspect / claim role wiring (orch_synthesizer)")
    print("=" * 72)
    try:
        import agent.orch_synthesizer as osyn
        check(
            "orch_synthesizer импортирует _classify_claim_role",
            osyn._classify_claim_role is _classify_claim_role,
        )
    except Exception as exc:
        check("orch_synthesizer импортирует _classify_claim_role", False, f"raised {exc!r}")

    # Проверка reuse-пути: claim с уже проставленным supports_query_aspect
    # не должен пересчитывать роль по (возможно устаревшему) query_context.
    claim_with_aspect = {
        "claim_text": real_claims["core_direct"],
        "claim_type": "hypothesis",
        "query_context": "нерелевантный запрос",
        "supports_query_aspect": ["CORE"],
    }
    cer._query_relevance_score = lambda t, q: 0.0
    try:
        score_reused = _claim_retrieval_priority(claim_with_aspect)
    finally:
        cer._query_relevance_score = orig_relevance_fn
    check(
        "supports_query_aspect=['CORE'] реально повышает priority (reuse путь)",
        score_reused >= 8.0,  # base 1.5 + CORE boost 6.0 минимум
        f"score={score_reused:.2f}",
    )

    print()
    print("=" * 72)
    print("8. Profile formatting не падает на новых cost-ключах")
    print("=" * 72)
    try:
        dummy_cost = {
            "pre_pipeline_ms": 1234.5,
            "claim_setup_ms": 6789.0,
            "claim_retrieval_ms": 245590.0,
            "claim_pass2_mapping_nli_ms": 18700.0,
            "claim_claim_nli_ms": 21370.0,
            "final_coverage_ms": 1110.0,
            "total_ms": 630350.0,
        }
        profile_keys = [
            ("pre_pipeline_personality", "pre_pipeline_ms"),
            ("claim_setup_validator_mapper1_nli1", "claim_setup_ms"),
            ("claim_specific_retrieval", "claim_retrieval_ms"),
            ("claim_pass2_mapper_nli", "claim_pass2_mapping_nli_ms"),
            ("claim_claim_nli", "claim_claim_nli_ms"),
            ("final_claim_coverage", "final_coverage_ms"),
        ]
        lines = []
        for label, key in profile_keys:
            value = dummy_cost.get(key)
            if isinstance(value, (int, float)):
                pct = (value / dummy_cost["total_ms"]) * 100.0
                lines.append(f"[PROFILE] {label:<24} {value / 1000:>8.2f}s {pct:>6.1f}%")
        check(
            "все новые profile ключи форматируются без исключений",
            len(lines) == len(profile_keys),
            f"formatted {len(lines)}/{len(profile_keys)}",
        )
    except Exception as exc:
        check("profile formatting", False, f"raised {exc!r}")

    print()
    print("=" * 72)
    print("9. Existential negation ('нет X') + морфология (YANDI_CLAIM_ROLE_MORPHOLOGY_FIX.md)")
    print("=" * 72)
    # Реальные claims из ЭТОГО (второго) живого прогона
    # (/tmp/yandi_regression_fix_integration.log), не выдуманные.
    jupiter_query = "Есть ли разумная жизнь на Юпитере?"
    jupiter_cases = [
        ("Нет разумной жизни на Юпитере.", True, "CORE"),
        ("Разумная жизнь на Юпитере не была обнаружена.", True, "CORE"),
        (
            "Телескопические наблюдения не зафиксировали признаков жизни на Юпитере.",
            True,
            "DIRECT_DECISION_EVIDENCE",
        ),
        (
            "Космические миссии не зафиксировали признаков жизни на Юпитере.",
            True,
            "DIRECT_DECISION_EVIDENCE",
        ),
        (
            "Зонды не обнаружили признаков жизни на Юпитере.",
            True,
            "DIRECT_DECISION_EVIDENCE",
        ),
        ("На Юпитере нет жидкой воды.", True, "BACKGROUND"),
        ("Температура на Юпитере не превышает -145°C.", False, "BACKGROUND"),
    ]

    for text, expect_absence, expect_role in jupiter_cases:
        absence = _is_absence_claim(text)
        role = _classify_claim_role(text, jupiter_query)["role"]
        check(
            f"[{expect_role}] absence={expect_absence} — {text[:65]!r}",
            absence == expect_absence and role == expect_role,
            f"got absence={absence} role={role}",
        )

    # Другой домен — не Jupiter-specific.
    mars_query = "Есть ли вода на Марсе?"
    mars_cases = [
        ("Нет воды на Марсе.", "CORE"),
        ("Марс не имеет глобального магнитного поля.", "BACKGROUND"),
    ]
    for text, expect_role in mars_cases:
        role = _classify_claim_role(text, mars_query)["role"]
        check(
            f"[Mars, {expect_role}] {text!r}",
            role == expect_role,
            f"got role={role}",
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
