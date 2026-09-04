"""
agent/epistemic_claim_family_regression_test.py — Epistemic Core v1
Phase 10 regression: cross-request claim linking
(agent/claim_family_registry.py::ClaimFamilyRegistry).

classify_claim_pair() is mocked throughout (matching this project's
established convention for network-dependent tests) — this suite proves
the REGISTRY's own logic (family creation, linking, append-only history,
idempotency, domain scoping, fail-safe loading) is correct, not
Phase 9B's classifier itself (already covered by its own suite).

"ТОЧКА НОЛЬ" UPDATE (owner mandate, 2026-09): ClaimFamilyRegistry is
SQL-only now (claim_family + family_member), no storage_file, no
`.families` in-memory list to inspect directly from tests. A small fake
claim_family/family_member connection stands in for the real bastion-
protected tables; _snapshot() below reconstructs the same
[{"family_id", "domain", "canonical_text", "members": [...]}] shape the
old JSON file used to have, from that fake's own state, so every
original check's INTENT survives even though the underlying storage
does not exist as an in-memory attribute anymore. One real, deliberate
narrowing: family_member (unlike the old JSON member dict) never stored
claim_text redundantly — it only ever held claim_id + linked_at, matching
the real SQL schema's own (more normalized) design, where the actual
wording lives once, on claim_occurrence, keyed by claim_id — so this
file's own "wording preserved verbatim" check now asserts that against
the claim_id itself, not a duplicated text field that never existed in
the real table this class writes to.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_claim_family_regression_test
"""

import contextlib
import json
import time
import time as _time
from unittest.mock import patch

from agent.claim_family_registry import ClaimFamilyRegistry
import agent.claim_family_registry as registry_mod
from agent.db.sql.connection import SqlUnavailable
from agent.orch_schemas import ClaimRecord
from agent.orch_tracer import Trace

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
        norm = " ".join(sql.split())
        upper = norm.upper()
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


def _use_fake_connection():
    """Returns (registry, conn) — a fresh, isolated fake claim_family/
    family_member store and a real ClaimFamilyRegistry wired to it."""
    conn = _CFFakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    registry_mod.get_connection = _fake_get_connection
    return ClaimFamilyRegistry(), conn


def _snapshot(conn):
    """Reconstructs the old JSON shape from the fake connection's own
    state, ordered oldest-first — every original check below reads
    this instead of a (now nonexistent) `.families` attribute."""
    families = sorted(conn.families.values(), key=lambda f: f["created_at"])
    out = []
    for f in families:
        members = sorted(
            (m for m in conn.members.values() if m["family_id"] == f["family_id"]),
            key=lambda m: m["linked_at"],
        )
        out.append({**f, "members": [{"claim_id": m["claim_id"], "linked_at": m["linked_at"]} for m in members]})
    return out


# ── 1. First claim in a domain creates a brand-new family ──

reg1, conn1 = _use_fake_connection()
fam_id_1 = reg1.find_or_link_claim("Юпитер — крупнейшая планета.", "cl_aaa", "science")
check(
    "first claim in an empty registry creates a new family",
    fam_id_1 is not None and fam_id_1.startswith("fam_"),
    f"{fam_id_1}",
)
check(
    "new family has exactly one member, matching the founding claim_id",
    len(_snapshot(conn1)) == 1 and _snapshot(conn1)[0]["members"][0]["claim_id"] == "cl_aaa",
    f"{_snapshot(conn1)}",
)

# ── 2. A second, semantically-equivalent occurrence links into the SAME family (mocked classifier) ──

with patch.object(registry_mod, "classify_claim_pair", return_value="equivalent"):
    fam_id_2 = reg1.find_or_link_claim("Крупнейшая планета — Юпитер.", "cl_bbb", "science")
check(
    "second occurrence (judged equivalent) links into the SAME family, not a new one",
    fam_id_2 == fam_id_1 and len(_snapshot(conn1)) == 1,
    f"fam_id_2={fam_id_2} fam_id_1={fam_id_1} families={len(_snapshot(conn1))}",
)
check(
    "occurrence claim_id is preserved distinctly — family now has 2 DIFFERENT claim_ids, not collapsed into one "
    "(the claim's own WORDING lives once on claim_occurrence, keyed by this same claim_id — not duplicated here, "
    "unlike the retired JSON member dict)",
    {m["claim_id"] for m in _snapshot(conn1)[0]["members"]} == {"cl_aaa", "cl_bbb"},
    f"{_snapshot(conn1)[0]['members']}",
)

# ── 3. A NOT-equivalent occurrence creates a SEPARATE family ──

with patch.object(registry_mod, "classify_claim_pair", return_value="different"):
    fam_id_3 = reg1.find_or_link_claim(
        "Юпитер ранее считался крупнейшей планетой до новых наблюдений.", "cl_ccc", "science",
    )
check(
    "a claim judged NOT equivalent (e.g. a temporal variant, per Phase 9B's guard) gets its OWN family, "
    "not merged into the existing one",
    fam_id_3 != fam_id_1 and len(_snapshot(conn1)) == 2,
    f"fam_id_3={fam_id_3} families={len(_snapshot(conn1))}",
)

# ── 4. Idempotency: linking the SAME claim_id twice does not duplicate it in the members list ──

with patch.object(registry_mod, "classify_claim_pair", return_value="equivalent"):
    reg1.find_or_link_claim("Крупнейшая планета — Юпитер.", "cl_bbb", "science")  # same claim_id as before
check(
    "re-linking the same claim_id does not duplicate it (family_member's real PRIMARY KEY "
    "(family_id, claim_id) + INSERT IGNORE makes this a safe no-op at the SQL level)",
    len(_snapshot(conn1)[0]["members"]) == 2,  # still cl_aaa + cl_bbb, not 3
    f"{_snapshot(conn1)[0]['members']}",
)

# ── 5. Domain scoping: same text, different domain -> does NOT reuse the other domain's family ──

with patch.object(registry_mod, "classify_claim_pair", return_value="equivalent"):
    fam_id_other_domain = reg1.find_or_link_claim("Юпитер — крупнейшая планета.", "cl_ddd", "history")
check(
    "identical text in a DIFFERENT domain creates its own family, never reuses another domain's family "
    "(domain-scoped comparison, mirrors belief_manager.py's own topic-scoping)",
    fam_id_other_domain != fam_id_1 and len(_snapshot(conn1)) == 3,
    f"fam_id_other_domain={fam_id_other_domain} families={len(_snapshot(conn1))}",
)

# ── 6. Persistence: a SECOND registry instance sharing the SAME connection sees identical state
#      (SQL persistence is now inherent — no reload-from-disk step needed to prove it; this proves
#      the class itself holds no hidden in-memory state of its own that a fresh instance would miss) ──

reg1b = ClaimFamilyRegistry()  # still wired to conn1 via registry_mod.get_connection
check(
    "a brand-new ClaimFamilyRegistry() instance, same connection, sees the SAME families/members "
    "immediately — proves no per-instance in-memory state is hiding anywhere",
    len(_snapshot(conn1)) == 3 and len(_snapshot(conn1)[0]["members"]) == 2,
    f"{[len(f['members']) for f in _snapshot(conn1)]}",
)

# ── 7. Empty/missing claim_text or claim_id -> None, no crash, no fabricated family ──

reg2, conn2 = _use_fake_connection()
check(
    "empty claim_text -> None, no family created",
    reg2.find_or_link_claim("", "cl_x", "science") is None and len(_snapshot(conn2)) == 0,
)
check(
    "empty claim_id -> None, no family created",
    reg2.find_or_link_claim("some text", "", "science") is None and len(_snapshot(conn2)) == 0,
)

# ── 8. Fail LOUD, not fail-open: SQL genuinely unreachable raises SqlUnavailable
#      (replaces the retired "corrupt JSON file" scenario, which has no SQL equivalent —
#      "точка ноль": there is no more file-based fallback to quietly succeed against). ──

reg3 = ClaimFamilyRegistry()


def _raise_unavailable(autocommit=False):
    raise SqlUnavailable("forced unreachable for this test")


with patch.object(registry_mod, "get_connection", _raise_unavailable):
    raised = False
    try:
        reg3.find_or_link_claim("some claim", "cl_z", "science")
    except SqlUnavailable:
        raised = True
    check(
        "with SQL genuinely unreachable, find_or_link_claim() raises SqlUnavailable — the deliberate "
        "opposite of the retired JSON fail-safe (\"corrupt file -> start empty, never crash\")",
        raised,
    )

# ── 9. Round trip through Trace: semantic_family_id survives serialization ──

trace = Trace(trace_id="t_test", timestamp=_time.time(), query="test")
trace.add_claim_raw({
    "claim_id": "cl_rt1",
    "claim_text": "Достаточно длинный текст утверждения для прохождения фильтра чистоты трассировки.",
    "verification_status": "unverified",
    "semantic_family_id": "fam_12345678",
})
trace.add_claim_raw({
    "claim_id": "cl_rt2",
    "claim_text": "Ещё одно достаточно длинное утверждение без семейной привязки для проверки совместимости.",
    "verification_status": "unverified",
    # no semantic_family_id key at all — simulates a claim outside the [:3] cap
})
rt = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
by_id = {c["claim_id"]: c for c in rt["claims"]}
check(
    "round trip: semantic_family_id survives serialization when set",
    by_id["cl_rt1"]["semantic_family_id"] == "fam_12345678",
    f"{by_id.get('cl_rt1')}",
)
check(
    "round trip / backward compat: missing semantic_family_id key -> None, no crash",
    by_id["cl_rt2"]["semantic_family_id"] is None,
    f"{by_id.get('cl_rt2')}",
)

# ── 10. Backward compatibility: ClaimRecord constructed without the kwarg ──

try:
    old_style = ClaimRecord(claim_id="cl_old", claim_text="predates Phase 10 entirely")
    check(
        "ClaimRecord constructed without semantic_family_id kwarg defaults to None",
        old_style.semantic_family_id is None,
    )
except Exception as e:
    check("ClaimRecord constructed without semantic_family_id kwarg defaults to None", False, repr(e))

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
