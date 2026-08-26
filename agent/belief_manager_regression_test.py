"""
agent/belief_manager_regression_test.py — regression for the batched
_find_similar() embedding fix (performance architecture pass, unaccounted
time investigation).

Root cause fixed: _is_similar_statement() used to make 2 separate
/api/embed HTTP calls per candidate compared (including re-embedding the
SAME new statement on every single iteration). With 108 active
"biological"-topic beliefs in the real registry, one add_belief() call
for a genuinely new statement could cost 200+ HTTP round-trips
(~27s/candidate observed live). Fixed by batch-embedding [statement] +
all same-topic candidates in ONE /api/embed call, then iterating
candidates in the SAME original order with the SAME decision logic
(exact-match first, then threshold-gated LLM judge, first "equivalent"
wins) — only the embedding lookup changed from N live calls to 1 batch
lookup.

Live equivalence check already done against real registry/beliefs.json
data (108 real "biological" beliefs): max abs diff between old
sequential and new batched cosine similarities = 2.98e-08 (float32
noise), 30.8x speedup on a 15-candidate subset.

This suite covers the MECHANISM deterministically with mocked embed/LLM
calls: exact-match fast path, one-batch-call guarantee (not N calls),
threshold gating, LLM-judge equivalent/contradicts/different outcomes,
first-match short-circuit order preservation, and fail-safe (no merge)
on embedding failure.

Run: /home/iam/venv/bin/python3 -m agent.belief_manager_regression_test
"""

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.belief_manager import BeliefManager, Belief

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


def _make_manager(tmp_path):
    mgr = BeliefManager.__new__(BeliefManager)
    mgr.beliefs = []
    mgr.storage_path = tmp_path
    mgr._save = lambda: None  # avoid touching disk in these tests
    return mgr


def _add(mgr, topic, statement, status="active"):
    b = Belief(
        id=f"bel_{len(mgr.beliefs)}",
        topic=topic,
        statement=statement,
        confidence=0.7,
        evidence_for=[],
        evidence_against=[],
        claim_ids=[],
        created_at=0.0,
        updated_at=0.0,
        history=[],
        status=status,
        prior=0.5,
        likelihood=0.7,
        contradiction_score=0.0,
    )
    mgr.beliefs.append(b)
    return b


TMP = Path("/tmp/belief_manager_regression_test_dummy.json")

# ── 1. Exact match (normalized) short-circuits, no embed/LLM calls ──

mgr1 = _make_manager(TMP)
b1 = _add(mgr1, "biological", "Митохондрии присутствуют в клетках эукариот.")

embed_calls = {"n": 0}
llm_calls = {"n": 0}


def _spy_embed(texts):
    embed_calls["n"] += 1
    return np.ones((len(texts), 4), dtype=np.float32)


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed)):
    with patch.object(mgr1, "_llm_judge_relation", lambda a, b: llm_calls.__setitem__("n", llm_calls["n"] + 1) or "equivalent"):
        found = mgr1._find_similar("biological", "митохондрии   присутствуют В клетках эукариот.")

check("exact match (case/whitespace normalized) returns the existing belief", found is b1)
check("exact match takes the fast path: no embed call needed", embed_calls["n"] == 0, f"got {embed_calls['n']}")
check("exact match takes the fast path: no LLM judge call needed", llm_calls["n"] == 0, f"got {llm_calls['n']}")

# ── 2. ONE batch embed call regardless of candidate count ──

mgr2 = _make_manager(TMP)
for i in range(10):
    _add(mgr2, "biological", f"Утверждение номер {i} про клетки.")

embed_calls["n"] = 0


def _spy_embed2(texts):
    embed_calls["n"] += 1
    check(
        "batch call receives statement + all candidates in one shot",
        len(texts) == 11,
        f"got {len(texts)} texts",
    )
    vecs = np.zeros((len(texts), 4), dtype=np.float32)
    vecs[:, 0] = 1.0  # all identical -> similarity 1.0 for everything
    return vecs


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed2)):
    with patch.object(mgr2, "_llm_judge_relation", lambda a, b: "different"):
        mgr2._find_similar("biological", "Новое непохожее утверждение.")

check("exactly ONE embed call made for 10 candidates (not 10 or 20)", embed_calls["n"] == 1, f"got {embed_calls['n']}")

# ── 3. Similarity below threshold (0.70) skips LLM judge entirely ──

mgr3 = _make_manager(TMP)
_add(mgr3, "biological", "Слон - крупное млекопитающее.")

llm_calls["n"] = 0


def _spy_embed_low(texts):
    # statement vector orthogonal-ish to candidate -> similarity ~0.0
    vecs = np.zeros((len(texts), 2), dtype=np.float32)
    vecs[0] = [1.0, 0.0]
    vecs[1] = [0.0, 1.0]
    return vecs


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_low)):
    with patch.object(mgr3, "_llm_judge_relation", lambda a, b: llm_calls.__setitem__("n", llm_calls["n"] + 1) or "equivalent"):
        found3 = mgr3._find_similar("biological", "Совершенно другая тема про математику.")

check("below-threshold similarity -> no match returned", found3 is None)
check("below-threshold similarity -> LLM judge never invoked", llm_calls["n"] == 0, f"got {llm_calls['n']}")

# ── 4. Above threshold + LLM judge says equivalent -> match ──

mgr4 = _make_manager(TMP)
b4 = _add(mgr4, "biological", "Кит - млекопитающее, а не рыба.")


def _spy_embed_high(texts):
    vecs = np.ones((len(texts), 2), dtype=np.float32)
    return vecs  # identical vectors -> similarity 1.0


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr4, "_llm_judge_relation", lambda a, b: "equivalent"):
        found4 = mgr4._find_similar("biological", "Киты являются млекопитающими, не рыбами.")

check("above-threshold + LLM equivalent -> returns the matching belief", found4 is b4)

# ── 5. Above threshold + LLM says contradicts/different -> no match ──

mgr5 = _make_manager(TMP)
_add(mgr5, "biological", "Кит - млекопитающее.")

with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr5, "_llm_judge_relation", lambda a, b: "contradicts"):
        found5 = mgr5._find_similar("biological", "Кит - это вид рыбы.")

check("above-threshold + LLM contradicts -> no match (not merged)", found5 is None)

# ── 6. First-match short-circuit: iteration order preserved ──

mgr6 = _make_manager(TMP)
b6_first = _add(mgr6, "biological", "Кандидат первый.")
b6_second = _add(mgr6, "biological", "Кандидат второй.")

judge_seen = []


def _judge_first_wins(a, b):
    judge_seen.append(a)
    return "equivalent"  # both would match; first in order must win


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr6, "_llm_judge_relation", _judge_first_wins):
        found6 = mgr6._find_similar("biological", "Новое утверждение.")

check("first candidate in original order wins (short-circuit)", found6 is b6_first)
check("second candidate never reached once first matched", judge_seen == ["Кандидат первый."], f"got {judge_seen}")

# ── 7. Topic/status filtering preserved (unaffected candidates excluded) ──

mgr7 = _make_manager(TMP)
_add(mgr7, "historical", "Другая тема.")
_add(mgr7, "biological", "Отклонённое убеждение.", status="rejected")
b7_active = _add(mgr7, "biological", "Активное убеждение про клетки.")

with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr7, "_llm_judge_relation", lambda a, b: "equivalent"):
        found7 = mgr7._find_similar("biological", "Новый кандидат.")

check("only same-topic, active/revised beliefs considered", found7 is b7_active)

# ── 8. Fail-safe: embedding batch failure -> no merge (never crash) ──

mgr8 = _make_manager(TMP)
_add(mgr8, "biological", "Что-то про биологию.")

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: None)):
    found8 = mgr8._find_similar("biological", "Другое утверждение, не точное совпадение.")

check("embed batch failure (returns None) -> no match, no crash", found8 is None)

# ── 9. No candidates for topic -> returns None without calling embed ──

mgr9 = _make_manager(TMP)
_add(mgr9, "historical", "Не та тема.")

embed_calls["n"] = 0
with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed)):
    found9 = mgr9._find_similar("biological", "Любое утверждение.")

check("no same-topic candidates -> None, no embed call wasted", found9 is None and embed_calls["n"] == 0)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
