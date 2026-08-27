"""
agent/epistemic_claim_family_regression_test.py — Epistemic Core v1
Phase 10 regression: cross-request claim linking
(agent/claim_family_registry.py::ClaimFamilyRegistry).

classify_claim_pair() is mocked throughout (matching this project's
established convention for network-dependent tests) — this suite proves
the REGISTRY's own logic (family creation, linking, append-only history,
idempotency, domain scoping, fail-safe loading) is correct, not
Phase 9B's classifier itself (already covered by its own suite). All
tests use a scratch storage_file — the real registry/claim_families.json
is never touched.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_claim_family_regression_test
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.claim_family_registry import ClaimFamilyRegistry
import agent.claim_family_registry as registry_mod
from agent.orch_schemas import ClaimRecord
from agent.orch_tracer import Trace
import time as _time

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


def _scratch_registry():
    tmp = Path(tempfile.mkdtemp()) / "claim_families_test.json"
    return ClaimFamilyRegistry(storage_file=tmp), tmp


# ── 1. First claim in a domain creates a brand-new family ──

reg1, path1 = _scratch_registry()
fam_id_1 = reg1.find_or_link_claim("Юпитер — крупнейшая планета.", "cl_aaa", "science")
check(
    "first claim in an empty registry creates a new family",
    fam_id_1 is not None and fam_id_1.startswith("fam_"),
    f"{fam_id_1}",
)
check(
    "new family has exactly one member, matching the founding claim_id and text",
    len(reg1.families) == 1
    and reg1.families[0]["members"][0]["claim_id"] == "cl_aaa"
    and reg1.families[0]["members"][0]["claim_text"] == "Юпитер — крупнейшая планета.",
    f"{reg1.families}",
)

# ── 2. A second, semantically-equivalent occurrence links into the SAME family (mocked classifier) ──

with patch.object(registry_mod, "classify_claim_pair", return_value="equivalent"):
    fam_id_2 = reg1.find_or_link_claim("Крупнейшая планета — Юпитер.", "cl_bbb", "science")
check(
    "second occurrence (judged equivalent) links into the SAME family, not a new one",
    fam_id_2 == fam_id_1 and len(reg1.families) == 1,
    f"fam_id_2={fam_id_2} fam_id_1={fam_id_1} families={len(reg1.families)}",
)
check(
    "occurrence claim_id is preserved distinctly — family now has 2 DIFFERENT claim_ids, not collapsed into one",
    {m["claim_id"] for m in reg1.families[0]["members"]} == {"cl_aaa", "cl_bbb"},
    f"{reg1.families[0]['members']}",
)
check(
    "wording of the second occurrence is preserved verbatim, not normalized away",
    any(m["claim_text"] == "Крупнейшая планета — Юпитер." for m in reg1.families[0]["members"]),
    f"{reg1.families[0]['members']}",
)

# ── 3. A NOT-equivalent occurrence creates a SEPARATE family (proves the wiring respects the classifier's verdict, "
#      including for temporal variants — Phase 9B's hardening_guard already proved it returns 'different' there) ──

with patch.object(registry_mod, "classify_claim_pair", return_value="different"):
    fam_id_3 = reg1.find_or_link_claim(
        "Юпитер ранее считался крупнейшей планетой до новых наблюдений.", "cl_ccc", "science",
    )
check(
    "a claim judged NOT equivalent (e.g. a temporal variant, per Phase 9B's guard) gets its OWN family, "
    "not merged into the existing one",
    fam_id_3 != fam_id_1 and len(reg1.families) == 2,
    f"fam_id_3={fam_id_3} families={len(reg1.families)}",
)

# ── 4. Idempotency: linking the SAME claim_id twice does not duplicate it in the members list ──

with patch.object(registry_mod, "classify_claim_pair", return_value="equivalent"):
    reg1.find_or_link_claim("Крупнейшая планета — Юпитер.", "cl_bbb", "science")  # same claim_id as before
check(
    "re-linking the same claim_id does not duplicate it in the family's members list",
    len(reg1.families[0]["members"]) == 2,  # still cl_aaa + cl_bbb, not 3
    f"{reg1.families[0]['members']}",
)

# ── 5. Domain scoping: same text, different domain -> does NOT reuse the other domain's family ──

with patch.object(registry_mod, "classify_claim_pair", return_value="equivalent"):
    fam_id_other_domain = reg1.find_or_link_claim("Юпитер — крупнейшая планета.", "cl_ddd", "history")
check(
    "identical text in a DIFFERENT domain creates its own family, never reuses another domain's family "
    "(domain-scoped comparison, mirrors belief_manager.py's own topic-scoping)",
    fam_id_other_domain != fam_id_1 and len(reg1.families) == 3,
    f"fam_id_other_domain={fam_id_other_domain} families={len(reg1.families)}",
)

# ── 6. Persistence: reload from disk reproduces the same family structure ──

reg1b = ClaimFamilyRegistry(storage_file=path1)
check(
    "reloading the registry from disk reproduces the same number of families and members",
    len(reg1b.families) == 3
    and len(reg1b.families[0]["members"]) == 2,
    f"{[len(f['members']) for f in reg1b.families]}",
)

# ── 7. Empty/missing claim_text or claim_id -> None, no crash, no fabricated family ──

reg2, _ = _scratch_registry()
check(
    "empty claim_text -> None, no family created",
    reg2.find_or_link_claim("", "cl_x", "science") is None and len(reg2.families) == 0,
)
check(
    "empty claim_id -> None, no family created",
    reg2.find_or_link_claim("some text", "", "science") is None and len(reg2.families) == 0,
)

# ── 8. Fail-safe: a corrupt registry file does not crash, starts empty ──

corrupt_path = Path(tempfile.mkdtemp()) / "corrupt.json"
corrupt_path.write_text("{not valid json", encoding="utf-8")
try:
    reg_corrupt = ClaimFamilyRegistry(storage_file=corrupt_path)
    check(
        "a corrupt on-disk registry file does not crash construction — starts empty",
        reg_corrupt.families == [],
    )
except Exception as e:
    check("a corrupt on-disk registry file does not crash construction", False, repr(e))

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
