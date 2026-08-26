"""
agent/source_quality.py — единая оценка качества источников YANDI.

ВАЖНО:
- качество источника != релевантность;
- качество источника != согласие источников;
- качество источника != истинность claim;
- домен сам по себе не доказывает истинность;
- Source Quality Gate определяет пригодность источника как evidence.

Логическое отношение source -> claim определяется отдельно через NLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List
from urllib.parse import urlparse


@dataclass
class SourceQualityResult:
    quality_score: float
    source_class: str

    evidence_eligible: bool
    evidence_role: str = "context"

    authority: float = 0.5
    traceability: float = 0.5
    primaryness: float = 0.5

    reasons: List[str] = field(default_factory=list)


# ------------------------------------------------------------
# Доменные признаки.
#
# Это НЕ белый список "истины".
# Они дают только prior для authority/source_class.
# ------------------------------------------------------------

SCIENTIFIC_DOMAINS = {
    "nature.com",
    "science.org",
    "sciencedirect.com",
    "springer.com",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
}

REFERENCE_DOMAINS = {
    "wikipedia.org",
    "britannica.com",
}

FORUM_DOMAINS = {
    "reddit.com",
    "quora.com",
}

SOCIAL_DOMAINS = {
    "vk.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "t.me",
}


def _hostname(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()

        if host.startswith("www."):
            host = host[4:]

        return host
    except Exception:
        return ""


def _matches_domain(host: str, domains: set[str]) -> bool:
    return any(
        host == domain or host.endswith("." + domain)
        for domain in domains
    )


def _classify_source(host: str, url: str) -> tuple[str, float, float]:
    """
    Возвращает:
        source_class,
        authority prior,
        primaryness prior
    """

    if not host:
        return "unknown", 0.25, 0.20

    # Государственные источники.
    if (
        host.endswith(".gov")
        or host.endswith(".gov.uk")
        or ".gov." in host
    ):
        return "primary", 0.90, 0.95

    # Университеты.
    if host.endswith(".edu") or ".edu." in host:
        return "scientific", 0.85, 0.75

    if _matches_domain(host, SCIENTIFIC_DOMAINS):
        return "scientific", 0.90, 0.80

    if _matches_domain(host, REFERENCE_DOMAINS):
        return "reference", 0.75, 0.45

    if _matches_domain(host, FORUM_DOMAINS):
        return "forum", 0.30, 0.25

    if _matches_domain(host, SOCIAL_DOMAINS):
        return "social", 0.20, 0.20

    lowered = url.lower()

    if "blog" in lowered:
        return "blog_opinion", 0.35, 0.30

    if "forum" in lowered:
        return "forum", 0.30, 0.25

    return "unknown", 0.50, 0.40



def _refine_source_class(
    source_class: str,
    authority: float,
    primaryness: float,
    url: str,
    title: str,
    text: str,
) -> tuple[str, float, float, List[str]]:
    """
    Уточняет класс источника по содержанию и признакам происхождения.

    Это НЕ проверка истинности текста.
    Функция определяет только тип и эпистемическую роль источника.
    """

    reasons: List[str] = []

    combined = " ".join([
        url or "",
        title or "",
        (text or "")[:5000],
    ]).lower()

    # --------------------------------------------------------
    # Форумы / пользовательские площадки
    # --------------------------------------------------------

    forum_markers = [
        "форум",
        "обсуждение",
        "ответ пользователя",
        "комментарии пользователей",
    ]

    if any(marker in combined for marker in forum_markers):
        return (
            "forum",
            min(authority, 0.30),
            min(primaryness, 0.25),
            ["content indicates forum/community source"],
        )

    # --------------------------------------------------------
    # Личные блоги / авторское мнение
    # --------------------------------------------------------

    blog_markers = [
        "livejournal",
        "личный блог",
        "мой блог",
        "мое мнение",
        "моё мнение",
        "авторский блог",
    ]

    if any(marker in combined for marker in blog_markers):
        return (
            "blog_opinion",
            min(authority, 0.35),
            min(primaryness, 0.30),
            ["content indicates personal/blog source"],
        )

    # --------------------------------------------------------
    # Спекулятивные / эзотерические источники.
    #
    # Это НЕ означает "ложь".
    # Это означает: источник не является самостоятельным
    # factual evidence для эмпирического claim.
    # --------------------------------------------------------

    speculative_markers = [
        "высший разум",
        "телепат",
        "астраль",
        "эзотер",
        "пятое измерение",
        "пять измерений",
        "энергетическое тело",
        "контактёр",
        "контактер",
        "ченнелинг",
        "откровение свыше",
        "между мирами",
    ]

    speculative_hits = sum(
        1
        for marker in speculative_markers
        if marker in combined
    )

    # Один случайный маркер внутри длинной статьи ещё не делает
    # весь источник спекулятивным: это может быть цитата,
    # обсуждение чужой позиции или название.
    #
    # Один признак достаточен только если он находится
    # непосредственно в URL/title — то есть описывает сам материал.
    title_url = " ".join([
        url or "",
        title or "",
    ]).lower()

    strong_speculative_hit = any(
        marker in title_url
        for marker in speculative_markers
    )

    if speculative_hits >= 2 or strong_speculative_hit:
        return (
            "speculative",
            min(authority, 0.20),
            min(primaryness, 0.20),
            [
                f"content contains speculative markers: "
                f"{speculative_hits}; "
                f"strong_title_url={strong_speculative_hit}"
            ],
        )

    # --------------------------------------------------------
    # Научные / исследовательские признаки
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Scientific provenance.
    #
    # ВАЖНО:
    # слова "учёные", "исследование", "институт" внутри
    # журналистской статьи НЕ превращают саму статью
    # в scientific evidence.
    #
    # Для повышения unknown -> scientific нужны признаки
    # происхождения самой публикации.
    # --------------------------------------------------------

    strong_scientific_markers = [
        "doi:",
        "doi.org/",
        "peer-reviewed",
        "рецензируемая статья",
        "рецензируемое исследование",
        "volume ",
        "issue ",
        "abstract",
        "references",
        "bibliography",
    ]

    strong_scientific_hits = sum(
        1
        for marker in strong_scientific_markers
        if marker in combined
    )

    # Unknown-домен повышаем до scientific только при
    # сильных provenance-признаках.
    #
    # Уже известные научные домены были классифицированы
    # ранее в _classify_source() и не зависят от этого блока.
    if (
        source_class == "unknown"
        and strong_scientific_hits >= 2
    ):
        return (
            "scientific",
            max(authority, 0.75),
            max(primaryness, 0.65),
            [
                f"content contains strong scientific provenance markers: "
                f"{strong_scientific_hits}"
            ],
        )

    # --------------------------------------------------------
    # Новостной / редакционный материал
    # --------------------------------------------------------

    news_markers = [
        "информационное агентство",
        "редакция",
        "новости",
        "корреспондент",
        "опубликовано",
        "автор:",
    ]

    news_hits = sum(
        1
        for marker in news_markers
        if marker in combined
    )

    if news_hits >= 2:
        return (
            "news",
            max(min(authority, 0.60), 0.50),
            min(primaryness, 0.40),
            [
                f"content indicates editorial/news source: "
                f"{news_hits}"
            ],
        )

    return source_class, authority, primaryness, reasons


def evaluate_source_quality(
    url: str,
    title: str = "",
    text: str = "",
    source_type: str = "web",
) -> SourceQualityResult:
    """
    Единая базовая оценка источника.

    Здесь НЕТ проверки:
      supports / contradicts / unrelated.

    Здесь также НЕТ проверки семантической релевантности claim.

    Это исключительно оценка пригодности самого источника
    как потенциального evidence.
    """

    reasons: List[str] = []

    host = _hostname(url)

    source_class, authority, primaryness = _classify_source(
        host,
        url,
    )

    # Уточняем тип источника по содержанию.
    (
        source_class,
        authority,
        primaryness,
        refinement_reasons,
    ) = _refine_source_class(
        source_class=source_class,
        authority=authority,
        primaryness=primaryness,
        url=url,
        title=title,
        text=text,
    )

    reasons.extend(refinement_reasons)

    # --------------------------------------------------------
    # Traceability
    # --------------------------------------------------------

    traceability = 0.0

    if url:
        traceability += 0.45
        reasons.append("source has URL")

    if title and len(title.strip()) >= 5:
        traceability += 0.20
        reasons.append("source has title")

    clean_text = (text or "").strip()

    if len(clean_text) >= 200:
        traceability += 0.20
        reasons.append("source contains substantial text")

    if len(clean_text) >= 1000:
        traceability += 0.10
        reasons.append("source contains extended text")

    if host:
        traceability += 0.05

    traceability = min(1.0, traceability)

    # --------------------------------------------------------
    # Pipeline-generated данные не считаем независимым
    # внешним evidence.
    # --------------------------------------------------------

    if "generated" in source_type.lower():
        source_class = "generated_pipeline"
        authority = min(authority, 0.20)
        primaryness = 0.10
        reasons.append("pipeline-generated source")

    # --------------------------------------------------------
    # Итоговая оценка.
    #
    # authority — кто говорит;
    # traceability — можем ли проверить;
    # primaryness — насколько близко к первоисточнику.
    # --------------------------------------------------------

    quality_score = (
        authority * 0.45
        + traceability * 0.35
        + primaryness * 0.20
    )

    quality_score = round(
        max(0.0, min(1.0, quality_score)),
        3,
    )

    # --------------------------------------------------------
    # Evidence eligibility
    # --------------------------------------------------------
    #
    # ВАЖНО:
    # traceability != credibility.
    #
    # Блог, форум или социальная сеть могут быть прекрасно
    # трассируемыми источниками, но это ещё не делает их
    # самостоятельным factual evidence.
    #
    # Unknown не запрещаем полностью: неизвестный домен может
    # оказаться нормальным специализированным ресурсом.
    # Но для него нужен более высокий score.
    blocked_classes = {
        "generated_pipeline",
        "social",
        "forum",
        "blog_opinion",
        "speculative",
        "news",
        "popular_article",
    }

    if source_class in blocked_classes:
        evidence_eligible = False
        reasons.append(
            f"source class {source_class} is not eligible "
            f"as standalone factual evidence"
        )

    elif source_class == "unknown":
        evidence_eligible = (
            quality_score >= 0.70
            and authority >= 0.50
            and traceability >= 0.70
        )

        if evidence_eligible:
            reasons.append(
                "unknown source passed strict quality gate"
            )
        else:
            reasons.append(
                "unknown source failed strict quality gate"
            )

    else:
        evidence_eligible = (
            quality_score >= 0.55
            and traceability >= 0.50
        )

        if evidence_eligible:
            reasons.append(
                "source passed base quality gate"
            )
        else:
            reasons.append(
                "source failed base quality gate"
            )

    # --------------------------------------------------------
    # Epistemic evidence role
    # --------------------------------------------------------
    #
    # direct:
    #   источник может непосредственно участвовать в factual evidence
    #
    # secondary:
    #   вторичный пересказ / журналистский материал
    #
    # context:
    #   источник может содержать позицию или гипотезу,
    #   но не должен сам по себе менять factual belief
    #
    # internal:
    #   результат собственного pipeline YANDI
    #
    if source_class in {
        "primary",
        "scientific",
        "reference",
    }:
        evidence_role = "direct"

    elif source_class in {
        "news",
        "popular_article",
    }:
        evidence_role = "secondary"

    elif source_class == "generated_pipeline":
        evidence_role = "internal"

    else:
        evidence_role = "context"

    reasons.append(f"evidence role: {evidence_role}")

    return SourceQualityResult(
        quality_score=quality_score,
        source_class=source_class,
        evidence_eligible=evidence_eligible,
        evidence_role=evidence_role,
        authority=round(authority, 3),
        traceability=round(traceability, 3),
        primaryness=round(primaryness, 3),
        reasons=reasons,
    )


# ============================================================
# EVIDENCE DIRECTNESS (YANDI_EVIDENCE_ELIGIBILITY_AND_REGISTRY_AUDIT.md, §P0-F)
# ============================================================
#
# evaluate_source_quality() выше отвечает ТОЛЬКО на вопрос
# "насколько источнику вообще можно доверять" (SOURCE AUTHORITY) —
# по домену/классу, БЕЗ знания claim, к которому evidence привязывается.
# evidence_role/evidence_eligible из этой функции остаются 100%
# производными от source_class (домена) — это НЕ меняется здесь.
#
# Отдельная, ранее отсутствовавшая ось: EVIDENCE DIRECTNESS —
# насколько КОНКРЕТНЫЙ passage действительно отвечает на КОНКРЕТНЫЙ
# claim. Источник с высоким authority может дать нерелевантный
# passage; источник с authority="unknown" (домен вне whitelist) может
# содержать passage, прямо и специфично подтверждающий claim.
#
# Эта функция НЕ заменяет evaluate_source_quality() и НЕ трогает её
# thresholds/blocked_classes. Она используется ТОЛЬКО как ВТОРОЙ,
# независимый путь для Claim Status gate (orchestrator_v2.py) —
# authority-путь остаётся первым и главным, directness — дополнение,
# не замена.
def evaluate_evidence_directness(
    claim_text: str,
    passage_text: str,
) -> float:
    """
    Cosine similarity между claim и конкретным passage evidence.

    ВАЖНО:
    - НЕ truth, НЕ support/contradict (это определяет NLI отдельно);
    - НЕ заменяет source authority — используется ТОЛЬКО вместе
      с проверкой, что source_class не входит в hard-blocked список
      (см. HARD_BLOCKED_SOURCE_CLASSES в orchestrator_v2.py);
    - при недоступном embedding endpoint возвращает 0.0 (нейтрально,
      тот же принцип, что и в claim_evidence_retriever.py
      ::_query_relevance_score) — не выдумывает directness при
      технической ошибке.
    """
    claim_text = (claim_text or "").strip()
    passage_text = (passage_text or "").strip()

    if not claim_text or not passage_text:
        return 0.0

    try:
        import requests
        import numpy as np

        session = requests.Session()
        session.trust_env = False

        def _embed(value: str):
            resp = session.post(
                "http://127.0.0.1:11434/api/embed",
                json={
                    "model": "embeddinggemma:latest",
                    "input": value[:2000],
                },
                timeout=15,
            )
            resp.raise_for_status()

            vec = np.array(
                resp.json()["embeddings"][0],
                dtype=np.float32,
            )

            norm = np.linalg.norm(vec)
            return vec / norm if norm > 0 else vec

        claim_vec = _embed(claim_text)
        passage_vec = _embed(passage_text)

        return float(np.dot(claim_vec, passage_vec))

    except Exception:
        return 0.0


if __name__ == "__main__":

    tests = [
        (
            "https://www.nature.com/articles/test",
            "Scientific paper",
            "A" * 1500,
            "web",
        ),
        (
            "https://en.wikipedia.org/wiki/Jupiter",
            "Jupiter",
            "A" * 1500,
            "web",
        ),
        (
            "https://random-blog.example/post",
            "My opinion",
            "A" * 1500,
            "web",
        ),
        (
            "https://reddit.com/r/space/test",
            "Discussion",
            "A" * 1500,
            "web",
        ),
        (
            "https://unknown.example/article",
            "Unknown source",
            "A" * 1500,
            "web",
        ),
        (
            "",
            "",
            "generated answer",
            "generated_pipeline",
        ),
    ]

    for url, title, text, source_type in tests:
        result = evaluate_source_quality(
            url=url,
            title=title,
            text=text,
            source_type=source_type,
        )

        print()
        print(url or "<NO URL>")
        print(" class       :", result.source_class)
        print(" quality     :", result.quality_score)
        print(" authority   :", result.authority)
        print(" traceability:", result.traceability)
        print(" primaryness :", result.primaryness)
        print(" eligible    :", result.evidence_eligible)
        print(" reasons     :", "; ".join(result.reasons))
