"""
agent/polarity_hardening_regression_test.py — Этап 4D-1 (P8) regression:
predicate polarity guard (agent/claim_semantic_identity_hardening.py's
_NEGATION, feeding hardening_guard()'s negation_marker_mismatch veto).

Correctness-first patch: a family-matching false positive on
"X causes Y" vs "X does not cause Y" would let CONTRADICTORY claims
merge into one semantic family — worse than the old "we searched again
for a paraphrase" false negative. This suite proves:

  1. Old working negation cases still work (не была/не обнаружено/
     не найдено/нельзя/невозможно/неэффективно).
  2. A real pre-existing bug is fixed: "не является"/"не являются" etc.
     never matched before (trailing \\b landed mid-word on the longer
     conjugated forms) despite looking covered by the old pattern.
  3. New conservative predicate stems (вызывает/влияет/подтверждает/
     снижает/приводит) catch generic "не + predicate" — NOT a
     generic "не anywhere in text" detector.
  4. Explicit non-predicate "не"-idioms (не только/не менее/не более/
     не обязательно/не просто) do NOT false-veto.
  5. English predicate negation (does not/is not/cannot/was not/n't).
  6. Symmetry: hardening_guard(A, B) == hardening_guard(B, A) for a
     polarity mismatch.
  7. The guard still only ever DOWNGRADES equivalent->different, never
     the reverse (existing invariant, re-confirmed after this change).

Run: /home/iam/venv/bin/python3 -m agent.polarity_hardening_regression_test
"""
from __future__ import annotations

from agent.claim_semantic_identity_hardening import _NEGATION, hardening_guard

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


# ============================================================
# 1. Existing negation cases still work (not regressed by the rewrite).
# ============================================================

_EXISTING_CASES = [
    ("не была подтверждена", "Связь не была подтверждена исследованием."),
    ("не обнаружено", "Эффект не обнаружен в контрольной группе."),
    ("не найдено", "Результат не найден."),
    ("нельзя", "Это нельзя доказать."),
    ("невозможно", "Это невозможно доказать."),
    ("неэффективно", "Метод неэффективен для лечения."),
]
for label, text in _EXISTING_CASES:
    check(f"1: existing case '{label}' still matches _NEGATION", bool(_NEGATION.search(text)), text)

# ============================================================
# 2. Real pre-existing bug fixed: "не является"/"не являются" etc.
# ============================================================

_BYT_FAMILY = [
    "Кофе не является канцерогеном.",
    "Эти утверждения не являются эквивалентными.",
    "Он не являлся членом организации.",
    "Она не являлась участницей исследования.",
]
for text in _BYT_FAMILY:
    check(f"2: 'не явля-' family now matches (was broken by a trailing-boundary bug): {text!r}",
          bool(_NEGATION.search(text)))

# ============================================================
# 3. New conservative predicate stems (§2 of the brief) — generic
#    "не + predicate", not "не anywhere in text".
# ============================================================

_NEW_PREDICATE_CASES = [
    ("вызывает", "Кофе вызывает рак.", "Кофе не вызывает рак."),
    ("влияет", "X влияет на Y.", "X не влияет на Y."),
    ("подтверждает", "Исследование подтверждает связь.", "Исследование не подтверждает связь."),
    ("снижает", "Препарат снижает риск.", "Препарат не снижает риск."),
    ("приводит", "X приводит к Y.", "X не приводит к Y."),
    ("является", "Кофе является канцерогеном.", "Кофе не является канцерогеном."),
]
for verb, positive, negative in _NEW_PREDICATE_CASES:
    reason = hardening_guard(positive, negative)
    check(
        f"3: '{verb}' vs 'не {verb}' -> hardening_guard vetoes as negation_marker_mismatch",
        reason == "negation_marker_mismatch",
        f"got {reason!r}",
    )

# ============================================================
# 4. Non-predicate "не"-idioms must NOT false-veto.
# ============================================================

_EXCLUSION_CASES = [
    ("не только", "Это не только экономический союз, но и политический.", "Это экономический и политический союз."),
    ("не менее", "В ЕС входит не менее 27 государств.", "В ЕС входит 27 государств."),
    ("не более", "В ЕС входит не более 27 государств.", "В ЕС входит 27 государств."),
    ("не обязательно", "Это не обязательно означает рост.", "Это означает рост."),
    ("не просто", "Это не просто экономический союз.", "Это экономический союз."),
]
for label, a, b in _EXCLUSION_CASES:
    fired = bool(_NEGATION.search(a))
    check(
        f"4: '{label}' is NOT detected as predicate negation (idiom/particle, not a negated predicate)",
        not fired,
        f"'{a}' matched: {_NEGATION.search(a).group() if fired else None}",
    )

# ============================================================
# 5. English predicate negation.
# ============================================================

_EN_CASES = [
    ("does not", "Coffee causes cancer.", "Coffee does not cause cancer."),
    ("is not", "This is proven.", "This is not proven."),
    ("cannot", "It can be true.", "It cannot be true."),
    ("was not", "It was proven.", "It was not proven."),
    ("n't contraction", "It works.", "It doesn't work."),
]
for label, a, b in _EN_CASES:
    check(f"5: English '{label}' detected in negated text", bool(_NEGATION.search(b)), b)
    check(f"5: English '{label}' positive text has NO false match", not _NEGATION.search(a), a)

# ============================================================
# 6. Symmetry: order of arguments must not change the verdict.
# ============================================================

_pos, _neg = "Кофе вызывает рак.", "Кофе не вызывает рак."
check(
    "6: hardening_guard(A, B) == hardening_guard(B, A) for a polarity mismatch",
    hardening_guard(_pos, _neg) == hardening_guard(_neg, _pos) == "negation_marker_mismatch",
    f"{hardening_guard(_pos, _neg)!r} vs {hardening_guard(_neg, _pos)!r}",
)

# ============================================================
# 7. The guard can only DOWNGRADE — never fabricate a match. Confirmed
# by construction (hardening_guard only ever returns a reason string or
# None, never forces "equivalent") — re-verified via the integration
# path used by classify_claim_pair_detailed elsewhere in the existing
# suite (agent/epistemic_claim_semantic_identity_hardening_regression_
# test.py); here we just confirm a GENUINE paraphrase (no polarity
# mismatch) is untouched by the new stems.
# ============================================================

_paraphrase_a = "Кофе вызывает рак у некоторых людей."
_paraphrase_b = "У некоторых людей кофе вызывает рак."
check(
    "7: a genuine paraphrase (same polarity, same predicate) is NOT vetoed by the new stems",
    hardening_guard(_paraphrase_a, _paraphrase_b) is None,
    f"got {hardening_guard(_paraphrase_a, _paraphrase_b)!r}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
