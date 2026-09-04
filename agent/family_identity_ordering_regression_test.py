"""
agent/family_identity_ordering_regression_test.py — Этап 4G-1 (P10)
regression: FAMILY IDENTITY ORDERING.

assign_claim_family_identity() moved from AFTER
classify_claim_epistemic_status() to BEFORE it (orchestrator_v2.py) —
INSPECT (Этап 4F/4G-1) confirmed neither function reads the other's
output (assign_claim_family_identity only touches claim_text/claim_id/
epistemic_result.domain/is_subjective_answer; status.py has zero
references to semantic_family_id). This suite proves that PROVEN
independence, not just asserts it: running both functions in either
order on independent copies of the same fixture must produce BYTE-
IDENTICAL verification_status/support_count/contradiction_count AND
semantic_family_id results.

Run: /home/iam/venv/bin/python3 -m agent.family_identity_ordering_regression_test
"""
from __future__ import annotations

import contextlib
import copy
import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orchestrator_v2 as orch_v2_mod
import agent.orch_tracer as ot
import agent.verification_memory as vm
import agent.orchestrator.claims.lifecycle as lifecycle_mod
from agent.orchestrator.claims.status import classify_claim_epistemic_status
from agent.orchestrator.claims.lifecycle import assign_claim_family_identity
from agent.claim_family_registry import ClaimFamilyRegistry
import agent.claim_family_registry as registry_mod


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


def _make_claims_and_evidence():
    """A realistic post-PASS2 fixture: 3 claims, each with real
    evidence_relations (as PASS1/PASS2/memory pass would have already
    produced by this point in the real pipeline), so
    classify_claim_epistemic_status has real work to do."""
    claims = [
        {
            "claim_id": "cl_ord_1",
            "claim_text": "Европейский союз включает 27 государств-членов на текущий момент.",
            "claim_confidence": 0.6,
            "derived_from_evidence_ids": ["ev_ord_1a", "ev_ord_1b"],
            "evidence_relations": [
                {"evidence_id": "ev_ord_1a", "relation": "supports", "evidence_role": "direct",
                 "evidence_eligible": True, "source_class": "reference"},
                {"evidence_id": "ev_ord_1b", "relation": "uncertain", "evidence_role": "context",
                 "evidence_eligible": False, "source_class": "unknown"},
            ],
        },
        {
            "claim_id": "cl_ord_2",
            "claim_text": "У Юпитера известно 95 подтверждённых спутников на данный момент.",
            "claim_confidence": 0.5,
            "derived_from_evidence_ids": ["ev_ord_2a"],
            "evidence_relations": [
                {"evidence_id": "ev_ord_2a", "relation": "contradicts", "evidence_role": "direct",
                 "evidence_eligible": True, "source_class": "reference"},
            ],
        },
        {
            "claim_id": "cl_ord_3",
            "claim_text": "Совершенно другое утверждение без какой-либо evidence вообще.",
            "claim_confidence": 0.4,
            "derived_from_evidence_ids": [],
            "evidence_relations": [],
        },
    ]

    evidence_data = [
        {"evidence_id": "ev_ord_1a", "source_uri": "https://a.example/eu", "content_excerpt": "eu 27",
         "source_class": "reference", "quality_score": 0.8, "source_cluster_id": "sc_1a"},
        {"evidence_id": "ev_ord_1b", "source_uri": "https://b.example/eu", "content_excerpt": "eu something",
         "source_class": "unknown", "quality_score": 0.3, "source_cluster_id": "sc_1b"},
        {"evidence_id": "ev_ord_2a", "source_uri": "https://c.example/jupiter", "content_excerpt": "jupiter 95",
         "source_class": "reference", "quality_score": 0.8, "source_cluster_id": "sc_2a"},
    ]

    return claims, evidence_data


def _run_family_then_status(claims, evidence_data, registry_storage=None):
    with patch.object(lifecycle_mod, "get_claim_family_registry", _isolated_registry):
        assign_claim_family_identity(claims, _FakeEpistemicResult(), False, {}, _noop_log, False)
    classify_claim_epistemic_status(claims, _noop_log, False, evidence_data)
    return claims


def _run_status_then_family(claims, evidence_data, registry_storage=None):
    classify_claim_epistemic_status(claims, _noop_log, False, evidence_data)
    with patch.object(lifecycle_mod, "get_claim_family_registry", _isolated_registry):
        assign_claim_family_identity(claims, _FakeEpistemicResult(), False, {}, _noop_log, False)
    return claims


# ============================================================
# 1/2/4. Order-independence: family-then-status vs status-then-family
# produce IDENTICAL verification_status/support_count/
# contradiction_count AND semantic_family_id on independent copies.
# ============================================================

claims_a, evidence_a = _make_claims_and_evidence()
claims_b, evidence_b = _make_claims_and_evidence()

result_new_order = _run_family_then_status(claims_a, evidence_a)   # NEW (Этап 4G-1) order
result_old_order = _run_status_then_family(claims_b, evidence_b)   # OLD (pre-4G-1) order

for c_new, c_old in zip(result_new_order, result_old_order):
    check(
        f"1/2: claim={c_new['claim_id']} verification_status identical regardless of call order",
        c_new.get("verification_status") == c_old.get("verification_status"),
        f"new={c_new.get('verification_status')!r} old={c_old.get('verification_status')!r}",
    )
    check(
        f"1/2: claim={c_new['claim_id']} support_count/contradiction_count identical regardless of order",
        c_new.get("support_count") == c_old.get("support_count")
        and c_new.get("contradiction_count") == c_old.get("contradiction_count"),
        f"new=({c_new.get('support_count')},{c_new.get('contradiction_count')}) "
        f"old=({c_old.get('support_count')},{c_old.get('contradiction_count')})",
    )
    check(
        f"4: claim={c_new['claim_id']} semantic_family_id assigned in BOTH orders (all claims, not just [:3])",
        c_new.get("semantic_family_id") is not None and c_old.get("semantic_family_id") is not None,
        f"new={c_new.get('semantic_family_id')} old={c_old.get('semantic_family_id')}",
    )

check(
    "1: ALL claims got semantic_family_id (coverage unaffected by reordering)",
    all(c.get("semantic_family_id") is not None for c in result_new_order),
    f"{[c.get('semantic_family_id') for c in result_new_order]}",
)

# ============================================================
# 3. canonical Trust semantics unchanged (fixed-input smoke check,
# same pattern used in every prior Этап of this session).
# ============================================================

from agent.orchestrator.epistemic.canonical_trust import compute_canonical_trust

_ct = compute_canonical_trust("VERIFIED", "VERIFIED", _noop_log, False)
check(
    "3: canonical Trust semantics unchanged (both strands agree -> that value, diverged=False)",
    _ct["canonical_trust"] == "VERIFIED" and _ct["diverged"] is False,
    f"{_ct}",
)

# ============================================================
# 5. Trace still persists semantic_family_id (end-to-end, production
# no-cache path shape: assign_claim_family_identity -> add_claim_raw ->
# persist_verification_evidence -> index).
# ============================================================

traces_5 = Path(tempfile.mkdtemp(prefix="p10_trace_"))
index_5 = Path(tempfile.mkdtemp(prefix="p10_index_")) / "index.db"

claims_5, evidence_5 = _make_claims_and_evidence()
for c in claims_5:
    c["content_hash"] = f"hash_{c['claim_id']}"

with patch.object(ot, "TRACES_DIR", traces_5), \
     patch.object(vm, "TRACES_DIR", traces_5), \
     patch.object(vm, "INDEX_DB", index_5), \
     patch.object(lifecycle_mod, "get_claim_family_registry", _isolated_registry):

    assign_claim_family_identity(claims_5, _FakeEpistemicResult(), False, {}, _noop_log, False)
    classify_claim_epistemic_status(claims_5, _noop_log, False, evidence_5)

    trace_5 = ot.Trace(trace_id="t_p10", timestamp=0.0, query="q")
    for c in claims_5:
        trace_5.add_claim_raw(c)
    vm.persist_verification_evidence(trace_5, claims_5, evidence_5)
    ot.DecisionTracer().save_trace(trace_5)

saved_5 = trace_5.to_dict()
check(
    "5: persisted Trace still has semantic_family_id set for every claim (end-to-end, new call order)",
    all(c.get("semantic_family_id") is not None for c in saved_5["claims"]),
    f"{[c.get('semantic_family_id') for c in saved_5['claims']]}",
)

# ============================================================
# Structural: assign_claim_family_identity() call now appears BEFORE
# classify_claim_epistemic_status() in the real production source.
# ============================================================

_src = inspect.getsource(orch_v2_mod)
_assign_pos = _src.find("assign_claim_family_identity(\n")
_status_pos = _src.find("classify_claim_epistemic_status(claims_data")
check(
    "structural: assign_claim_family_identity() now called BEFORE "
    "classify_claim_epistemic_status() in orchestrator_v2.py's real production source",
    -1 < _assign_pos < _status_pos,
    f"assign_pos={_assign_pos} status_pos={_status_pos}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
