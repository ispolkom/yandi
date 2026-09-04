"""
agent/belief_manager_regression_test.py — regression for the batched
_find_similar() embedding fix (performance architecture pass, unaccounted
time investigation).

Root cause fixed: _is_similar_statement() used to make 2 separate
/api/embed HTTP calls per candidate compared. Fixed by batch-embedding
[statement] + all same-topic candidates in ONE /api/embed call, then
iterating candidates in the SAME original order with the SAME decision
logic (exact-match first, then threshold-gated LLM judge, first
"equivalent" wins) — only the embedding lookup changed from N live
calls to 1 batch lookup.

This suite covers the MECHANISM deterministically with mocked embed/LLM
calls: exact-match fast path, one-batch-call guarantee (not N calls),
threshold gating, LLM-judge equivalent/contradicts/different outcomes,
first-match short-circuit order preservation, and fail-safe (no merge)
on embedding failure.

"ТОЧКА НОЛЬ" UPDATE (owner mandate, 2026-09): _find_similar() now reads
candidates from SQL (agent.db.sql.repositories.list_beliefs_by_topic())
instead of an in-memory `self.beliefs` list — the old
`BeliefManager.__new__(BeliefManager); mgr.beliefs = [...]` construction
this file used to use bypasses __init__ entirely and has no SQL
equivalent to bypass INTO. Rewired to mock repo.list_beliefs_by_topic()
directly (and a no-op get_connection(), since _find_similar() still
opens a connection before handing it to the now-mocked repo call) — the
DECISION LOGIC under test (exact match, batching, threshold, LLM judge,
short-circuit order, fail-safe) is completely unchanged, only how
candidates are supplied changed.

Run: /home/iam/venv/bin/python3 -m agent.belief_manager_regression_test
"""

import contextlib
from unittest.mock import patch

import numpy as np

from agent.belief_manager import BeliefManager
import agent.belief_manager as bm_mod

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


def _row(belief_id, topic, statement, status="active"):
    """A SQL-row-shaped dict — exactly what agent.db.sql.repositories.
    list_beliefs_by_topic() would hand back (already JSON-decoded, see
    repositories._decode_belief_json())."""
    return {
        "belief_id": belief_id, "topic": topic, "statement": statement,
        "confidence": 0.7, "status": status,
        "evidence_for": [], "evidence_against": [], "claim_ids": [],
        "prior": 0.5, "likelihood": 0.7, "contradiction_score": 0.0, "decay_factor": 0.95,
        "superseded_by": None, "created_at": 0.0, "updated_at": 0.0,
    }


@contextlib.contextmanager
def _noop_get_connection(autocommit=False):
    yield None


def _find_similar_with(mgr, candidates, topic, statement):
    """Runs mgr._find_similar(topic, statement) with get_connection()
    a no-op and list_beliefs_by_topic() doing the SAME topic+status
    filtering the real SQL query does (WHERE topic=%s AND status IN
    ('active','revised')) over the given fixture rows — preserves this
    file's own scenario 7 (topic/status filtering) as a meaningful
    check of the real integration contract, not just a mock that
    hands back whatever it's given unconditionally."""
    def _fake_list_beliefs_by_topic(conn, t, statuses=None):
        statuses = statuses or ["active", "revised"]
        return [dict(c) for c in candidates if c["topic"] == t and c["status"] in statuses]

    with patch.object(bm_mod, "get_connection", _noop_get_connection):
        with patch.object(bm_mod.repo, "list_beliefs_by_topic", _fake_list_beliefs_by_topic):
            return mgr._find_similar(topic, statement)


mgr = BeliefManager.__new__(BeliefManager)  # skip __init__'s decay sweep — irrelevant to _find_similar

# ── 1. Exact match (normalized) short-circuits, no embed/LLM calls ──

b1 = _row("bel_0", "biological", "Митохондрии присутствуют в клетках эукариот.")

embed_calls = {"n": 0}
llm_calls = {"n": 0}


def _spy_embed(texts):
    embed_calls["n"] += 1
    return np.ones((len(texts), 4), dtype=np.float32)


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed)):
    with patch.object(mgr, "_llm_judge_relation", lambda a, b: llm_calls.__setitem__("n", llm_calls["n"] + 1) or "equivalent"):
        found = _find_similar_with(mgr, [b1], "biological", "митохондрии   присутствуют В клетках эукариот.")

check("exact match (case/whitespace normalized) returns the existing belief", found is not None and found.id == "bel_0")
check("exact match takes the fast path: no embed call needed", embed_calls["n"] == 0, f"got {embed_calls['n']}")
check("exact match takes the fast path: no LLM judge call needed", llm_calls["n"] == 0, f"got {llm_calls['n']}")

# ── 2. ONE batch embed call regardless of candidate count ──

rows2 = [_row(f"bel_{i}", "biological", f"Утверждение номер {i} про клетки.") for i in range(10)]

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
    with patch.object(mgr, "_llm_judge_relation", lambda a, b: "different"):
        _find_similar_with(mgr, rows2, "biological", "Новое непохожее утверждение.")

check("exactly ONE embed call made for 10 candidates (not 10 or 20)", embed_calls["n"] == 1, f"got {embed_calls['n']}")

# ── 3. Similarity below threshold (0.70) skips LLM judge entirely ──

row3 = _row("bel_3", "biological", "Слон - крупное млекопитающее.")
llm_calls["n"] = 0


def _spy_embed_low(texts):
    vecs = np.zeros((len(texts), 2), dtype=np.float32)
    vecs[0] = [1.0, 0.0]
    vecs[1] = [0.0, 1.0]
    return vecs


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_low)):
    with patch.object(mgr, "_llm_judge_relation", lambda a, b: llm_calls.__setitem__("n", llm_calls["n"] + 1) or "equivalent"):
        found3 = _find_similar_with(mgr, [row3], "biological", "Совершенно другая тема про математику.")

check("below-threshold similarity -> no match returned", found3 is None)
check("below-threshold similarity -> LLM judge never invoked", llm_calls["n"] == 0, f"got {llm_calls['n']}")

# ── 4. Above threshold + LLM judge says equivalent -> match ──

row4 = _row("bel_4", "biological", "Кит - млекопитающее, а не рыба.")


def _spy_embed_high(texts):
    return np.ones((len(texts), 2), dtype=np.float32)  # identical vectors -> similarity 1.0


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr, "_llm_judge_relation", lambda a, b: "equivalent"):
        found4 = _find_similar_with(mgr, [row4], "biological", "Киты являются млекопитающими, не рыбами.")

check("above-threshold + LLM equivalent -> returns the matching belief", found4 is not None and found4.id == "bel_4")

# ── 5. Above threshold + LLM says contradicts/different -> no match ──

row5 = _row("bel_5", "biological", "Кит - млекопитающее.")

with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr, "_llm_judge_relation", lambda a, b: "contradicts"):
        found5 = _find_similar_with(mgr, [row5], "biological", "Кит - это вид рыбы.")

check("above-threshold + LLM contradicts -> no match (not merged)", found5 is None)

# ── 6. First-match short-circuit: iteration order preserved ──

row6_first = _row("bel_6a", "biological", "Кандидат первый.")
row6_second = _row("bel_6b", "biological", "Кандидат второй.")

judge_seen = []


def _judge_first_wins(a, b):
    judge_seen.append(a)
    return "equivalent"  # both would match; first in order must win


with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr, "_llm_judge_relation", _judge_first_wins):
        found6 = _find_similar_with(mgr, [row6_first, row6_second], "biological", "Новое утверждение.")

check("first candidate in original order wins (short-circuit)", found6 is not None and found6.id == "bel_6a")
check("second candidate never reached once first matched", judge_seen == ["Кандидат первый."], f"got {judge_seen}")

# ── 7. Topic/status filtering preserved (unaffected candidates excluded) ──
# NOTE: the actual topic/status filtering now lives in the real SQL
# query (list_beliefs_by_topic's WHERE clause) rather than inside
# _find_similar() itself — _find_similar_with()'s fake reproduces that
# exact filter, so this still meaningfully proves the integration
# contract (call it with the right topic, trust it to filter), not
# _find_similar()'s own no-longer-existent filtering code.

row7_other_topic = _row("bel_7a", "historical", "Другая тема.")
row7_rejected = _row("bel_7b", "biological", "Отклонённое убеждение.", status="rejected")
row7_active = _row("bel_7c", "biological", "Активное убеждение про клетки.")

with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed_high)):
    with patch.object(mgr, "_llm_judge_relation", lambda a, b: "equivalent"):
        found7 = _find_similar_with(mgr, [row7_other_topic, row7_rejected, row7_active], "biological", "Новый кандидат.")

check("only same-topic, active/revised beliefs considered", found7 is not None and found7.id == "bel_7c")

# ── 8. Fail-safe: embedding batch failure -> no merge (never crash) ──

row8 = _row("bel_8", "biological", "Что-то про биологию.")

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: None)):
    found8 = _find_similar_with(mgr, [row8], "biological", "Другое утверждение, не точное совпадение.")

check("embed batch failure (returns None) -> no match, no crash", found8 is None)

# ── 9. No candidates for topic -> returns None without calling embed ──

row9 = _row("bel_9", "historical", "Не та тема.")

embed_calls["n"] = 0
with patch.object(BeliefManager, "_embed_batch", staticmethod(_spy_embed)):
    found9 = _find_similar_with(mgr, [row9], "biological", "Любое утверждение.")

check("no same-topic candidates -> None, no embed call wasted", found9 is None and embed_calls["n"] == 0)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
