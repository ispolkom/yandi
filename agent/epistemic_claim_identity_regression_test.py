"""
agent/epistemic_claim_identity_regression_test.py — Epistemic Core v1
Phase 2 regression: deterministic claim content_hash (agent/claim_identity.py).

Proves the canonicalization policy documented in claim_identity.py's
docstring: whitespace, Unicode composition, case, and trailing punctuation
are normalized away; internal punctuation, wording, and language are NOT
(this is content identity, not semantic identity — two paraphrases must
NOT be forced to collide). Also proves the wiring into
claims/lifecycle.py's "CLAIM IDENTITY" block (claim_id stays a fresh
random UUID per occurrence; content_hash is deterministic for identical
normalized text) and the round trip into the persisted trace.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_claim_identity_regression_test
"""

import json
import time
import unicodedata

from agent.claim_identity import canonicalize_claim_text, compute_claim_content_hash
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


# ── 1. Whitespace differences collapse to the same hash ──

a = "Юпитер   имеет    кольца."
b = "  Юпитер\nимеет\tкольца.  "
check(
    "whitespace: internal runs + leading/trailing collapse to same hash",
    compute_claim_content_hash(a) == compute_claim_content_hash(b),
    f"{compute_claim_content_hash(a)} != {compute_claim_content_hash(b)}",
)

# ── 2. Unicode NFC vs NFD composition of the same visual text hash the same ──

nfc_text = unicodedata.normalize("NFC", "Café was founded in 1976.")
nfd_text = unicodedata.normalize("NFD", "Café was founded in 1976.")
check(
    "unicode: NFC and NFD forms of the same text hash identically",
    nfc_text != nfd_text  # sanity: they really are different byte sequences
    and compute_claim_content_hash(nfc_text) == compute_claim_content_hash(nfd_text),
    f"nfc_bytes_differ={nfc_text != nfd_text} "
    f"hash_nfc={compute_claim_content_hash(nfc_text)} hash_nfd={compute_claim_content_hash(nfd_text)}",
)

# ── 3. Case differences collapse (including non-ASCII casefold) ──

check(
    "case: ASCII case differences hash the same",
    compute_claim_content_hash("Aspartame is safe.") == compute_claim_content_hash("ASPARTAME IS SAFE."),
)
check(
    "case: Cyrillic case differences hash the same (casefold, not just .lower())",
    compute_claim_content_hash("Аспартам безопасен.") == compute_claim_content_hash("АСПАРТАМ БЕЗОПАСЕН."),
)

# ── 4. Trailing punctuation is stripped; INTERNAL punctuation is NOT ──

check(
    "trailing punctuation: period present/absent hashes the same",
    compute_claim_content_hash("Aspartame is safe.") == compute_claim_content_hash("Aspartame is safe"),
)
check(
    "trailing punctuation: exclamation/question marks also stripped",
    compute_claim_content_hash("Aspartame is safe!") == compute_claim_content_hash("Aspartame is safe"),
)
check(
    "internal punctuation changes the hash (not stripped, meaning-bearing)",
    compute_claim_content_hash("Aspartame, is safe.") != compute_claim_content_hash("Aspartame is safe."),
)

# ── 5. Empty / whitespace-only text -> None, not a fabricated shared identity ──

check("empty string -> None", compute_claim_content_hash("") is None)
check("whitespace-only string -> None", compute_claim_content_hash("   \n\t  ") is None)
check(
    "two distinct empty-ish claims do NOT collide on a fake shared hash",
    compute_claim_content_hash("") is None and compute_claim_content_hash("   ") is None
    and compute_claim_content_hash("") == compute_claim_content_hash("   "),  # both None, not a real collision
)

# ── 6. Multilingual text is NOT unified — different languages hash differently ──

check(
    "multilingual: Russian and English statements of the same fact do NOT collide "
    "(this is content identity, not semantic identity)",
    compute_claim_content_hash("Юпитер — крупнейшая планета.")
    != compute_claim_content_hash("Jupiter is the largest planet."),
)

# ── 7. Genuinely different claims hash differently (sanity, not a trivial always-equal function) ──

check(
    "sanity: unrelated claims hash differently",
    compute_claim_content_hash("Аспартам безопасен для здоровья.")
    != compute_claim_content_hash("Юпитер — газовый гигант."),
)

# ── 8. Number/date changes are NOT normalized away (precision matters epistemically) ──

check(
    "numeric precision: '95 moons' vs '96 moons' must NOT collide",
    compute_claim_content_hash("У Юпитера 95 спутников.")
    != compute_claim_content_hash("У Юпитера 96 спутников."),
)

# ── 9. Integration: claims/lifecycle.py's CLAIM IDENTITY block wiring ──

from agent.orchestrator.claims.lifecycle import setup_claim_and_evidence_lifecycle
import inspect

src = inspect.getsource(setup_claim_and_evidence_lifecycle)
check(
    "lifecycle.py wiring: CLAIM IDENTITY block calls compute_claim_content_hash",
    "compute_claim_content_hash" in src,
)

# Simulate two "occurrences" of the identical claim text (as if extracted
# twice, e.g. two separate requests) going through the same normalization
# path claim_identity.py exposes, and confirm: different occurrence IDs
# (by construction, uuid4 is different every call) but same content_hash.
import uuid as _uuid

occurrence_1 = {"claim_id": f"cl_{_uuid.uuid4().hex[:8]}", "claim_text": "Юпитер имеет кольца."}
occurrence_2 = {"claim_id": f"cl_{_uuid.uuid4().hex[:8]}", "claim_text": "юпитер   имеет кольца"}
occurrence_1["content_hash"] = compute_claim_content_hash(occurrence_1["claim_text"])
occurrence_2["content_hash"] = compute_claim_content_hash(occurrence_2["claim_text"])

check(
    "occurrence identity (claim_id) differs across two 'extractions' of the same text",
    occurrence_1["claim_id"] != occurrence_2["claim_id"],
)
check(
    "content identity (content_hash) is identical for the same normalized text",
    occurrence_1["content_hash"] == occurrence_2["content_hash"]
    and occurrence_1["content_hash"] is not None,
)

# ── 10. Round trip: content_hash survives Trace.add_claim_raw() -> to_dict() -> json ──

trace = Trace(trace_id="t_test", timestamp=time.time(), query="test")
claim_with_hash = {
    "claim_id": "cl_roundtrip",
    "claim_text": "Аспартам одобрен как безопасная пищевая добавка.",
    "derived_from_evidence_ids": [],
    "claim_type": "factual",
    "claim_confidence": 0.6,
    "verification_status": "supported",
    "content_hash": compute_claim_content_hash("Аспартам одобрен как безопасная пищевая добавка."),
}
trace.add_claim_raw(claim_with_hash)
rt = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
check(
    "round trip: content_hash survives serialization",
    rt["claims"][0]["content_hash"] == claim_with_hash["content_hash"]
    and rt["claims"][0]["content_hash"] is not None,
    f"{rt['claims'][0]}",
)

# ── 11. Backward compatibility: claim dict with no content_hash key at all ──

trace2 = Trace(trace_id="t_test2", timestamp=time.time(), query="test2")
claim_no_hash = {
    "claim_id": "cl_old",
    "claim_text": "Claim from code that doesn't know about content_hash yet.",
    "derived_from_evidence_ids": [],
    "claim_type": "factual",
    "claim_confidence": 0.5,
    "verification_status": "unverified",
    # no "content_hash" key
}
try:
    trace2.add_claim_raw(claim_no_hash)
    rt2 = json.loads(json.dumps(trace2.to_dict(), ensure_ascii=False))
    check(
        "backward compat: missing content_hash key -> None, no crash",
        rt2["claims"][0]["content_hash"] is None,
        f"{rt2['claims'][0]}",
    )
except Exception as e:
    check("backward compat: missing content_hash key -> None, no crash", False, repr(e))

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
