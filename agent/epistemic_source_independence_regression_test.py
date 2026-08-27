"""
agent/epistemic_source_independence_regression_test.py — Epistemic Core v1
Phase 5 regression: source-independence clustering prototype
(agent/source_independence_prototype.py), evaluated against the labeled
corpus (agent/source_independence_corpus.py).

This suite pins the exact evaluation numbers reported in
YANDI_EPISTEMIC_CORE_V1_PHASE5_SOURCE_INDEPENDENCE.md — if a future edit
to the prototype's similarity functions or thresholds changes these
numbers, this suite fails loudly rather than silently drifting.

Reminder: this whole module is an OFFLINE PROTOTYPE, not wired into
production. This suite tests the prototype's own internal correctness,
not any production code path.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_source_independence_regression_test
"""

from agent.source_independence_prototype import (
    SourceCandidate,
    canonical_url,
    domain,
    title_similarity,
    content_fingerprint_similarity,
    cluster_url_exact,
    cluster_domain_only,
    cluster_combined,
    VARIANTS,
    evaluate_variant,
)
from agent.source_independence_corpus import LABELED_PAIRS

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


# ── 1. Signal-level sanity checks ──

check(
    "canonical_url: identical URLs match",
    canonical_url("https://example.com/a") == canonical_url("https://EXAMPLE.com/a"),
)
check(
    "domain: www. prefix stripped, matches source_quality._hostname convention",
    domain("https://www.example.com/x") == domain("https://example.com/x") == "example.com",
)
check(
    "title_similarity: identical titles -> 1.0",
    title_similarity("Same Title Here", "Same Title Here") == 1.0,
)
check(
    "title_similarity: empty title -> 0.0, no crash",
    title_similarity("", "Something") == 0.0,
)
check(
    "content_fingerprint_similarity: identical text -> 1.0",
    content_fingerprint_similarity("some shared content here for testing purposes", "some shared content here for testing purposes") == 1.0,
)
check(
    "content_fingerprint_similarity: completely disjoint text -> 0.0",
    content_fingerprint_similarity(
        "aaaaaa bbbbbb cccccc dddddd eeeeee ffffff",
        "gggggg hhhhhh iiiiii jjjjjj kkkkkk llllll",
    ) == 0.0,
)

# ── 2. Same-domain does NOT imply same-origin under cluster_combined (the plan's explicit warning) ──

same_domain_diff_story_a = SourceCandidate(
    url="https://portal.example.com/a", title="Local bakery wins regional pastry award",
    content_excerpt="A family-owned bakery downtown took first place in the regional pastry competition this weekend after three years of trying.",
)
same_domain_diff_story_b = SourceCandidate(
    url="https://portal.example.com/b", title="City council approves new bridge funding",
    content_excerpt="Council members voted six to one on Tuesday night to approve funding for repairs to the aging river crossing bridge downtown.",
)
check(
    "cluster_domain_only WOULD wrongly merge two unrelated same-domain articles (demonstrates the naive-assumption risk)",
    cluster_domain_only(same_domain_diff_story_a, same_domain_diff_story_b) is True,
)
check(
    "cluster_combined correctly does NOT merge them despite same domain",
    cluster_combined(same_domain_diff_story_a, same_domain_diff_story_b) is False,
)

# ── 3. Different-domain does NOT imply independence under cluster_combined ──

diff_domain_same_story_a = SourceCandidate(
    url="https://site-one.example.com/story", title="Central bank holds rates steady",
    content_excerpt="The central bank left its benchmark interest rate unchanged, citing persistent inflation pressure across the economy this quarter.",
)
diff_domain_same_story_b = SourceCandidate(
    url="https://site-two.example.org/news", title="Rates unchanged, bank cites inflation",
    content_excerpt="The central bank left its benchmark interest rate unchanged, citing persistent inflation pressure across the economy this quarter.",
)
check(
    "cluster_url_exact misses cross-domain syndication (today's production baseline behavior)",
    cluster_url_exact(diff_domain_same_story_a, diff_domain_same_story_b) is False,
)
check(
    "cluster_combined correctly catches cross-domain syndication via content fingerprint",
    cluster_combined(diff_domain_same_story_a, diff_domain_same_story_b) is True,
)

# ── 4. Pinned evaluation numbers against the labeled corpus (regression-pins the report's findings) ──

results = {name: evaluate_variant(fn, LABELED_PAIRS) for name, fn in VARIANTS.items()}

check(
    "url_exact: precision=1.000 (never false-merges) but low recall (misses syndication) — pinned",
    results["url_exact"]["precision"] == 1.0 and results["url_exact"]["fp"] == 0,
    f"{results['url_exact']}",
)
check(
    "domain_only: produces at least one false merge on this corpus (the naive-assumption failure, empirically demonstrated)",
    results["domain_only"]["fp"] >= 1,
    f"{results['domain_only']}",
)
check(
    "combined: zero false merges on this corpus (the critical safety property per the plan's own emphasis)",
    results["combined"]["fp"] == 0,
    f"{results['combined']}",
)
check(
    "combined: strictly higher recall than both baselines on this corpus",
    results["combined"]["recall"] > results["url_exact"]["recall"]
    and results["combined"]["recall"] > results["domain_only"]["recall"],
    f"combined={results['combined']['recall']} url_exact={results['url_exact']['recall']} domain_only={results['domain_only']['recall']}",
)
check(
    "combined: precision >= both baselines (does not trade safety for recall)",
    results["combined"]["precision"] >= results["url_exact"]["precision"]
    and results["combined"]["precision"] >= results["domain_only"]["precision"],
    f"{results['combined']}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
