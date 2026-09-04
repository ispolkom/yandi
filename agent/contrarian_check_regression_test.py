"""
agent/contrarian_check_regression_test.py — owner mandate: "янди должна
не верить, искать теорию заговора". Covers agent/contrarian_check.py:
the LLM gate (does this topic have a known alternative/conspiracy
narrative?), the synthetic-claim retrieval path (same shape as agent/
dependency_recheck.py's own), the outcome classification, and the
deterministic note formatting.

_call_ollama/retrieve_for_claims/classify_relation ARE mocked (module-
level monkeypatch on agent.contrarian_check's own bound names) — same
pattern already established by agent/epistemic_dependency_recheck_
regression_test.py for the sibling synthetic-claim path this module
reuses. This file does not re-test retrieve_for_claims/classify_relation
themselves (already covered elsewhere).

Covers:
    1. Gate: has_alternative=false -> None, no retrieval attempted at all
       (cost discipline — a routine question must not trigger a real
       retrieval call).
    2. Gate: has_alternative=true but empty/missing alternative_claim ->
       None (never proceed on a hollow gate response).
    3. Gate call raising / malformed JSON -> None, fails open, never
       raises out of check_for_alternative_theory().
    4. Full path, each outcome: contradicted (evidence_against only),
       supported (evidence_for only), disputed (both), inconclusive
       (evidence exists but neither supports nor contradicts),
       no_evidence (retrieval returns nothing) — same outcome
       vocabulary as agent/dependency_recheck.py, not a second one.
    5. retrieve_for_claims() raising -> None (fails open).
    6. format_alternative_note(): deterministic, includes the claim text
       and the matching verdict phrase for each outcome.
    7. Structural: agent/orchestrator/response/writeback.py calls
       check_for_alternative_theory() ONLY when NOT subjective, NOT
       skip_rag, AND web_used — never unconditionally.

Run: /home/iam/venv/bin/python3 -m agent.contrarian_check_regression_test
"""
from __future__ import annotations

import inspect

import agent.contrarian_check as cc_mod
from agent.contrarian_check import check_for_alternative_theory, format_alternative_note

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


def _patch(ollama=None, retrieve=None, classify=None):
    if ollama is not None:
        cc_mod._call_ollama = ollama
    if retrieve is not None:
        cc_mod.retrieve_for_claims = retrieve
    if classify is not None:
        cc_mod.classify_relation = classify


_orig_ollama = cc_mod._call_ollama
_orig_retrieve = cc_mod.retrieve_for_claims
_orig_classify = cc_mod.classify_relation


def _reset():
    _patch(ollama=_orig_ollama, retrieve=_orig_retrieve, classify=_orig_classify)


# ============================================================
# 1. Gate: no alternative -> None, no retrieval attempted.
# ============================================================

_retrieve_calls = []
_patch(
    ollama=lambda prompt: '{"has_alternative": false, "alternative_claim": null}',
    retrieve=lambda claims, fetch_cache=None: (_retrieve_calls.append(claims) or []),
)
result = check_for_alternative_theory("сколько будет дважды два умножить на два")
check("1: gate says no alternative -> returns None", result is None)
check("1: retrieval was NEVER attempted (cost discipline)", len(_retrieve_calls) == 0)
_reset()

# ============================================================
# 2. Gate says yes but claim text is empty -> None.
# ============================================================

_patch(ollama=lambda prompt: '{"has_alternative": true, "alternative_claim": "  "}')
result2 = check_for_alternative_theory("была ли высадка на Луну?")
check("2: has_alternative=true but blank claim text -> None", result2 is None)
_reset()

# ============================================================
# 3. Fail-open: broken gate call / malformed JSON.
# ============================================================

def _raise(*a, **k):
    raise RuntimeError("Ollama unreachable")


_patch(ollama=_raise)
check("3a: _call_ollama() raising -> check_for_alternative_theory() returns None, never raises", check_for_alternative_theory("НЛО существуют?") is None)
_reset()

_patch(ollama=lambda prompt: "это не json вообще")
check("3b: malformed/non-JSON model output -> None", check_for_alternative_theory("была ли высадка на Луну?") is None)
_reset()

# ============================================================
# 4. Full path, each outcome.
# ============================================================

def _fake_evidence(n):
    return [{"evidence_id": f"ev{i}", "content_excerpt": f"excerpt {i}"} for i in range(n)]


_GATE_YES = '{"has_alternative": true, "alternative_claim": "Высадка на Луну была постановкой"}'

# contradicted: evidence exists, all CONTRADICTS.
_patch(ollama=lambda p: _GATE_YES, retrieve=lambda claims, fetch_cache=None: _fake_evidence(2), classify=lambda main, src: "contradicts")
r_contradicted = check_for_alternative_theory("была ли высадка на Луну?")
check("4a: all-CONTRADICTS evidence -> outcome='contradicted'", r_contradicted is not None and r_contradicted["outcome"] == "contradicted", f"{r_contradicted}")
check("4a: evidence_against populated, evidence_for empty", r_contradicted["evidence_against"] == ["ev0", "ev1"] and r_contradicted["evidence_for"] == [])
_reset()

# supported: all SUPPORTS.
_patch(ollama=lambda p: _GATE_YES, retrieve=lambda claims, fetch_cache=None: _fake_evidence(2), classify=lambda main, src: "supports")
r_supported = check_for_alternative_theory("была ли высадка на Луну?")
check("4b: all-SUPPORTS evidence -> outcome='supported'", r_supported["outcome"] == "supported")
_reset()

# disputed: mixed.
_calls = {"n": 0}
def _mixed_classify(main, src):
    _calls["n"] += 1
    return "supports" if _calls["n"] == 1 else "contradicts"


_patch(ollama=lambda p: _GATE_YES, retrieve=lambda claims, fetch_cache=None: _fake_evidence(2), classify=_mixed_classify)
r_disputed = check_for_alternative_theory("была ли высадка на Луну?")
check("4c: mixed SUPPORTS+CONTRADICTS -> outcome='disputed'", r_disputed["outcome"] == "disputed")
_reset()

# inconclusive: evidence exists but neither supports nor contradicts.
_patch(ollama=lambda p: _GATE_YES, retrieve=lambda claims, fetch_cache=None: _fake_evidence(2), classify=lambda main, src: "unrelated")
r_inconclusive = check_for_alternative_theory("была ли высадка на Луну?")
check("4d: all-UNRELATED evidence -> outcome='inconclusive'", r_inconclusive["outcome"] == "inconclusive")
_reset()

# no_evidence: retrieval returns nothing at all.
_patch(ollama=lambda p: _GATE_YES, retrieve=lambda claims, fetch_cache=None: [])
r_none = check_for_alternative_theory("была ли высадка на Луну?")
check("4e: empty retrieval -> outcome='no_evidence'", r_none["outcome"] == "no_evidence")
_reset()

# ============================================================
# 5. retrieve_for_claims() raising -> fails open.
# ============================================================

_patch(ollama=lambda p: _GATE_YES, retrieve=_raise)
check("5: retrieve_for_claims() raising -> None, never propagates", check_for_alternative_theory("была ли высадка на Луну?") is None)
_reset()

# ============================================================
# 6. format_alternative_note().
# ============================================================

note = format_alternative_note({"alternative_claim": "Земля плоская", "outcome": "contradicted", "evidence_for": [], "evidence_against": ["e1"]})
check("6: note includes the alternative claim text", "Земля плоская" in note)
check("6: note includes the 'contradicted' verdict phrase", "противоречат" in note.lower())

note_disputed = format_alternative_note({"alternative_claim": "X", "outcome": "disputed", "evidence_for": ["a"], "evidence_against": ["b"]})
check("6: 'disputed' outcome uses the genuine-controversy phrasing, not a false-certainty one", "спора" in note_disputed.lower())

check(
    "6: format_alternative_note() is deterministic text — no LLM call happens inside it "
    "(same discipline as agent/claim_history_note.py's own formatter)",
    "_call_ollama" not in inspect.getsource(format_alternative_note),
)

# ============================================================
# 7. Structural: writeback.py's gating.
# ============================================================

import agent.orchestrator.response.writeback as wb

_src_wb = inspect.getsource(wb)
_pos_call = _src_wb.find("check_for_alternative_theory(query_to_use)")
check("7: writeback.py actually calls check_for_alternative_theory()", _pos_call != -1)

_gate_window = _src_wb[max(0, _pos_call - 400):_pos_call]
check(
    "7: the call is gated on NOT is_subjective_answer, NOT skip_rag, AND web_used — never "
    "unconditional",
    "is_subjective_answer" in _gate_window and "skip_rag" in _gate_window and "web_used" in _gate_window,
    _gate_window,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
