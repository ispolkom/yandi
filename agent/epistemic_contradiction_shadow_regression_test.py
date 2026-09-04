"""
agent/epistemic_contradiction_shadow_regression_test.py — Этап 4G-3
(P10) regression: EPISTEMIC CONTRADICTION SHADOW.

Covers the user's own 4G-3 spec:
    - Apple negative fixture (persisted contradicts edge stays, but
      evidence-grounded roots don't confirm it -> candidate=False).
    - Positive fixtures: EU 27/28, Jupiter 95/97, Coffee causes/not.
    - Negative fixtures 1-5 (same root both sides; one side only
      uncertain/unrelated; one side only a local_memory replay of a
      root already counted on the other side; network_node/ai_chat
      without a stable root; supports/depends_on edge type never even
      evaluated as a contradiction).
    - Explicit cross-side root collision check matching the exact
      roots_a/roots_b/overlap/distinct/candidate shape from the brief.
    - Structural inertness: claims_data/evidence_data/graph.edges
      byte-identical before and after a run.
    - Fail-open: an exception anywhere inside must never propagate.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_contradiction_shadow_regression_test
"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
from agent.orch_schemas import EvidenceRecord
from agent.family_dependency_graph import FamilyDependencyGraph
import agent.family_dependency_graph as fdg_mod
import contextlib
from agent.epistemic_contradiction_shadow import (
    evaluate_contradiction_event,
    run_epistemic_contradiction_shadow,
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


def _noop_log(*a, **k):
    pass


class _FDFakeCursor:
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
        conn = self.conn
        if upper.startswith("INSERT IGNORE INTO CLAIM_FAMILY"):
            family_id, domain, canonical_text, created_at, updated_at = params
            conn.families.setdefault(family_id, {"family_id": family_id, "domain": domain, "canonical_text": canonical_text})
        elif upper.startswith("SELECT * FROM SEMANTIC_EDGE WHERE FAMILY_A=%S AND FAMILY_B=%S AND EDGE_TYPE=%S"):
            fam_a, fam_b, edge_type = params
            self._result = next(
                (dict(e) for e in conn.edges.values() if e["family_a"] == fam_a and e["family_b"] == fam_b and e["edge_type"] == edge_type),
                None,
            )
        elif upper.startswith("INSERT INTO SEMANTIC_EDGE"):
            edge_id, family_a, family_b, edge_type, reason, triggering, created_at, last_seen_at = params
            conn.edges[edge_id] = {
                "edge_id": edge_id, "family_a": family_a, "family_b": family_b, "edge_type": edge_type,
                "reason": reason, "observation_count": 1, "triggering_claim_ids": triggering,
                "created_at": created_at, "last_seen_at": last_seen_at,
            }
        elif upper.startswith("UPDATE SEMANTIC_EDGE SET OBSERVATION_COUNT"):
            last_seen_at, triggering, edge_id = params
            e = conn.edges[edge_id]
            e["observation_count"] += 1
            e["last_seen_at"] = last_seen_at
            e["triggering_claim_ids"] = triggering
        elif upper.startswith("SELECT * FROM SEMANTIC_EDGE WHERE FAMILY_B=%S AND EDGE_TYPE='DEPENDS_ON'"):
            (family_b,) = params
            self._results = [dict(e) for e in conn.edges.values() if e["family_b"] == family_b and e["edge_type"] == "depends_on"]
        elif upper.startswith("SELECT * FROM SEMANTIC_EDGE WHERE EDGE_TYPE='CONTRADICTS'"):
            self._results = [dict(e) for e in conn.edges.values() if e["edge_type"] == "contradicts"]
        elif upper.startswith("SELECT * FROM FAMILY_STATUS_STATE WHERE FAMILY_ID=%S"):
            (family_id,) = params
            self._result = dict(conn.status[family_id]) if family_id in conn.status else None
        elif upper.startswith("INSERT INTO FAMILY_STATUS_STATE"):
            family_id, last_status, updated_at = params
            conn.status[family_id] = {"family_id": family_id, "last_status": last_status, "updated_at": updated_at}
        elif upper.startswith("SELECT * FROM RECHECK_EVENT WHERE FAMILY_ID=%S ORDER BY STARTED_AT DESC LIMIT 1"):
            (family_id,) = params
            rows = [r for r in conn.rechecks if r["family_id"] == family_id]
            rows.sort(key=lambda r: r["started_at"])
            self._result = dict(rows[-1]) if rows else None
        elif upper.startswith("INSERT INTO RECHECK_EVENT"):
            family_id, run_id, trigger_reason, started_at, outcome, reason = params
            conn.rechecks.append({
                "family_id": family_id, "run_id": run_id, "trigger_reason": trigger_reason,
                "started_at": started_at, "outcome": outcome, "reason": reason,
            })

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class _FDFakeConnection:
    def __init__(self):
        self.families = {}
        self.edges = {}
        self.status = {}
        self.rechecks = []

    def cursor(self):
        return _FDFakeCursor(self)

    def commit(self):
        pass


def _fresh_graph():
    """A brand-new FamilyDependencyGraph backed by a fresh, empty fake
    claim_family/semantic_edge/family_status_state/recheck_event store
    ("точка ноль": no more storage_file= — see agent/family_dependency_
    graph.py's own rewrite)."""
    conn = _FDFakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    fdg_mod.get_connection = _fake_get_connection
    return FamilyDependencyGraph()



def _make_env():
    traces_dir = Path(tempfile.mkdtemp(prefix="p10_ecs_traces_"))
    index_db = Path(tempfile.mkdtemp(prefix="p10_ecs_index_")) / "index.db"
    return traces_dir, index_db


def _persist_historical(
    *,
    trace_id: str,
    claim_id: str,
    content_hash: str,
    family_id: str,
    evidence_id: str,
    source_uri: str,
    relation: str = "supports",
    route: str = "internet",
    origin_route=None,
    origin_trace_id=None,
    evidence_role: str = "direct",
    evidence_eligible: bool = True,
    source_class: str = "reference",
    directness: float = 0.8,
    retrieval_origin: str = "initial_web",
):
    """Persists one HISTORICAL claim+evidence occurrence through the
    real Trace/DecisionTracer save path (must be called inside a
    context where ot.TRACES_DIR/vm.TRACES_DIR/vm.INDEX_DB are already
    patched to test-local paths)."""
    trace = ot.Trace(trace_id=trace_id, timestamp=0.0, query="q")
    trace.add_claim_raw({
        "claim_id": claim_id,
        "claim_text": f"claim text for {claim_id}",
        "claim_confidence": 0.5,
        "content_hash": content_hash,
        "semantic_family_id": family_id,
        "derived_from_evidence_ids": [evidence_id],
        "evidence_relations": [
            {
                "evidence_id": evidence_id,
                "relation": relation,
                "evidence_role": evidence_role,
                "evidence_eligible": evidence_eligible,
                "source_class": source_class,
                "directness": directness,
                "retrieval_origin": retrieval_origin,
            },
        ],
    })
    trace.add_evidence(EvidenceRecord(
        evidence_id=evidence_id,
        source_type="web",
        source_uri=source_uri,
        content_excerpt="some excerpt text",
        source_class=source_class,
        evidence_eligible=evidence_eligible,
        evidence_role=evidence_role,
        route=route,
        origin_route=origin_route,
        origin_trace_id=origin_trace_id,
    ))
    ot.DecisionTracer().save_trace(trace)


def _current_claim(claim_id, family_id, evidence_id, relation="supports",
                    evidence_role="direct", evidence_eligible=True,
                    source_class="reference", directness=0.8,
                    retrieval_origin="claim_specific"):
    return {
        "claim_id": claim_id,
        "claim_text": f"current claim {claim_id}",
        "semantic_family_id": family_id,
        "evidence_relations": [
            {
                "evidence_id": evidence_id,
                "relation": relation,
                "evidence_role": evidence_role,
                "evidence_eligible": evidence_eligible,
                "source_class": source_class,
                "directness": directness,
                "retrieval_origin": retrieval_origin,
            },
        ],
    }


def _current_evidence(evidence_id, source_uri, route="internet", origin_route=None):
    return {
        "evidence_id": evidence_id,
        "source_uri": source_uri,
        "route": route,
        "origin_route": origin_route,
    }


# ============================================================
# APPLE NEGATIVE FIXTURE (mandatory): persisted contradicts edge
# between two families whose claims disagree on Apple's founding
# year, but NEITHER side has any real supporting evidence root behind
# it — exactly the real Этап 4E finding (pure claim<->claim NLI
# artifact, evidence-free).
# ============================================================

traces_apple, index_apple = _make_env()
graph_apple = _fresh_graph()
graph_apple.record_edge("fam_apple_1976", "fam_apple_1975", "contradicts", "claim_claim_nli:contradicts", ["cl_1976", "cl_1975"])
graph_apple.record_edge("fam_apple_1975", "fam_apple_1976", "contradicts", "claim_claim_nli:contradicts", ["cl_1976", "cl_1975"])

claims_apple = [
    {"claim_id": "cl_1976", "claim_text": "Apple была зарегистрирована в 1976 году.",
     "semantic_family_id": "fam_apple_1976", "evidence_relations": []},
    {"claim_id": "cl_1975", "claim_text": "Apple была зарегистрирована в 1975 году.",
     "semantic_family_id": "fam_apple_1975", "evidence_relations": []},
]
evidence_apple = []

with patch.object(ot, "TRACES_DIR", traces_apple), patch.object(vm, "TRACES_DIR", traces_apple), patch.object(vm, "INDEX_DB", index_apple):
    stats_apple = run_epistemic_contradiction_shadow(
        claims_apple, evidence_apple, graph=graph_apple, log=_noop_log, verbose=False,
    )

check(
    "APPLE NEGATIVE: contradicts edge persists in the graph (untouched)",
    len(graph_apple.all_contradicts_edges()) == 2,
    f"{graph_apple.all_contradicts_edges()}",
)
check(
    "APPLE NEGATIVE: exactly one edge evaluated (both directions collapsed to one pair)",
    stats_apple["contradicts_edges_evaluated"] == 1,
    f"{stats_apple}",
)
check(
    "APPLE NEGATIVE: candidate=False (no evidence-grounded roots on either side)",
    stats_apple["events"][0]["candidate"] is False
    and stats_apple["events"][0]["roots_a"] == 0
    and stats_apple["events"][0]["roots_b"] == 0
    and stats_apple["events"][0]["reason"] == "no_support_root_either_side",
    f"{stats_apple['events'][0]}",
)

# ============================================================
# POSITIVE FIXTURES: EU 27/28, Jupiter 95/97, Coffee causes/not —
# each side has >=1 eligible SUPPORTING root, from genuinely different
# URLs (one historical, one current, to also prove the two-layer merge
# works end to end).
# ============================================================

def _run_positive_fixture(name, fam_a, fam_b, url_a, url_b):
    traces_dir, index_db = _make_env()
    graph = _fresh_graph()
    graph.record_edge(fam_a, fam_b, "contradicts", "claim_claim_nli:contradicts", [f"cl_{name}_a", f"cl_{name}_b"])
    graph.record_edge(fam_b, fam_a, "contradicts", "claim_claim_nli:contradicts", [f"cl_{name}_a", f"cl_{name}_b"])

    with patch.object(ot, "TRACES_DIR", traces_dir), patch.object(vm, "TRACES_DIR", traces_dir), patch.object(vm, "INDEX_DB", index_db):
        _persist_historical(
            trace_id=f"t_{name}_a", claim_id=f"cl_{name}_a_hist", content_hash=f"h_{name}_a",
            family_id=fam_a, evidence_id=f"ev_{name}_a", source_uri=url_a,
        )
        claims = [
            {"claim_id": f"cl_{name}_a", "claim_text": "x", "semantic_family_id": fam_a, "evidence_relations": []},
            _current_claim(f"cl_{name}_b", fam_b, f"ev_{name}_b"),
        ]
        evidence = [_current_evidence(f"ev_{name}_b", url_b)]

        stats = run_epistemic_contradiction_shadow(claims, evidence, graph=graph, log=_noop_log, verbose=False)

    return stats


for fixture_name, fam_a, fam_b, url_a, url_b, label in [
    ("eu", "fam_eu27", "fam_eu28", "https://eu.example/27-states", "https://eu.example/28-states", "EU 27 vs 28"),
    ("jupiter", "fam_j95", "fam_j97", "https://astro.example/jupiter-95", "https://astro.example/jupiter-97", "Jupiter 95 vs 97"),
    ("coffee", "fam_causes", "fam_not_causes", "https://health.example/coffee-causes", "https://health.example/coffee-not-causes", "Coffee causes vs not"),
]:
    stats = _run_positive_fixture(fixture_name, fam_a, fam_b, url_a, url_b)
    check(
        f"POSITIVE ({label}): candidate=True with independent roots on both sides",
        stats["contradicts_edges_evaluated"] == 1
        and stats["events"][0]["candidate"] is True
        and stats["events"][0]["roots_a"] == 1
        and stats["events"][0]["roots_b"] == 1
        and stats["events"][0]["distinct"] == 2
        and stats["events"][0]["reason"] == "independent_support_both_sides",
        f"{stats['events'][0] if stats['events'] else stats}",
    )

# ============================================================
# NEGATIVE FIXTURE 1 / CROSS-SIDE ROOT COLLISION (explicit, exact
# shape from the brief): the SAME document supports both mutually
# exclusive claims (ambiguous excerpt) -> roots_a=={R}, roots_b=={R},
# overlap=1, distinct=1 -> candidate=False. NOT 1+1=2.
# ============================================================

traces_n1, index_n1 = _make_env()
graph_n1 = _fresh_graph()
graph_n1.record_edge("fam_n1_a", "fam_n1_b", "contradicts", "claim_claim_nli:contradicts", ["cl_n1_a", "cl_n1_b"])
graph_n1.record_edge("fam_n1_b", "fam_n1_a", "contradicts", "claim_claim_nli:contradicts", ["cl_n1_a", "cl_n1_b"])

SAME_URL = "https://ambiguous.example/one-doc"
claims_n1 = [
    _current_claim("cl_n1_a", "fam_n1_a", "ev_n1_a"),
    _current_claim("cl_n1_b", "fam_n1_b", "ev_n1_b"),
]
evidence_n1 = [
    _current_evidence("ev_n1_a", SAME_URL),
    _current_evidence("ev_n1_b", SAME_URL),
]

with patch.object(ot, "TRACES_DIR", traces_n1), patch.object(vm, "TRACES_DIR", traces_n1), patch.object(vm, "INDEX_DB", index_n1):
    stats_n1 = run_epistemic_contradiction_shadow(claims_n1, evidence_n1, graph=graph_n1, log=_noop_log, verbose=False)

ev1 = stats_n1["events"][0]
check(
    "NEG1/COLLISION: same root supports both sides -> roots_a=1 roots_b=1 overlap=1 distinct=1",
    ev1["roots_a"] == 1 and ev1["roots_b"] == 1 and ev1["overlap"] == 1 and ev1["distinct"] == 1,
    f"{ev1}",
)
check(
    "NEG1/COLLISION: candidate=False, NOT counted as 1+1=2",
    ev1["candidate"] is False and ev1["reason"] == "roots_collide_not_independent",
    f"{ev1}",
)

# ============================================================
# NEGATIVE FIXTURE 2: one side has only uncertain/unrelated evidence
# (never relation=="supports") -> that side contributes zero roots.
# ============================================================

traces_n2, index_n2 = _make_env()
graph_n2 = _fresh_graph()
graph_n2.record_edge("fam_n2_a", "fam_n2_b", "contradicts", "claim_claim_nli:contradicts", ["cl_n2_a", "cl_n2_b"])
graph_n2.record_edge("fam_n2_b", "fam_n2_a", "contradicts", "claim_claim_nli:contradicts", ["cl_n2_a", "cl_n2_b"])

claims_n2 = [
    _current_claim("cl_n2_a", "fam_n2_a", "ev_n2_a", relation="uncertain"),
    _current_claim("cl_n2_b", "fam_n2_b", "ev_n2_b", relation="supports"),
]
evidence_n2 = [
    _current_evidence("ev_n2_a", "https://x.example/uncertain"),
    _current_evidence("ev_n2_b", "https://x.example/legit"),
]

with patch.object(ot, "TRACES_DIR", traces_n2), patch.object(vm, "TRACES_DIR", traces_n2), patch.object(vm, "INDEX_DB", index_n2):
    stats_n2 = run_epistemic_contradiction_shadow(claims_n2, evidence_n2, graph=graph_n2, log=_noop_log, verbose=False)

ev2 = stats_n2["events"][0]
check(
    "NEG2: relation=='uncertain' contributes zero roots even though evidence_eligible=True",
    ev2["roots_a"] == 0 and ev2["roots_b"] == 1 and ev2["candidate"] is False
    and ev2["reason"] == "no_support_root_family_a",
    f"{ev2}",
)

# ============================================================
# NEGATIVE FIXTURE 3: one side has ONLY a local_memory replay of a
# root already counted on the OTHER side -> does not add independence.
# ============================================================

traces_n3, index_n3 = _make_env()
graph_n3 = _fresh_graph()
graph_n3.record_edge("fam_n3_a", "fam_n3_b", "contradicts", "claim_claim_nli:contradicts", ["cl_n3_a", "cl_n3_b"])
graph_n3.record_edge("fam_n3_b", "fam_n3_a", "contradicts", "claim_claim_nli:contradicts", ["cl_n3_a", "cl_n3_b"])

REPLAY_URL = "https://replay.example/same-story"
with patch.object(ot, "TRACES_DIR", traces_n3), patch.object(vm, "TRACES_DIR", traces_n3), patch.object(vm, "INDEX_DB", index_n3):
    _persist_historical(
        trace_id="t_n3_a", claim_id="cl_n3_a_hist", content_hash="h_n3_a",
        family_id="fam_n3_a", evidence_id="ev_n3_a_hist", source_uri=REPLAY_URL,
    )
    claims_n3 = [
        {"claim_id": "cl_n3_a", "claim_text": "x", "semantic_family_id": "fam_n3_a", "evidence_relations": []},
        _current_claim("cl_n3_b", "fam_n3_b", "ev_n3_b"),
    ]
    evidence_n3 = [_current_evidence("ev_n3_b", REPLAY_URL, route="local_memory", origin_route="internet")]

    stats_n3 = run_epistemic_contradiction_shadow(claims_n3, evidence_n3, graph=graph_n3, log=_noop_log, verbose=False)

ev3 = stats_n3["events"][0]
check(
    "NEG3: local_memory replay of a root already on the other side does not add independence "
    "(roots_a=1 roots_b=1 but SAME root -> distinct=1)",
    ev3["roots_a"] == 1 and ev3["roots_b"] == 1 and ev3["distinct"] == 1 and ev3["candidate"] is False,
    f"{ev3}",
)

# ============================================================
# NEGATIVE FIXTURE 4: network_node/ai_chat observation has no stable
# root in V1 -> never counted as independent, even if it's the only
# "supporting" evidence on that side.
# ============================================================

traces_n4, index_n4 = _make_env()
graph_n4 = _fresh_graph()
graph_n4.record_edge("fam_n4_a", "fam_n4_b", "contradicts", "claim_claim_nli:contradicts", ["cl_n4_a", "cl_n4_b"])
graph_n4.record_edge("fam_n4_b", "fam_n4_a", "contradicts", "claim_claim_nli:contradicts", ["cl_n4_a", "cl_n4_b"])

claims_n4 = [
    _current_claim("cl_n4_a", "fam_n4_a", "ev_n4_a"),
    _current_claim("cl_n4_b", "fam_n4_b", "ev_n4_b"),
]
evidence_n4 = [
    _current_evidence("ev_n4_a", "n/a", route="network_node"),
    _current_evidence("ev_n4_b", "https://y.example/real", route="internet"),
]

with patch.object(ot, "TRACES_DIR", traces_n4), patch.object(vm, "TRACES_DIR", traces_n4), patch.object(vm, "INDEX_DB", index_n4):
    stats_n4 = run_epistemic_contradiction_shadow(claims_n4, evidence_n4, graph=graph_n4, log=_noop_log, verbose=False)

ev4 = stats_n4["events"][0]
check(
    "NEG4: network_node evidence never counted as an independent root in V1",
    ev4["roots_a"] == 0 and ev4["roots_b"] == 1 and ev4["candidate"] is False
    and ev4["reason"] == "no_support_root_family_a",
    f"{ev4}",
)

# ============================================================
# NEGATIVE FIXTURE 5: edge_type == "supports"/"depends_on" is never
# even evaluated as a contradiction candidate (structurally excluded
# by _distinct_contradicts_pairs, not filtered post-hoc).
# ============================================================

traces_n5, index_n5 = _make_env()
graph_n5 = _fresh_graph()
graph_n5.record_edge("fam_n5_a", "fam_n5_b", "supports", "claim_claim_nli:supports", ["cl_n5_a", "cl_n5_b"])
graph_n5.record_edge("fam_n5_c", "fam_n5_d", "depends_on", "contradicts", ["cl_n5_c", "cl_n5_d"])

with patch.object(ot, "TRACES_DIR", traces_n5), patch.object(vm, "TRACES_DIR", traces_n5), patch.object(vm, "INDEX_DB", index_n5):
    stats_n5 = run_epistemic_contradiction_shadow([], [], graph=graph_n5, log=_noop_log, verbose=False)

check(
    "NEG5: supports/depends_on edges never enter the contradiction evaluation at all",
    stats_n5["contradicts_edges_evaluated"] == 0 and stats_n5["events"] == [],
    f"{stats_n5}",
)

# ============================================================
# STRUCTURAL: inertness — claims_data/evidence_data/graph.edges are
# byte-identical before and after a run (deepcopy comparison).
# ============================================================

traces_s, index_s = _make_env()
graph_s = _fresh_graph()
graph_s.record_edge("fam_s_a", "fam_s_b", "contradicts", "claim_claim_nli:contradicts", ["cl_s_a", "cl_s_b"])
graph_s.record_edge("fam_s_b", "fam_s_a", "contradicts", "claim_claim_nli:contradicts", ["cl_s_a", "cl_s_b"])

claims_s = [
    _current_claim("cl_s_a", "fam_s_a", "ev_s_a"),
    _current_claim("cl_s_b", "fam_s_b", "ev_s_b"),
]
evidence_s = [
    _current_evidence("ev_s_a", "https://s.example/a"),
    _current_evidence("ev_s_b", "https://s.example/b"),
]
claims_s_before = copy.deepcopy(claims_s)
evidence_s_before = copy.deepcopy(evidence_s)
edges_s_before = copy.deepcopy(graph_s.all_contradicts_edges())

with patch.object(ot, "TRACES_DIR", traces_s), patch.object(vm, "TRACES_DIR", traces_s), patch.object(vm, "INDEX_DB", index_s):
    run_epistemic_contradiction_shadow(claims_s, evidence_s, graph=graph_s, log=_noop_log, verbose=False)

check(
    "STRUCTURAL: claims_data unchanged after run (no mutation)",
    claims_s == claims_s_before,
    f"before={claims_s_before} after={claims_s}",
)
check(
    "STRUCTURAL: evidence_data unchanged after run (no mutation)",
    evidence_s == evidence_s_before,
    f"before={evidence_s_before} after={evidence_s}",
)
check(
    "STRUCTURAL: graph's contradicts edges unchanged after run (no new/mutated edges)",
    graph_s.all_contradicts_edges() == edges_s_before,
    f"before={edges_s_before} after={graph_s.all_contradicts_edges()}",
)

# ============================================================
# FAIL-OPEN: an exception anywhere inside must never propagate, and
# must return a safe/empty stats dict with error set.
# ============================================================

class _BrokenGraph:
    def all_contradicts_edges(self):
        raise RuntimeError("simulated graph corruption")

stats_broken = run_epistemic_contradiction_shadow(
    [], [], graph=_BrokenGraph(), log=_noop_log, verbose=True,
)
check(
    "FAIL-OPEN: exception inside is caught, never propagates, safe dict returned",
    stats_broken["contradicts_edges_evaluated"] == 0
    and stats_broken["events"] == []
    and stats_broken["error"] is not None,
    f"{stats_broken}",
)

# ============================================================
# evaluate_contradiction_event() direct unit check (bypassing the
# graph entirely) — same EU fixture, called directly.
# ============================================================

traces_u, index_u = _make_env()
with patch.object(ot, "TRACES_DIR", traces_u), patch.object(vm, "TRACES_DIR", traces_u), patch.object(vm, "INDEX_DB", index_u):
    _persist_historical(
        trace_id="t_u_a", claim_id="cl_u_a_hist", content_hash="h_u_a",
        family_id="fam_u_a", evidence_id="ev_u_a", source_uri="https://u.example/a",
    )
    evidence_by_id_u = {"ev_u_b": _current_evidence("ev_u_b", "https://u.example/b")}
    claims_u = [_current_claim("cl_u_b", "fam_u_b", "ev_u_b")]

    event_u = evaluate_contradiction_event("fam_u_a", "fam_u_b", claims_u, evidence_by_id_u)

check(
    "UNIT: evaluate_contradiction_event() callable directly (no graph needed), candidate=True",
    event_u["candidate"] is True and event_u["roots_a"] == 1 and event_u["roots_b"] == 1,
    f"{event_u}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
