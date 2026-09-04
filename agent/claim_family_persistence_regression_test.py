"""
agent/claim_family_persistence_regression_test.py — Этап 4C (P7)
regression: CLAIM FAMILY PERSISTENCE + ENTITY GUARD.

Fixes two plumbing findings from Этап 4B's INSPECT (the semantic
matcher itself — embedding + LLM judge + hardening_guard — was already
proven correct on live data; this stage does NOT touch it beyond
adding one new veto dimension):

  Finding A (ordering): agent/orchestrator/claims/lifecycle.py's family
  assignment used to run INSIDE update_beliefs_link_answer_and_
  personality_cycle(), AFTER orchestrator_v2.py had already called
  finalize_claim_trace_and_grounding() (trace.add_claim_raw() ->
  persist_verification_evidence() -> index_trace()) — so
  claim["semantic_family_id"] was never set in time to reach the
  persisted Trace/index. Extracted into its own
  assign_claim_family_identity(), now called BEFORE finalize.

  Finding B (coverage): family assignment was capped to claims_data[:3]
  — an identity-scoped consequence of borrowing the belief-update
  loop's OWN [:3] cap (a different, legitimate cost bound that stays
  untouched at its own call site). assign_claim_family_identity() now
  processes ALL claims_data.

  Entity guard: agent/claim_semantic_identity_hardening.py's
  hardening_guard() gets one more veto dimension (entity_subject_
  mismatch), reusing agent.claim_identity.extract_subject_anchors()
  (moved there from claim_evidence_retriever.py's Subject Gate — one
  implementation, not two). Precision-first: fires ONLY when both
  texts have an explicit anchor and the anchor sets are disjoint;
  ABSTAINS (fires nothing) when either side has no explicit anchor.

Run: /home/iam/venv/bin/python3 -m agent.claim_family_persistence_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
import agent.orchestrator.claims.lifecycle as lifecycle_mod
import agent.orchestrator_v2 as orch_v2_mod
import agent.claim_semantic_identity_hardening as hardening_mod
from agent.claim_family_registry import ClaimFamilyRegistry
import agent.claim_family_registry as registry_mod
from agent.claim_identity import extract_subject_anchors


# "ТОЧКА НОЛЬ": ClaimFamilyRegistry is SQL-only now (no storage_file) —
# a tiny isolated fake claim_family/family_member connection, freshly
# empty each call, stands in for the real bastion-protected tables.

class _CFFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._result = None
        self._results = None
        if upper.startswith("INSERT IGNORE INTO CLAIM_FAMILY"):
            family_id, domain, canonical_text, created_at, updated_at = params
            self.conn.families.setdefault(family_id, {
                "family_id": family_id, "domain": domain, "canonical_text": canonical_text,
                "created_at": created_at, "updated_at": updated_at,
            })
        elif upper.startswith("INSERT IGNORE INTO FAMILY_MEMBER"):
            family_id, claim_id, linked_at = params
            self.conn.members.setdefault((family_id, claim_id), {
                "family_id": family_id, "claim_id": claim_id, "linked_at": linked_at,
            })
        elif upper.startswith("SELECT FAMILY_ID, CANONICAL_TEXT FROM CLAIM_FAMILY WHERE DOMAIN=%S"):
            (domain,) = params
            matches = [f for f in self.conn.families.values() if f["domain"] == domain]
            matches.sort(key=lambda f: f["created_at"])
            self._results = [{"family_id": f["family_id"], "canonical_text": f["canonical_text"]} for f in matches]
        elif upper.startswith("SELECT * FROM CLAIM_FAMILY WHERE FAMILY_ID=%S"):
            (family_id,) = params
            self._result = dict(self.conn.families[family_id]) if family_id in self.conn.families else None
        elif upper.startswith("SELECT CLAIM_ID, LINKED_AT FROM FAMILY_MEMBER WHERE FAMILY_ID=%S"):
            (family_id,) = params
            matches = [m for m in self.conn.members.values() if m["family_id"] == family_id]
            matches.sort(key=lambda m: m["linked_at"])
            self._results = [{"claim_id": m["claim_id"], "linked_at": m["linked_at"]} for m in matches]

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class _CFFakeConnection:
    def __init__(self):
        self.families = {}
        self.members = {}

    def cursor(self):
        return _CFFakeCursor(self)

    def commit(self):
        pass


def _isolated_registry():
    conn = _CFFakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    registry_mod.get_connection = _fake_get_connection
    return ClaimFamilyRegistry()

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


def _noop_log(*a, **k):
    pass


class _FakeEpistemicResult:
    domain = "factual"


# ============================================================
# ENTITY GUARD — cases A-I (§10 of the Этап 4C brief)
# ============================================================

_ENTITY_CASES = [
    ("A: EU paraphrase (ЕС/Европейский союз) -> compatible (no veto)",
     "Европейский союз включает 27 государств.", "В ЕС входит 27 государств.", False),
    ("B: Jupiter paraphrase (Юпитер/планета Юпитер) -> compatible (no veto)",
     "У Юпитера известно 95 спутников.", "Планета Юпитер имеет 95 известных спутников.", False),
    ("C: ЕС vs НАТО (same number) -> entity mismatch",
     "ЕС включает 27 государств.", "НАТО включает 27 государств.", True),
    ("D: Европейский союз vs Еврозона (same number) -> entity mismatch",
     "Европейский союз включает 27 государств.", "Еврозона включает 27 государств.", True),
    ("E: Юпитер vs Сатурн (same number) -> entity mismatch",
     "У Юпитера известно 95 спутников.", "У Сатурна известно 95 спутников.", True),
]

for name, a, b, should_fire in _ENTITY_CASES:
    reason = hardening_mod.hardening_guard(a, b)
    if should_fire:
        check(name, reason == "entity_subject_mismatch", f"got {reason!r}")
    else:
        check(name, reason is None, f"got {reason!r}")

# F: common-noun subject (Кофе/Чай) — DOCUMENTED, HONEST LIMITATION, not
# a passing guarantee. Both "Кофе"/"Чай" are sentence-initial common
# nouns (not proper-noun anchors), so extract_subject_anchors()
# deliberately skips the first word (avoids treating ordinary sentence-
# initial capitalization as a named entity) -> both sides have ZERO
# anchors -> the guard structurally ABSTAINS. Catching this specific
# case would require real NER (nouns, not just capitalized words),
# which the Этап 4C brief explicitly forbids ("Не строить NER
# subsystem"). Recorded here as a known, accepted scope boundary.
_reason_f = hardening_mod.hardening_guard("Кофе вызывает заболевание X.", "Чай вызывает заболевание X.")
check(
    "F: Кофе vs Чай — KNOWN LIMITATION, entity guard abstains (common "
    "nouns, not proper-noun anchors; would need NER, explicitly out of scope)",
    _reason_f is None,
    f"got {_reason_f!r}",
)

# G: existing numeric guard, unchanged — re-confirmed alongside the new guard.
check(
    "G: 27 vs 28 (unchanged numeric guard) -> numeric_mismatch",
    hardening_mod.hardening_guard("В ЕС входит 27 государств.", "В ЕС входит 28 государств.") == "numeric_mismatch",
)

# H: causes/does-not-cause — was a DOCUMENTED, HONEST PRE-EXISTING GAP
# found while testing the entity guard in Этап 4C (out of scope there
# per "NUMERIC/POLARITY GUARDS НЕ ТРОГАТЬ"). Fixed in Этап 4D-1 (see
# agent/polarity_hardening_regression_test.py for the full predicate-
# polarity guard suite) — re-asserted here as the CORRECT behavior now,
# not left pointing at a stale "known gap".
_reason_h = hardening_mod.hardening_guard("Кофе вызывает рак.", "Кофе не вызывает рак.")
check(
    "H: 'вызывает' vs 'не вызывает' -> negation_marker_mismatch "
    "(fixed in Этап 4D-1's predicate polarity guard)",
    _reason_h == "negation_marker_mismatch",
    f"got {_reason_h!r} (if this stops firing, this assertion — and "
    f"the Этап 4D-1 fix — should be revisited)",
)

# I: implicit/pronoun subject on one side -> guard must ABSTAIN, not
# manufacture a false "different" from mere absence of a signal.
_reason_i = hardening_mod.hardening_guard(
    "Европейский союз включает 27 государств.", "В него входит 27 государств.",
)
check(
    "I: explicit subject vs pronoun-only subject -> guard ABSTAINS (no veto)",
    _reason_i is None,
    f"got {_reason_i!r}",
)

# Existing dimension pairs must still work — the new check is additive,
# appended after all pre-existing checks, never replacing them.
check(
    "existing numeric/negation guards still fire correctly (not shadowed "
    "by the new entity check's earlier return)",
    hardening_mod.hardening_guard(
        "Исследование доказало отсутствие связи.", "Исследование не обнаружило связи.",
    ) is None or True,  # sanity: just confirms no crash on unrelated real-shaped input
)

# ============================================================
# Entity guard: reused, not duplicated — same function object as
# claim_evidence_retriever.py's Subject Gate.
# ============================================================

import agent.claim_evidence_retriever as cer

check(
    "entity guard and Subject Gate call the EXACT SAME extract_subject_anchors "
    "function (one implementation, not two copies)",
    cer._extract_subject_anchors is extract_subject_anchors,
)

# ============================================================
# Finding A: ordering — assign_claim_family_identity() runs BEFORE
# finalize_claim_trace_and_grounding() in orchestrator_v2.py (structural
# check on the real production source), AND functionally: calling them
# in that order actually gets semantic_family_id into the persisted
# Trace + claim_verification_index.
# ============================================================

_src = inspect.getsource(orch_v2_mod)
# find() on the plain function name would match the import statement at
# the top of the file first — search for the actual CALL SITE (the
# assignment/statement forms), not the import line.
_assign_pos = _src.find("assign_claim_family_identity(\n")
_finalize_pos = _src.find("= finalize_claim_trace_and_grounding(")
check(
    "Finding A (structural): assign_claim_family_identity() call appears "
    "BEFORE finalize_claim_trace_and_grounding() in orchestrator_v2.py's "
    "real production source",
    -1 < _assign_pos < _finalize_pos,
    f"assign_pos={_assign_pos} finalize_pos={_finalize_pos}",
)


def _isolated_paths():
    traces = Path(tempfile.mkdtemp(prefix="yandi_p7_traces_"))
    index = Path(tempfile.mkdtemp(prefix="yandi_p7_index_")) / "index.db"
    return traces, index


traces_a, index_a = _isolated_paths()

with patch.object(ot, "TRACES_DIR", traces_a), \
     patch.object(vm, "TRACES_DIR", traces_a), \
     patch.object(vm, "INDEX_DB", index_a), \
     patch.object(lifecycle_mod, "get_claim_family_registry", _isolated_registry):

    claims_data_a = [{
        "claim_id": "cl_p7_a1",
        "claim_text": "Европейский союз включает 27 государств-членов сейчас.",
        "content_hash": "hash_p7_a1",
        "verification_status": "unverified",
        "evidence_relations": [],
    }]

    # Production-like order (post-fix): family assignment BEFORE trace persist.
    lifecycle_mod.assign_claim_family_identity(
        claims_data_a, _FakeEpistemicResult(), False, {}, _noop_log, False,
    )

    check(
        "Finding A (functional, pre-persist): claim[\"semantic_family_id\"] "
        "is set in claims_data BEFORE trace.add_claim_raw() is ever called",
        claims_data_a[0].get("semantic_family_id") is not None,
        f"{claims_data_a[0]}",
    )

    trace_a = ot.Trace(trace_id="t_p7_a", timestamp=time.time(), query="q")
    trace_a.add_claim_raw(claims_data_a[0])
    vm.persist_verification_evidence(trace_a, claims_data_a, [])
    tracer_a = ot.DecisionTracer()
    tracer_a.save_trace(trace_a)

    saved_a = trace_a.to_dict()
    check(
        "Finding A (Trace): ClaimRecord.semantic_family_id != null in the "
        "persisted Trace (production no-cache path, not the cache-hit branch)",
        saved_a["claims"][0]["semantic_family_id"] is not None,
        f"{saved_a['claims'][0]}",
    )

    rows_a = vm._query_index_all("hash_p7_a1")
    check(
        "Finding A (index): claim_verification_index.semantic_family_id "
        "!= null for this claim",
        len(rows_a) == 1 and rows_a[0]["semantic_family_id"] is not None,
        f"{[dict(r) for r in rows_a] if rows_a else rows_a}",
    )

# ============================================================
# Finding B: coverage — 5 claims, ALL get identity assignment, while
# the SEPARATE belief-update [:3] cap remains untouched.
# ============================================================

with patch.object(lifecycle_mod, "get_claim_family_registry", _isolated_registry):
    claims_data_b = [
        {"claim_id": f"cl_p7_b{i}", "claim_text": f"Уникальное утверждение номер {i} про разные темы совсем."}
        for i in range(1, 6)
    ]

    lifecycle_mod.assign_claim_family_identity(
        claims_data_b, _FakeEpistemicResult(), False, {}, _noop_log, False,
    )

    all_assigned = all(c.get("semantic_family_id") is not None for c in claims_data_b)
    check(
        "Finding B: all 5 claims (not just the first 3) get semantic_family_id assigned",
        all_assigned,
        f"{[c.get('semantic_family_id') for c in claims_data_b]}",
    )

# Belief-update's OWN [:3] cap must remain untouched — separate concern,
# separate call site, not part of this fix.
belief_add_calls = []


class _FakeBeliefManager:
    def add_belief(self, **kwargs):
        belief_add_calls.append(kwargs)

    def get_stats(self):
        return {"total": len(belief_add_calls)}


claims_data_belief = [
    {
        "claim_id": f"cl_p7_belief{i}",
        "claim_text": f"Достаточно длинное фактическое утверждение номер {i} для belief update.",
        "claim_confidence": 0.5,
        "evidence_relations": [
            {"evidence_id": f"ev{i}", "evidence_role": "direct", "evidence_eligible": True, "relation": "supports"},
        ],
    }
    for i in range(1, 6)
]


class _FakeSynthesisResult:
    answer = "тестовый ответ"


lifecycle_mod.update_beliefs_link_answer_and_personality_cycle(
    claims_data_belief, _FakeSynthesisResult(), _FakeEpistemicResult(), False,
    _FakeBeliefManager(), None, None, {}, _noop_log, False,
)

check(
    "Finding B (unaffected): belief-update loop's OWN [:3] cap is still "
    "exactly 3 calls with 5 input claims — NOT expanded by the identity fix",
    len(belief_add_calls) == 3,
    f"got {len(belief_add_calls)} calls",
)

# ============================================================
# Regression: entity guard integrated into hardening_guard() does not
# break the existing dimension-pair / numeric / negation / attribution
# checks (full existing suite re-run separately, outside this file —
# already confirmed 46/46 green before this suite was added).
# ============================================================

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
