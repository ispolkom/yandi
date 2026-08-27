"""
agent/claim_semantic_identity_corpus.py — Epistemic Core v1 Phase 9:
labeled mini-corpus for evaluating claim semantic (paraphrase) identity
(agent/claim_semantic_identity_prototype.py).

Covers the categories the plan requires: exact duplicates, paraphrases,
near-paraphrases with a changed number/date/entity (the critical risk —
"Jupiter has 95 moons" vs "96 moons"), negation, scope changes, temporal
changes, causal-vs-correlational statements, and one multilingual
paraphrase (Russian/English) to see whether the pipeline's embedding
model handles it at all.
"""

# Each entry: (claim_a, claim_b, should_be_equivalent: bool, category: str)

LABELED_PAIRS = [
    # ── exact duplicate (modulo whitespace) ──
    (
        "Юпитер является крупнейшей планетой Солнечной системы.",
        "Юпитер   является  крупнейшей планетой Солнечной системы.",
        True, "exact_duplicate",
    ),

    # ── clear paraphrase, same fact, different wording ──
    (
        "Аспартам является одобренной безопасной пищевой добавкой согласно FDA.",
        "По данным FDA, аспартам признан допустимым и безопасным подсластителем.",
        True, "paraphrase",
    ),

    # ── CRITICAL: near-paraphrase with a changed NUMBER — must NOT be equivalent ──
    (
        "У Юпитера подтверждено 95 спутников.",
        "У Юпитера подтверждено 96 спутников.",
        False, "near_paraphrase_changed_number",
    ),

    # ── near-paraphrase with a changed DATE — must NOT be equivalent ──
    (
        "Компания Apple была официально зарегистрирована в 1976 году.",
        "Компания Apple была официально зарегистрирована в 1975 году.",
        False, "near_paraphrase_changed_date",
    ),

    # ── near-paraphrase with a changed ENTITY — must NOT be equivalent ──
    (
        "Роман «Война и мир» написал Лев Толстой.",
        "Роман «Война и мир» написал Фёдор Достоевский.",
        False, "near_paraphrase_changed_entity",
    ),

    # ── negation — must NOT be equivalent ──
    (
        "Аспартам безопасен для здоровья при умеренном потреблении.",
        "Аспартам не является безопасным для здоровья.",
        False, "negation",
    ),

    # ── scope change (universal vs existential quantifier) — must NOT be equivalent ──
    (
        "Все известные виды пингвинов не умеют летать.",
        "Некоторые виды пингвинов не умеют летать.",
        False, "scope_change",
    ),

    # ── temporal change — must NOT be equivalent ──
    (
        "Компания в настоящее время проводит реструктуризацию.",
        "Компания ранее уже проводила реструктуризацию несколько лет назад.",
        False, "temporal_change",
    ),

    # ── causal vs correlational — must NOT be equivalent (different epistemic strength) ──
    (
        "Курение вызывает рак лёгких.",
        "Курение статистически связано с повышенным риском рака лёгких.",
        False, "causal_vs_correlational",
    ),

    # ── multilingual paraphrase (Russian/English), same fact — hardest case, report honestly ──
    (
        "Тахион — гипотетическая частица, движущаяся быстрее света.",
        "A tachyon is a hypothetical particle that always travels faster than light.",
        True, "multilingual_paraphrase",
    ),

    # ── sanity floor: two unrelated claims — must NOT be equivalent ──
    (
        "Столица Франции — Париж.",
        "Аспартам метаболизируется в организме на фенилаланин и метанол.",
        False, "unrelated_sanity_floor",
    ),

    # ── second clear paraphrase for balance ──
    (
        "ВОЗ классифицировала аспартам как возможно канцерогенный для человека.",
        "Всемирная организация здравоохранения отнесла аспартам к категории веществ, возможно вызывающих рак у людей.",
        True, "paraphrase",
    ),
]
