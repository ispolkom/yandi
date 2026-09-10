"""
llm_gateway/llamacpp_backend_regression_test.py

Two tiers:
  1. Cheap, always-safe checks of the registry/error-reporting surface
     (no GPU, no model load, no llama_cpp import needed to matter).
  2. A real, live generation smoke test against the actual GGUF file —
     only runs if the file exists AND llama_cpp actually imports AND
     LLM_GATEWAY_RUN_LIVE_SMOKE_TEST=1 is set. Skips (not fails)
     otherwise, same "degrade gracefully, never fail the suite for an
     absent optional dependency" discipline as
     claim_priority_regression_test.py's own network-dependency note.

Запуск: python3 -m llm_gateway.llamacpp_backend_regression_test
Живой смоук-тест: LLM_GATEWAY_RUN_LIVE_SMOKE_TEST=1 python3 -m llm_gateway.llamacpp_backend_regression_test
"""
from __future__ import annotations

import os
import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from llm_gateway import llamacpp_backend as be

    check(
        "known model aliases all registered",
        all(m in be._MODEL_REGISTRY for m in ("heretic:q8", "qwen3:14b", "qwen9b:q8")),
    )
    check("unknown model name is not registered", "totally-made-up-model" not in be._MODEL_REGISTRY)
    check(
        "has_model() is False for an unregistered name",
        be.has_model("totally-made-up-model") is False,
    )
    check(
        "the three real aliases all resolve to the SAME gguf path (one file, not three copies)",
        len({be._MODEL_REGISTRY[m].path for m in ("heretic:q8", "qwen3:14b", "qwen9b:q8")}) == 1,
    )

    gguf_path = be._MODEL_REGISTRY["heretic:q8"].path
    file_present = os.path.exists(gguf_path)
    print(f"[info] GGUF file present on disk: {file_present} ({gguf_path})")
    print(f"[info] llama_cpp importable: {be.Llama is not None}"
          + (f" ({be.registry_error()})" if be.Llama is None else ""))

    if not os.environ.get("LLM_GATEWAY_RUN_LIVE_SMOKE_TEST"):
        print("[skip] живой смоук-тест: LLM_GATEWAY_RUN_LIVE_SMOKE_TEST не установлен")
    elif be.Llama is None:
        print(f"[skip] живой смоук-тест: llama_cpp недоступен ({be.registry_error()})")
    elif not file_present:
        print(f"[skip] живой смоук-тест: GGUF-файл не найден ({gguf_path})")
    else:
        text, meta = be.generate(
            "Ответь одним словом: сколько будет 2+2?",
            model="heretic:q8",
            system=None,
            temperature=0.0,
            max_tokens=50,
            response_format=None,
        )
        check("live generation returns non-empty text", bool(text.strip()), repr(text))
        check("live generation reports a done_reason", meta.get("done_reason") in ("stop", "length"), repr(meta))
        print(f"[info] live model said: {text!r}")

        text_json, _ = be.generate(
            'Верни JSON: {"answer": "четыре"}',
            model="heretic:q8",
            system="Отвечай ТОЛЬКО валидным JSON, без пояснений.",
            temperature=0.0,
            max_tokens=50,
            response_format="json",
        )
        import json as _json
        try:
            parsed = _json.loads(text_json)
            check("live generation with response_format=json produces parseable JSON", True)
            check("parsed JSON has expected shape", isinstance(parsed, dict), repr(parsed))
        except Exception as e:
            check("live generation with response_format=json produces parseable JSON", False, f"{e}: {text_json!r}")

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
