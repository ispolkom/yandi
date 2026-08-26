"""
agent/final_claim_extraction_regression_test.py — P0-B regression.

Deterministic, no live Ollama required. Covers:
    - fenced JSON
    - prose + JSON (no fence)
    - prose + fenced JSON
    - trailing comma
    - truncated JSON (fence opened, never closed) -> must be reported
      as generation_truncated when Ollama signals done_reason="length",
      NOT a generic parse_error
    - malformed garbage (no JSON at all, done_reason="stop") -> stays
      parse_error, must NOT be misreported as generation_truncated
    - 20+ claims (task-specific token budget, not the 500-token
      conductor budget final_claim_coverage used to inherit)

Run: /home/iam/venv/bin/python3 -m agent.final_claim_extraction_regression_test
"""

from unittest.mock import patch

from agent.final_claim_coverage import (
    _extract_json,
    _format_hint,
    _json_completeness_indicator,
    extract_final_claims,
    evaluate_final_claim_coverage,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def _mock_gen(response: str, done_reason: str = "stop", eval_count: int = 42):
    return {
        "response": response,
        "done_reason": done_reason,
        "eval_count": eval_count,
        "num_predict": 2000,
    }


# ── 1. Fenced JSON (clean) ──

fenced = '''```json
{
  "claims": [
    {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},
    {"claim_text": "Атмосфера Юпитера содержит водород.", "claim_type": "factual"}
  ]
}
```'''
data = _extract_json(fenced)
check("fenced JSON parses", bool(data) and len(data.get("claims", [])) == 2, f"{data}")

# ── 2. Prose + JSON, no fence ──

prose_no_fence = '''Вот извлечённые claims:

{
  "claims": [
    {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"}
  ]
}'''
data = _extract_json(prose_no_fence)
check("prose + JSON (no fence) parses", bool(data) and len(data.get("claims", [])) == 1, f"{data}")

# ── 3. Prose + fenced JSON ──

prose_fenced = '''Вот извлечённые claims в формате JSON:

```json
{
  "claims": [
    {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},
    {"claim_text": "Атмосфера Юпитера содержит водород.", "claim_type": "factual"}
  ]
}
```

Надеюсь, это поможет!'''
data = _extract_json(prose_fenced)
check("prose + fenced JSON parses", bool(data) and len(data.get("claims", [])) == 2, f"{data}")
hint = _format_hint(prose_fenced)
check(
    "format_hint tags prose+fence combination correctly",
    "has_code_fence" in hint and "prose_before_json" in hint,
    f"hint={hint}",
)

# ── 4. Trailing comma ──

trailing_comma = '''{
  "claims": [
    {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},
  ],
}'''
data = _extract_json(trailing_comma)
check("trailing comma sanitized and parses", bool(data) and len(data.get("claims", [])) == 1, f"{data}")

# ── 5. Truncated JSON (fence opened, never closed) ──
# Reproduces the exact live-log signature: format_hint=has_code_fence+prose_before_json,
# but here we ALSO drive it through extract_final_claims() with a mocked
# done_reason="length" to prove the status split works end-to-end.

truncated = '''Вот извлечённые claims в формате JSON:

```json
{
  "claims": [
    {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},
    {"claim_text": "Атмосфера Юпитера содержит водород.", "claim_type": "factual"}
'''
data = _extract_json(truncated)
check("truncated JSON fails to parse (as expected, not silently patched)", data == {}, f"{data}")
check(
    "json_completeness_indicator flags truncated text as incomplete",
    _json_completeness_indicator(truncated) == "incomplete",
)

with patch(
    "agent.final_claim_coverage._call_ollama_for_extraction",
    return_value=_mock_gen(truncated, done_reason="length", eval_count=2000),
):
    claims, status = extract_final_claims("dummy final answer text")
    check(
        "extract_final_claims: done_reason=length -> status=generation_truncated (NOT parse_error)",
        status == "generation_truncated",
        f"got status={status}",
    )
    check("extract_final_claims: truncated -> claims=[]", claims == [])

# ── 6. Malformed garbage, no JSON at all, done_reason=stop ──

garbage = "Извини, я не смогла обработать этот запрос корректно. Попробуй переформулировать."

with patch(
    "agent.final_claim_coverage._call_ollama_for_extraction",
    return_value=_mock_gen(garbage, done_reason="stop"),
):
    claims, status = extract_final_claims("dummy final answer text")
    check(
        "extract_final_claims: malformed garbage + done_reason=stop -> stays parse_error "
        "(NOT misreported as generation_truncated)",
        status == "parse_error",
        f"got status={status}",
    )

# ── 7. 20+ claims (task-specific budget, not the 500-token conductor budget) ──

many_claims = {
    "claims": [
        {"claim_text": f"Тестовое утверждение номер {i} про Юпитер и его свойства.", "claim_type": "factual"}
        for i in range(25)
    ]
}
import json as _json
many_claims_text = "```json\n" + _json.dumps(many_claims, ensure_ascii=False) + "\n```"

with patch(
    "agent.final_claim_coverage._call_ollama_for_extraction",
    return_value=_mock_gen(many_claims_text, done_reason="stop"),
):
    claims, status = extract_final_claims("dummy final answer text")
    check("extract_final_claims: 20+ claims -> status=ok", status == "ok", f"got status={status}")
    check("extract_final_claims: 20+ claims -> all 25 recovered", len(claims) == 25, f"got {len(claims)}")

# ── 8. Coverage evaluator treats generation_truncated exactly like other error statuses ──
# (not touched by this pass — verified empirically, not just by reading the code)

with patch(
    "agent.final_claim_coverage._call_ollama_for_extraction",
    return_value=_mock_gen(truncated, done_reason="length"),
):
    result = evaluate_final_claim_coverage(
        answer="Достаточно длинный финальный ответ " * 20,
        pipeline_claims=[],
    )
    check(
        "evaluate_final_claim_coverage: generation_truncated -> coverage_score=0.0 "
        "(not vacuous 1.0)",
        result.coverage_score == 0.0,
        f"got score={result.coverage_score} status={result.coverage_status}",
    )
    check(
        "evaluate_final_claim_coverage: generation_truncated -> coverage_status=extraction_error",
        result.coverage_status == "extraction_error",
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
