"""
llm_gateway/vector_space_regression_test.py

Проверяет vector_space.py в изоляции: fingerprint детерминирован,
разные поля дают разные fingerprint'ы, UNKNOWN_VECTOR_SPACE никогда не
совместим ни с чем (даже с самим собой), round-trip to_dict/from_dict.

Запуск: python3 -m llm_gateway.vector_space_regression_test
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
    from llm_gateway import vector_space as vs

    a = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="text-embedding-3-small", dimension=1536, normalized=False)
    a2 = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="text-embedding-3-small", dimension=1536, normalized=False)
    b_diff_model = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="text-embedding-3-large", dimension=1536, normalized=False)
    b_diff_dim = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="text-embedding-3-small", dimension=3072, normalized=False)
    b_diff_backend = vs.VectorSpaceId(backend="llamacpp", protocol="llamacpp-embed", model="text-embedding-3-small", dimension=1536, normalized=False)
    b_diff_protocol = vs.VectorSpaceId(backend="remote", protocol="remote-anthropic-embeddings", model="text-embedding-3-small", dimension=1536, normalized=False)
    b_diff_norm = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="text-embedding-3-small", dimension=1536, normalized=True)

    check("same fields -> identical fingerprint", a.fingerprint() == a2.fingerprint())
    check("compatible(a, a2) -> True (same space)", vs.compatible(a, a2) is True)
    check("different model -> different fingerprint", a.fingerprint() != b_diff_model.fingerprint())
    check("different dimension -> different fingerprint", a.fingerprint() != b_diff_dim.fingerprint())
    check("SAME dimension, different model -> NOT compatible (dimension alone proves nothing)", vs.compatible(a, b_diff_model) is False)
    check("different backend -> different fingerprint, not compatible", vs.compatible(a, b_diff_backend) is False)
    check("different protocol (same backend/model/dim) -> not compatible", vs.compatible(a, b_diff_protocol) is False)
    check("different normalized flag -> not compatible", vs.compatible(a, b_diff_norm) is False)

    check("None on either side -> never compatible", vs.compatible(None, a) is False and vs.compatible(a, None) is False)
    check("UNKNOWN vs a real space -> never compatible", vs.compatible(vs.UNKNOWN_VECTOR_SPACE, a) is False)
    check(
        "UNKNOWN vs UNKNOWN -> STILL never compatible (can't prove same unknown origin)",
        vs.compatible(vs.UNKNOWN_VECTOR_SPACE, vs.UNKNOWN_VECTOR_SPACE) is False,
    )

    d = a.to_dict()
    restored = vs.VectorSpaceId.from_dict(d)
    check("to_dict/from_dict round-trips to an identical fingerprint", restored.fingerprint() == a.fingerprint())
    check("to_dict includes a human-checkable fingerprint field", d.get("fingerprint") == a.fingerprint())

    # Fingerprint string compare also works directly (not just VectorSpaceId objects).
    check("compatible() accepts raw fingerprint strings too", vs.compatible(a.fingerprint(), a2.fingerprint()) is True)
    check("compatible() with mismatched raw fingerprint strings -> False", vs.compatible(a.fingerprint(), b_diff_model.fingerprint()) is False)

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
