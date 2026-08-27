"""
agent/source_independence_corpus.py — Epistemic Core v1 Phase 5: labeled
mini-corpus for evaluating source-independence clustering variants
(agent/source_independence_prototype.py).

*** SYNTHETIC EVALUATION DATA — NOT SCRAPED, NOT REAL PUBLISHERS. ***
All URLs use example.* / test domains deliberately, so this corpus is
never mistaken for real citations or real scraped content. Each pair
below is hand-constructed to be representative of a real-world pattern
(wire syndication, independent reporting, etc.) without claiming to BE
that real content.

Covers the 7 categories the plan requires: A/A exact duplicates, same
publisher different article, wire story syndicated across domains,
independent reporting of the same fact, partial copy, citation of the
original source, and genuinely independent sources — with both positive
(should cluster) and negative (should NOT cluster) examples so precision
and recall are both measurable, not just one or the other.
"""

from agent.source_independence_prototype import SourceCandidate

# Each entry: (SourceCandidate A, SourceCandidate B, should_cluster: bool, category: str)

LABELED_PAIRS = [
    # ── 1. A/A exact duplicate (same URL, same everything) ──
    (
        SourceCandidate(
            url="https://wire-a.example.com/story/aspartame-2026",
            title="FDA review finds no new safety concerns for aspartame",
            content_excerpt="FDA scientists reviewed the available data again in 2026 and found no new safety concerns for aspartame under approved conditions of use.",
        ),
        SourceCandidate(
            url="https://wire-a.example.com/story/aspartame-2026",
            title="FDA review finds no new safety concerns for aspartame",
            content_excerpt="FDA scientists reviewed the available data again in 2026 and found no new safety concerns for aspartame under approved conditions of use.",
        ),
        True, "aa_exact_duplicate",
    ),

    # ── 2. Same publisher, DIFFERENT article (same domain, unrelated story) ──
    (
        SourceCandidate(
            url="https://news-hub.example.com/tech/apple-earnings-q3",
            title="Apple reports record quarterly revenue",
            content_excerpt="Apple Inc. reported record quarterly revenue driven by strong iPhone and services sales in the most recent quarter.",
        ),
        SourceCandidate(
            url="https://news-hub.example.com/health/aspartame-review",
            title="Health regulators revisit sweetener safety data",
            content_excerpt="Health regulators in several countries have revisited safety data for common artificial sweeteners including aspartame this year.",
        ),
        False, "same_publisher_different_article",
    ),

    # ── 3. Wire story syndicated across DIFFERENT domains (the critical case) ──
    (
        SourceCandidate(
            url="https://regional-outlet-a.example.com/world/apple-founding",
            title="How three men in a garage built Apple Computer",
            content_excerpt="Steve Jobs, Steve Wozniak and Ronald Wayne founded Apple Computer Company in a garage in 1976. Wayne sold his 10 percent stake for 800 dollars less than two weeks later, a decision he says he does not regret.",
        ),
        SourceCandidate(
            url="https://regional-outlet-b.example.com/business/apple-origin-story",
            title="Three men, a garage, and the birth of Apple",
            content_excerpt="Steve Jobs, Steve Wozniak and Ronald Wayne founded Apple Computer Company in a garage back in 1976. Wayne would sell his 10 percent stake for just 800 dollars less than two weeks later, a decision he says he does not regret.",
        ),
        True, "wire_syndicated_cross_domain",
    ),

    # ── 4. Independent reporting of the SAME underlying fact, different domains, different wording/framing ──
    (
        SourceCandidate(
            url="https://outlet-x.example.com/science/aspartame-cancer-who",
            title="WHO classifies aspartame as possibly carcinogenic",
            content_excerpt="Международное агентство по изучению рака при ВОЗ в 2023 году включило аспартам в список веществ, «возможно канцерогенных для человека», подчеркнув при этом, что доказательства ограничены и текущие нормы потребления не меняются.",
        ),
        SourceCandidate(
            url="https://outlet-y.example.com/health/sweetener-debate-2023",
            title="Sugar substitute under scrutiny after WHO ruling",
            content_excerpt="После решения комитета ВОЗ по аспартаму диетологи разделились: одни считают классификацию поводом для осторожности, другие указывают, что реальная суточная доза для среднего человека остаётся далеко ниже опасного порога.",
        ),
        False, "independent_reporting_same_fact",
    ),

    # ── 5. Partial copy — a short aggregator blurb lifted verbatim from a longer article ──
    (
        SourceCandidate(
            url="https://longform-outlet.example.com/features/tolstoy-war-and-peace",
            title="Inside the writing of War and Peace",
            content_excerpt="Лев Толстой начал работу над романом «Война и мир» в 1863 году и завершил его только в 1869-м, переписывая отдельные главы по многу раз в поисках нужной интонации повествования.",
        ),
        SourceCandidate(
            url="https://aggregator-site.example.com/digest/todays-literature-facts",
            title="Literary fact of the day",
            content_excerpt="Толстой начал работу над романом «Война и мир» в 1863 году и завершил его только в 1869-м.",
        ),
        True, "partial_copy",
    ),

    # ── 6. Citation of the original source — attributed quote inside otherwise original commentary ──
    (
        SourceCandidate(
            url="https://original-report.example.com/investigations/jupiter-moon-count",
            title="Astronomers confirm 12 new moons of Jupiter",
            content_excerpt="A team at the Carnegie Institution announced the confirmation of 12 previously unconfirmed moons of Jupiter, bringing the known total higher than any other planet in the solar system.",
        ),
        SourceCandidate(
            url="https://commentary-blog.example.com/opinion/why-moon-counts-matter",
            title="Why we keep finding more moons — and why it matters",
            content_excerpt="As the Carnegie Institution team reported, astronomers confirmed 12 previously unconfirmed moons of Jupiter. What's more interesting than the number itself, in my view, is what it says about how survey telescope sensitivity has improved over the last decade.",
        ),
        True, "citation_of_original_source",
    ),

    # ── 7. Genuinely independent sources — different domains, different wording, no citation relationship ──
    (
        SourceCandidate(
            url="https://encyclopedia-mirror.example.org/wiki/tachyon",
            title="Tachyon — hypothetical faster-than-light particle",
            content_excerpt="A tachyon is a hypothetical particle that always travels faster than light. No experimental evidence for the existence of tachyons has been found, and their existence would be difficult to reconcile with causality as currently understood.",
        ),
        SourceCandidate(
            url="https://physics-forum-archive.example.net/threads/faster-than-light-particles-explained",
            title="Разбираем гипотезу о тахионах простыми словами",
            content_excerpt="Идея тахионов возникла как математическое следствие уравнений специальной теории относительности при мнимой массе покоя. Экспериментально их пока никто не зарегистрировал, а некоторые физики считают саму концепцию несовместимой с причинностью.",
        ),
        False, "genuinely_independent",
    ),

    # ── extra negative: two clearly unrelated topics, different domains (sanity floor) ──
    (
        SourceCandidate(
            url="https://cooking-site.example.com/recipes/borscht",
            title="Классический рецепт борща",
            content_excerpt="Для классического борща понадобятся свёкла, капуста, картофель, морковь и немного уксуса для сохранения яркого цвета готового блюда.",
        ),
        SourceCandidate(
            url="https://space-news.example.com/articles/jupiter-great-red-spot",
            title="Большое красное пятно Юпитера продолжает уменьшаться",
            content_excerpt="Наблюдения последних лет показывают, что знаменитое Большое красное пятно на Юпитере продолжает медленно сокращаться в размерах по сравнению с оценками столетней давности.",
        ),
        False, "unrelated_sanity_floor",
    ),

    # ── extra positive: wire syndication with a byline/intro difference (harder case) ──
    (
        SourceCandidate(
            url="https://finance-portal-a.example.com/markets/central-bank-rate-decision",
            title="Central bank holds rates steady, cites inflation concerns",
            content_excerpt="(Reuters-style wire) — The central bank left its benchmark interest rate unchanged on Wednesday, citing persistent inflation concerns and signaling that further hikes remain possible if price pressures do not ease in coming months.",
        ),
        SourceCandidate(
            url="https://finance-portal-b.example.com/economy/rates-unchanged-inflation",
            title="Rates unchanged as inflation worries persist",
            content_excerpt="The central bank left its benchmark interest rate unchanged on Wednesday, citing persistent inflation concerns, and signaled further hikes remain possible if price pressures do not ease in the coming months.",
        ),
        True, "wire_syndicated_cross_domain",
    ),
]
