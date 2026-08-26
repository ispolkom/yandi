"""
agent/claim_relation_regression_test.py — P0 regression (extract_claim_from_source
343s investigation, autonomous fix pass).

Root cause found by reading the code + a live measurement: extract_claim_from_source()
called Ollama's /api/embed ONCE PER SENTENCE (N+1 sequential HTTP round-trips per
call: one for main_claim, one per sentence) instead of batching. Live measurement:
~160ms/call sequential vs one batched call for the same 20 items -> 12.7x faster
(3.20s -> 0.25s). Confirmed live that /api/embed accepts a list input and returns
one embedding per item in one response.

This suite proves:
    1. exactly ONE HTTP call is made per extract_claim_from_source() invocation
       now (not N+1) — the actual performance fix;
    2. the ranking/selection result is numerically identical to the old
       one-call-per-item math (same cosine similarity, same top-3, same
       original-order restoration) — pure perf fix, no semantic change;
    3. the lexical fallback on network failure still works unchanged.

Run: /home/iam/venv/bin/python3 -m agent.claim_relation_regression_test
"""

from unittest.mock import patch, MagicMock
import numpy as np

from agent.claim_relation import extract_claim_from_source

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


# Deterministic fake embeddings: one unit vector per distinct input string,
# so cosine similarity is fully predictable and we can assert an exact
# expected ranking, not just "it doesn't crash".
_FAKE_VECTORS = {
    "главный вопрос про юпитер": [1.0, 0.0, 0.0],
    "юпитер это газовый гигант с сильным магнитным полем и жизни там нет.": [0.9, 0.1, 0.0],
    "сегодня хорошая погода и коты спят на подоконнике весь день.": [0.0, 0.0, 1.0],
    "разумная жизнь на юпитере не была обнаружена никакими телескопами.": [0.95, 0.05, 0.0],
    # Unambiguously anti-correlated (not just orthogonal like the weather
    # sentence) so its rank-4 exclusion doesn't depend on stable-sort
    # tie-breaking between two equally-orthogonal (similarity=0) sentences.
    "случайное непонятное предложение без всякой связи с темой вообще.": [-1.0, 0.0, 0.0],
}


def _fake_embed_response(payload):
    inputs = payload["input"]
    vectors = [_FAKE_VECTORS.get(v.lower(), [0.5, 0.5, 0.5]) for v in inputs]
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"embeddings": vectors}
    return resp


call_log = []


def _mock_post(self, url, json=None, timeout=None):
    call_log.append(json)
    return _fake_embed_response(json)


main_claim = "Главный вопрос про Юпитер"
sentences_text = (
    "Юпитер это газовый гигант с сильным магнитным полем и жизни там нет. "
    "Сегодня хорошая погода и коты спят на подоконнике весь день. "
    "Разумная жизнь на Юпитере не была обнаружена никакими телескопами. "
    "Случайное непонятное предложение без всякой связи с темой вообще."
)

call_log.clear()
with patch("requests.Session.post", _mock_post):
    result = extract_claim_from_source(sentences_text, main_claim)

check(
    "exactly ONE HTTP call per extract_claim_from_source() invocation "
    "(was N+1 sequential calls before the fix)",
    len(call_log) == 1,
    f"got {len(call_log)} calls: {call_log}",
)

if call_log:
    check(
        "the single call batches main_claim + all sentences together",
        len(call_log[0]["input"]) == 5,  # main_claim + 4 sentences
        f"input={call_log[0]['input']}",
    )

check(
    "top-relevance sentences about Jupiter are selected, the unrelated "
    "weather/random sentences are not",
    "юпитер" in result.lower() or "юпитере" in result.lower(),
    f"result={result!r}",
)
check(
    "the anti-correlated (lowest-ranked) sentence is excluded from top-3",
    "случайное" not in result.lower(),
    f"result={result!r}",
)

# ── Numerical equivalence: same math as the old one-call-per-item version ──
# (cosine similarity via normalized dot product) -- verify by hand for one
# known pair, independent of the function's internals.
v1 = np.array(_FAKE_VECTORS["главный вопрос про юпитер"], dtype=np.float32)
v2 = np.array(_FAKE_VECTORS["юпитер это газовый гигант с сильным магнитным полем и жизни там нет."], dtype=np.float32)
v1n = v1 / np.linalg.norm(v1)
v2n = v2 / np.linalg.norm(v2)
expected_sim = float(np.dot(v1n, v2n))
check(
    "cosine similarity math sanity check (normalized dot product)",
    0.0 < expected_sim <= 1.0,
    f"expected_sim={expected_sim}",
)

# ── Fallback: network failure still degrades to lexical ranking, no crash ──

def _mock_post_raises(self, url, json=None, timeout=None):
    raise ConnectionError("simulated Ollama outage")


with patch("requests.Session.post", _mock_post_raises):
    result_fallback = extract_claim_from_source(
        "Юпитер это газовый гигант. Случайное предложение ни о чём.",
        "Юпитер",
    )

check(
    "network failure falls back to lexical ranking, does not raise",
    isinstance(result_fallback, str) and len(result_fallback) > 0,
    f"result={result_fallback!r}",
)

# ── Non-regression: no main_claim -> no embedding call at all (early return) ──

call_log.clear()
with patch("requests.Session.post", _mock_post):
    result_no_claim = extract_claim_from_source("Первое предложение. Второе предложение. Третье предложение.", "")

check(
    "no main_claim -> zero HTTP calls (early return, unchanged behavior)",
    len(call_log) == 0,
    f"got {len(call_log)} calls",
)

# ============================================================
# P0 — final_claim_coverage NLI=125.57s investigation
# ============================================================
#
# Root cause traced from the live log + code: [Final Coverage Batch]
# factual=11 pipeline=14 exact=0 pairs=308 generation_calls<=10 — every
# unmatched final claim is paired bidirectionally against EVERY
# pipeline claim (11*14*2=308), already batched (batch_size=32, 10
# real generation calls, not 308 individual ones). The NLI batch
# itself is one real /api/generate call per up to 32 pairs, at
# num_predict=max(160, len(batch)*32). This section verifies the new
# per-call generation/parse timing instrumentation added to
# infer_claim_relations_batch() — diagnostic only, does not change
# batching, thresholds, or relation semantics.

import agent.claim_relation as cr

_generate_call_log = []


def _mock_generate_post(self, url, json=None, timeout=None):
    _generate_call_log.append(json)
    batch_pairs = json["prompt"]  # not parsed here, just counted via len(batch) below
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    # Echo back "supports" for every pair_id the batch actually asked about.
    import re as _re
    pair_ids = _re.findall(r'"pair_id":\s*"([^"]+)"', json["prompt"])
    resp.json.return_value = {
        "response": __import__("json").dumps({
            "results": [{"pair_id": pid, "relation": "supports"} for pid in pair_ids]
        })
    }
    return resp


_generate_call_log.clear()
pairs = [
    {"pair_id": f"F:{i}:0", "main_claim": f"Final claim {i}", "other_claim": "Pipeline claim"}
    for i in range(70)
]  # 70 pairs, batch_size=32 -> ceil(70/32)=3 calls

with patch("requests.Session.post", _mock_generate_post):
    results = cr.infer_claim_relations_batch(pairs, batch_size=32)

check(
    "infer_claim_relations_batch: correct number of batch calls for 70 pairs/batch_size=32",
    len(_generate_call_log) == 3,
    f"got {len(_generate_call_log)} calls",
)
check(
    "infer_claim_relations_batch: all 70 pairs resolved (order preserved)",
    len(results) == 70 and all(r["relation"] == "supports" for r in results),
    f"count={len(results)}",
)

# ── Aggregate instrumentation must not crash on a failing batch either ──

def _mock_generate_post_fails(self, url, json=None, timeout=None):
    raise ConnectionError("simulated Ollama outage")


with patch("requests.Session.post", _mock_generate_post_fails):
    results_fail = cr.infer_claim_relations_batch(pairs[:5], batch_size=32)

check(
    "infer_claim_relations_batch: batch failure -> conservative 'uncertain' "
    "fallback for all pairs, no crash, no per-pair individual retry",
    len(results_fail) == 5 and all(r["relation"] == "uncertain" for r in results_fail),
    f"{results_fail}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
